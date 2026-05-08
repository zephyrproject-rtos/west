//! Layered TOML configuration for west.
//!
//! `Configuration` is an ordered stack of TOML layers. Layer order is
//! low-to-high precedence: layers later in the stack override earlier ones on
//! merged reads. The store is level-agnostic — see [`crate::config_paths`] for
//! west's system/global/local convention.

use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use log::trace;
use toml_edit::{DocumentMut, Item, Table, Value};

#[derive(Debug, Clone, PartialEq)]
pub enum ConfigValue {
    String(String),
    Bool(bool),
    Integer(i64),
    Float(f64),
    List(Vec<ConfigValue>),
}

impl ConfigValue {
    /// Build a `List` of strings from any iterable of string-like items.
    pub fn list_of_strings<I, S>(items: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        ConfigValue::List(
            items
                .into_iter()
                .map(|s| ConfigValue::String(s.into()))
                .collect(),
        )
    }

    /// Parse a CLI-supplied value as a TOML expression. Bare strings (no
    /// leading TOML-construct char and not parseable as TOML) fall back to
    /// `ConfigValue::String`. Values that *look* like TOML constructs but
    /// fail to parse are rejected — callers must quote them explicitly.
    pub fn parse(s: &str) -> Result<Self, ConfigError> {
        let wrapped = format!("__v = {s}");
        match wrapped.parse::<DocumentMut>() {
            Ok(doc) => doc
                .get("__v")
                .and_then(|i| i.as_value())
                .and_then(ConfigValue::from_toml_value)
                .ok_or_else(|| ConfigError::InvalidValue {
                    input: s.to_owned(),
                    message: "unsupported TOML value type".to_owned(),
                }),
            Err(e) => {
                if s.starts_with(['[', '{', '"', '\'']) {
                    Err(ConfigError::InvalidValue {
                        input: s.to_owned(),
                        message: e.to_string(),
                    })
                } else {
                    Ok(ConfigValue::String(s.to_owned()))
                }
            }
        }
    }

    fn into_toml_value(self) -> Value {
        match self {
            ConfigValue::String(s) => Value::from(s),
            ConfigValue::Bool(b) => Value::from(b),
            ConfigValue::Integer(i) => Value::from(i),
            ConfigValue::Float(f) => Value::from(f),
            ConfigValue::List(items) => {
                Value::Array(items.into_iter().map(|cv| cv.into_toml_value()).collect())
            }
        }
    }

    fn from_toml_value(v: &Value) -> Option<Self> {
        match v {
            Value::String(s) => Some(ConfigValue::String(s.value().clone())),
            Value::Boolean(b) => Some(ConfigValue::Bool(*b.value())),
            Value::Integer(i) => Some(ConfigValue::Integer(*i.value())),
            Value::Float(f) => Some(ConfigValue::Float(*f.value())),
            Value::Array(arr) => arr
                .iter()
                .map(ConfigValue::from_toml_value)
                .collect::<Option<Vec<_>>>()
                .map(ConfigValue::List),
            _ => None,
        }
    }
}

/// Scalars render to their natural representation. Lists deliberately error
/// out — the CLI is responsible for per-element formatting.
impl fmt::Display for ConfigValue {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ConfigValue::String(s) => f.write_str(s),
            ConfigValue::Bool(b) => write!(f, "{b}"),
            ConfigValue::Integer(i) => write!(f, "{i}"),
            ConfigValue::Float(x) => write!(f, "{x}"),
            ConfigValue::List(_) => Err(fmt::Error),
        }
    }
}

#[derive(Debug)]
pub enum ConfigError {
    InvalidKey(String),
    InvalidValue {
        input: String,
        message: String,
    },
    UnknownLayer(PathBuf),
    Io {
        path: PathBuf,
        source: std::io::Error,
    },
    MalformedToml {
        path: PathBuf,
        source: toml_edit::TomlError,
    },
    TypeMismatch {
        option: String,
        expected: &'static str,
    },
    NotFound(String),
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ConfigError::InvalidKey(k) => {
                write!(
                    f,
                    "invalid configuration key {k:?} (expected `section.key`)"
                )
            }
            ConfigError::InvalidValue { input, message } => {
                write!(f, "invalid configuration value {input:?}: {message}")
            }
            ConfigError::UnknownLayer(p) => {
                write!(f, "unknown configuration layer: {}", p.display())
            }
            ConfigError::Io { path, source } => {
                write!(f, "io error on {}: {source}", path.display())
            }
            ConfigError::MalformedToml { path, source } => {
                write!(f, "malformed TOML in {}: {source}", path.display())
            }
            ConfigError::TypeMismatch { option, expected } => {
                write!(
                    f,
                    "configuration option {option:?} cannot be read as {expected}"
                )
            }
            ConfigError::NotFound(o) => write!(f, "configuration option not found: {o}"),
        }
    }
}

impl Error for ConfigError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            ConfigError::Io { source, .. } => Some(source),
            ConfigError::MalformedToml { source, .. } => Some(source),
            _ => None,
        }
    }
}

#[derive(Debug)]
struct Layer {
    path: PathBuf,
    doc: DocumentMut,
    exists: bool,
}

#[derive(Debug)]
pub struct Configuration {
    layers: Vec<Layer>,
    /// Ad-hoc, read-only overrides at top precedence. Sourced from `--config`
    /// CLI flags; never persisted; ignored by writes.
    inline: Option<DocumentMut>,
}

impl Configuration {
    /// Load each path in order. Missing files become empty layers. Layers
    /// later in the iterator override earlier ones on merged reads.
    pub fn load<I, P>(layer_paths: I) -> Result<Self, ConfigError>
    where
        I: IntoIterator<Item = P>,
        P: Into<PathBuf>,
    {
        let mut layers = Vec::new();
        for path in layer_paths {
            let path = path.into();
            let (doc, exists) = match fs::read_to_string(&path) {
                Ok(s) => {
                    let doc = s
                        .parse::<DocumentMut>()
                        .map_err(|e| ConfigError::MalformedToml {
                            path: path.clone(),
                            source: e,
                        })?;
                    (doc, true)
                }
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => (DocumentMut::new(), false),
                Err(e) => {
                    return Err(ConfigError::Io {
                        path: path.clone(),
                        source: e,
                    });
                }
            };
            trace!("loaded config layer {} (exists={exists})", path.display());
            layers.push(Layer { path, doc, exists });
        }
        Ok(Configuration {
            layers,
            inline: None,
        })
    }

    /// Attach ad-hoc overrides at top precedence. Reads consult them first;
    /// `set` / `delete` never touch them.
    pub fn with_inline(mut self, doc: DocumentMut) -> Self {
        self.inline = Some(doc);
        self
    }

    /// Set an option in the inline-override layer (creating the layer if
    /// absent). Inline overrides are read-only as far as `set` / `delete` /
    /// `delete_topmost` go — this method is the *only* way to populate them
    /// programmatically.
    pub fn set_inline(&mut self, option: &str, value: ConfigValue) -> Result<(), ConfigError> {
        let (section, key) = parse_key(option)?;
        let parts = dotted_parts(section, key);
        let (leaf, prefix) = parts.split_last().expect("parse_key ensures non-empty");
        let inline = self.inline.get_or_insert_with(DocumentMut::new);
        let table = ensure_table_mut(inline, prefix, option)?;
        table[*leaf] = Item::Value(value.into_toml_value());
        Ok(())
    }

    /// Walk inline → highest-precedence layer → lowest, returning the first
    /// `Value` for `section.key`.
    fn merged_lookup(&self, section: &str, key: &str) -> Option<&Value> {
        if let Some(inline) = &self.inline {
            if let Some(v) = lookup(inline, section, key) {
                return Some(v);
            }
        }
        for layer in self.layers.iter().rev() {
            if let Some(v) = lookup(&layer.doc, section, key) {
                return Some(v);
            }
        }
        None
    }

    /// Generic accessor returning the raw [`ConfigValue`] (or `None`).
    /// Useful for the CLI's display path which doesn't pre-commit to a type.
    pub fn get(&self, option: &str) -> Result<Option<ConfigValue>, ConfigError> {
        let (section, key) = parse_key(option)?;
        match self.merged_lookup(section, key) {
            Some(v) => {
                ConfigValue::from_toml_value(v)
                    .map(Some)
                    .ok_or_else(|| ConfigError::TypeMismatch {
                        option: option.to_owned(),
                        expected: "scalar or list",
                    })
            }
            None => Ok(None),
        }
    }

    pub fn get_str(&self, option: &str) -> Result<Option<String>, ConfigError> {
        let (section, key) = parse_key(option)?;
        match self.merged_lookup(section, key) {
            Some(v) => value_as_string(v)
                .map(Some)
                .ok_or_else(|| ConfigError::TypeMismatch {
                    option: option.to_owned(),
                    expected: "string",
                }),
            None => Ok(None),
        }
    }

    pub fn get_bool(&self, option: &str) -> Result<Option<bool>, ConfigError> {
        let (section, key) = parse_key(option)?;
        match self.merged_lookup(section, key) {
            Some(v) => value_as_bool(v)
                .map(Some)
                .ok_or_else(|| ConfigError::TypeMismatch {
                    option: option.to_owned(),
                    expected: "bool",
                }),
            None => Ok(None),
        }
    }

    pub fn get_i64(&self, option: &str) -> Result<Option<i64>, ConfigError> {
        let (section, key) = parse_key(option)?;
        match self.merged_lookup(section, key) {
            Some(v) => value_as_i64(v)
                .map(Some)
                .ok_or_else(|| ConfigError::TypeMismatch {
                    option: option.to_owned(),
                    expected: "integer",
                }),
            None => Ok(None),
        }
    }

    pub fn get_f64(&self, option: &str) -> Result<Option<f64>, ConfigError> {
        let (section, key) = parse_key(option)?;
        match self.merged_lookup(section, key) {
            Some(v) => value_as_f64(v)
                .map(Some)
                .ok_or_else(|| ConfigError::TypeMismatch {
                    option: option.to_owned(),
                    expected: "float",
                }),
            None => Ok(None),
        }
    }

    pub fn get_str_in(&self, option: &str, layer: &Path) -> Result<Option<String>, ConfigError> {
        let (section, key) = parse_key(option)?;
        let idx = self.find_layer(layer)?;
        match lookup(&self.layers[idx].doc, section, key) {
            Some(v) => value_as_string(v)
                .map(Some)
                .ok_or_else(|| ConfigError::TypeMismatch {
                    option: option.to_owned(),
                    expected: "string",
                }),
            None => Ok(None),
        }
    }

    pub fn get_bool_in(&self, option: &str, layer: &Path) -> Result<Option<bool>, ConfigError> {
        let (section, key) = parse_key(option)?;
        let idx = self.find_layer(layer)?;
        match lookup(&self.layers[idx].doc, section, key) {
            Some(v) => value_as_bool(v)
                .map(Some)
                .ok_or_else(|| ConfigError::TypeMismatch {
                    option: option.to_owned(),
                    expected: "bool",
                }),
            None => Ok(None),
        }
    }

    pub fn get_i64_in(&self, option: &str, layer: &Path) -> Result<Option<i64>, ConfigError> {
        let (section, key) = parse_key(option)?;
        let idx = self.find_layer(layer)?;
        match lookup(&self.layers[idx].doc, section, key) {
            Some(v) => value_as_i64(v)
                .map(Some)
                .ok_or_else(|| ConfigError::TypeMismatch {
                    option: option.to_owned(),
                    expected: "integer",
                }),
            None => Ok(None),
        }
    }

    pub fn get_f64_in(&self, option: &str, layer: &Path) -> Result<Option<f64>, ConfigError> {
        let (section, key) = parse_key(option)?;
        let idx = self.find_layer(layer)?;
        match lookup(&self.layers[idx].doc, section, key) {
            Some(v) => value_as_f64(v)
                .map(Some)
                .ok_or_else(|| ConfigError::TypeMismatch {
                    option: option.to_owned(),
                    expected: "float",
                }),
            None => Ok(None),
        }
    }

    pub fn get_list(&self, option: &str) -> Result<Option<Vec<ConfigValue>>, ConfigError> {
        let (section, key) = parse_key(option)?;
        match self.merged_lookup(section, key) {
            Some(v) => value_as_list(v)
                .map(Some)
                .ok_or_else(|| ConfigError::TypeMismatch {
                    option: option.to_owned(),
                    expected: "list",
                }),
            None => Ok(None),
        }
    }

    pub fn get_in(&self, option: &str, layer: &Path) -> Result<Option<ConfigValue>, ConfigError> {
        let (section, key) = parse_key(option)?;
        let idx = self.find_layer(layer)?;
        match lookup(&self.layers[idx].doc, section, key) {
            Some(v) => {
                ConfigValue::from_toml_value(v)
                    .map(Some)
                    .ok_or_else(|| ConfigError::TypeMismatch {
                        option: option.to_owned(),
                        expected: "scalar or list",
                    })
            }
            None => Ok(None),
        }
    }

    pub fn get_list_in(
        &self,
        option: &str,
        layer: &Path,
    ) -> Result<Option<Vec<ConfigValue>>, ConfigError> {
        let (section, key) = parse_key(option)?;
        let idx = self.find_layer(layer)?;
        match lookup(&self.layers[idx].doc, section, key) {
            Some(v) => value_as_list(v)
                .map(Some)
                .ok_or_else(|| ConfigError::TypeMismatch {
                    option: option.to_owned(),
                    expected: "list",
                }),
            None => Ok(None),
        }
    }

    pub fn get_list_str(&self, option: &str) -> Result<Option<Vec<String>>, ConfigError> {
        let (section, key) = parse_key(option)?;
        match self.merged_lookup(section, key) {
            Some(v) => value_as_list_str(v)
                .map(Some)
                .ok_or_else(|| ConfigError::TypeMismatch {
                    option: option.to_owned(),
                    expected: "list of strings",
                }),
            None => Ok(None),
        }
    }

    pub fn get_list_str_in(
        &self,
        option: &str,
        layer: &Path,
    ) -> Result<Option<Vec<String>>, ConfigError> {
        let (section, key) = parse_key(option)?;
        let idx = self.find_layer(layer)?;
        match lookup(&self.layers[idx].doc, section, key) {
            Some(v) => value_as_list_str(v)
                .map(Some)
                .ok_or_else(|| ConfigError::TypeMismatch {
                    option: option.to_owned(),
                    expected: "list of strings",
                }),
            None => Ok(None),
        }
    }

    pub fn set(
        &mut self,
        option: &str,
        value: ConfigValue,
        layer: &Path,
    ) -> Result<(), ConfigError> {
        let (section, key) = parse_key(option)?;
        let parts = dotted_parts(section, key);
        let (leaf, prefix) = parts.split_last().expect("parse_key ensures non-empty");
        let idx = self.find_layer(layer)?;
        let l = &mut self.layers[idx];

        let table = ensure_table_mut(&mut l.doc, prefix, option)?;
        table[*leaf] = Item::Value(value.into_toml_value());

        write_atomic(&l.path, &l.doc)?;
        l.exists = true;
        Ok(())
    }

    pub fn delete(&mut self, option: &str, layer: &Path) -> Result<(), ConfigError> {
        let (section, key) = parse_key(option)?;
        let parts = dotted_parts(section, key);
        let idx = self.find_layer(layer)?;
        let l = &mut self.layers[idx];

        delete_nested(&mut l.doc, &parts, option)?;
        write_atomic(&l.path, &l.doc)?;
        Ok(())
    }

    /// Delete from the highest-precedence layer that contains the key.
    pub fn delete_topmost(&mut self, option: &str) -> Result<(), ConfigError> {
        let (section, key) = parse_key(option)?;
        let target = self
            .layers
            .iter()
            .enumerate()
            .rev()
            .find(|(_, l)| lookup(&l.doc, section, key).is_some())
            .map(|(i, _)| i)
            .ok_or_else(|| ConfigError::NotFound(option.to_owned()))?;
        let path = self.layers[target].path.clone();
        self.delete(option, &path)
    }

    /// Merged dotted-key view across all layers. Higher-precedence layers
    /// override lower-precedence ones; inline overrides win over everything.
    pub fn items(&self) -> Vec<(String, ConfigValue)> {
        let mut merged = BTreeMap::new();
        for layer in &self.layers {
            for (k, v) in collect_items(&layer.doc) {
                merged.insert(k, v);
            }
        }
        if let Some(inline) = &self.inline {
            for (k, v) in collect_items(inline) {
                merged.insert(k, v);
            }
        }
        merged.into_iter().collect()
    }

    pub fn items_in(&self, layer: &Path) -> Result<Vec<(String, ConfigValue)>, ConfigError> {
        let idx = self.find_layer(layer)?;
        let mut items = collect_items(&self.layers[idx].doc);
        items.sort_by(|a, b| a.0.cmp(&b.0));
        Ok(items)
    }

    pub fn layer_paths(&self) -> Vec<&Path> {
        self.layers.iter().map(|l| l.path.as_path()).collect()
    }

    /// Subset of [`Self::layer_paths`] for layers that existed on disk at
    /// load time.
    pub fn existing(&self) -> Vec<&Path> {
        self.layers
            .iter()
            .filter(|l| l.exists)
            .map(|l| l.path.as_path())
            .collect()
    }

    fn find_layer(&self, path: &Path) -> Result<usize, ConfigError> {
        self.layers
            .iter()
            .position(|l| l.path == path)
            .ok_or_else(|| ConfigError::UnknownLayer(path.to_owned()))
    }
}

fn parse_key(option: &str) -> Result<(&str, &str), ConfigError> {
    match option.split_once('.') {
        Some((s, k)) if !s.is_empty() && !k.is_empty() => Ok((s, k)),
        _ => Err(ConfigError::InvalidKey(option.to_owned())),
    }
}

/// Build the full dotted path from `(section, rest)`. Each segment must be
/// non-empty.
fn dotted_parts<'a>(section: &'a str, key: &'a str) -> Vec<&'a str> {
    std::iter::once(section).chain(key.split('.')).collect()
}

/// Walk a dotted path through nested tables (and inline tables) and return
/// the leaf [`Value`] if it exists.
fn lookup<'a>(doc: &'a DocumentMut, section: &str, key: &str) -> Option<&'a Value> {
    let parts = dotted_parts(section, key);
    let (leaf, prefix) = parts.split_last()?;
    let mut cur = Cursor::Table(doc);
    for part in prefix {
        cur = cur.descend(part)?;
    }
    cur.value(leaf)
}

/// Cursor for navigating either kind of TOML table during read.
enum Cursor<'a> {
    Table(&'a toml_edit::Table),
    Inline(&'a toml_edit::InlineTable),
}

impl<'a> Cursor<'a> {
    fn descend(self, name: &str) -> Option<Cursor<'a>> {
        match self {
            Cursor::Table(t) => {
                let item = t.get(name)?;
                if let Some(t) = item.as_table() {
                    Some(Cursor::Table(t))
                } else if let Some(it) = item.as_inline_table() {
                    Some(Cursor::Inline(it))
                } else {
                    None
                }
            }
            Cursor::Inline(it) => {
                let v = it.get(name)?;
                v.as_inline_table().map(Cursor::Inline)
            }
        }
    }

    fn value(self, name: &str) -> Option<&'a Value> {
        match self {
            Cursor::Table(t) => t.get(name).and_then(|i| i.as_value()),
            Cursor::Inline(it) => it.get(name),
        }
    }
}

/// Walk + create intermediate sub-tables, returning the deepest table.
/// Errors if any intermediate exists but is not a standard table (e.g.,
/// it's an inline table or a scalar).
fn ensure_table_mut<'a>(
    table: &'a mut Table,
    parts: &[&str],
    option: &str,
) -> Result<&'a mut Table, ConfigError> {
    if parts.is_empty() {
        return Ok(table);
    }
    let head = parts[0];
    let item = table
        .entry(head)
        .or_insert_with(|| Item::Table(Table::new()));
    let sub = item
        .as_table_mut()
        .ok_or_else(|| ConfigError::TypeMismatch {
            option: option.to_owned(),
            expected: "table",
        })?;
    ensure_table_mut(sub, &parts[1..], option)
}

/// Recursively delete the leaf at `parts` and clean up any sub-tables that
/// become empty along the way.
fn delete_nested(table: &mut Table, parts: &[&str], option: &str) -> Result<(), ConfigError> {
    debug_assert!(!parts.is_empty());
    if parts.len() == 1 {
        if table.remove(parts[0]).is_none() {
            return Err(ConfigError::NotFound(option.to_owned()));
        }
        return Ok(());
    }
    let head = parts[0];
    let item = table
        .get_mut(head)
        .ok_or_else(|| ConfigError::NotFound(option.to_owned()))?;
    let sub = item
        .as_table_mut()
        .ok_or_else(|| ConfigError::NotFound(option.to_owned()))?;
    delete_nested(sub, &parts[1..], option)?;
    if sub.is_empty() {
        table.remove(head);
    }
    Ok(())
}

fn value_as_string(v: &Value) -> Option<String> {
    v.as_str().map(str::to_owned)
}

fn value_as_bool(v: &Value) -> Option<bool> {
    v.as_bool()
}

fn value_as_i64(v: &Value) -> Option<i64> {
    v.as_integer()
}

fn value_as_f64(v: &Value) -> Option<f64> {
    v.as_float()
}

fn value_as_list(v: &Value) -> Option<Vec<ConfigValue>> {
    let arr = v.as_array()?;
    arr.iter().map(ConfigValue::from_toml_value).collect()
}

fn value_as_list_str(v: &Value) -> Option<Vec<String>> {
    let arr = v.as_array()?;
    arr.iter().map(value_as_string).collect()
}

fn collect_items(doc: &DocumentMut) -> Vec<(String, ConfigValue)> {
    let mut out = Vec::new();
    walk_table_items(doc, "", &mut out);
    out
}

/// Recursively walk `table` and append `(dotted-key, value)` pairs into `out`.
/// Nested tables produce dotted keys; the leaf must be a serializable
/// scalar/list (anything `ConfigValue::from_toml_value` accepts).
fn walk_table_items(table: &Table, prefix: &str, out: &mut Vec<(String, ConfigValue)>) {
    for (key, item) in table.iter() {
        let path = if prefix.is_empty() {
            key.to_owned()
        } else {
            format!("{prefix}.{key}")
        };
        if let Some(sub) = item.as_table() {
            walk_table_items(sub, &path, out);
        } else if let Some(inline) = item.as_inline_table() {
            walk_inline_items(inline, &path, out);
        } else if let Some(v) = item.as_value() {
            if let Some(cv) = ConfigValue::from_toml_value(v) {
                out.push((path, cv));
            }
        }
    }
}

fn walk_inline_items(
    inline: &toml_edit::InlineTable,
    prefix: &str,
    out: &mut Vec<(String, ConfigValue)>,
) {
    for (key, v) in inline.iter() {
        let path = format!("{prefix}.{key}");
        if let Some(nested) = v.as_inline_table() {
            walk_inline_items(nested, &path, out);
        } else if let Some(cv) = ConfigValue::from_toml_value(v) {
            out.push((path, cv));
        }
    }
}

fn write_atomic(path: &Path, doc: &DocumentMut) -> Result<(), ConfigError> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent).map_err(|e| ConfigError::Io {
                path: parent.to_owned(),
                source: e,
            })?;
        }
    }
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    let mut tmp = tempfile::NamedTempFile::new_in(parent).map_err(|e| ConfigError::Io {
        path: path.to_owned(),
        source: e,
    })?;
    tmp.write_all(doc.to_string().as_bytes())
        .map_err(|e| ConfigError::Io {
            path: path.to_owned(),
            source: e,
        })?;
    tmp.persist(path).map_err(|e| ConfigError::Io {
        path: path.to_owned(),
        source: e.error,
    })?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn cfg(paths: &[&Path]) -> Configuration {
        Configuration::load(paths.iter().map(|p| p.to_path_buf())).expect("load")
    }

    #[test]
    fn load_with_no_files_yields_empty_config() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("missing.toml");
        let c = cfg(&[&p]);
        assert!(c.get_str("foo.bar").unwrap().is_none());
        assert_eq!(c.existing().len(), 0);
        assert_eq!(c.layer_paths().len(), 1);
    }

    #[test]
    fn set_creates_parent_dirs_and_writes_atomically() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("a/b/c/config.toml");
        let mut c = cfg(&[&p]);
        c.set("manifest.path", ConfigValue::String("zephyr".into()), &p)
            .unwrap();

        assert!(p.exists());
        let written = fs::read_to_string(&p).unwrap();
        assert!(written.contains("[manifest]"));
        assert!(written.contains(r#"path = "zephyr""#));
        assert_eq!(c.existing(), vec![p.as_path()]);
    }

    #[test]
    fn precedence_later_layer_wins() {
        let tmp = TempDir::new().unwrap();
        let p1 = tmp.path().join("a.toml");
        let p2 = tmp.path().join("b.toml");
        let p3 = tmp.path().join("c.toml");
        let mut c = cfg(&[&p1, &p2, &p3]);

        c.set("k.v", ConfigValue::String("low".into()), &p1)
            .unwrap();
        c.set("k.v", ConfigValue::String("mid".into()), &p2)
            .unwrap();
        c.set("k.v", ConfigValue::String("high".into()), &p3)
            .unwrap();

        assert_eq!(c.get_str("k.v").unwrap().as_deref(), Some("high"));
    }

    #[test]
    fn get_str_in_returns_only_that_layers_value() {
        let tmp = TempDir::new().unwrap();
        let p1 = tmp.path().join("a.toml");
        let p2 = tmp.path().join("b.toml");
        let mut c = cfg(&[&p1, &p2]);

        c.set("k.v", ConfigValue::String("low".into()), &p1)
            .unwrap();
        c.set("k.v", ConfigValue::String("high".into()), &p2)
            .unwrap();

        assert_eq!(c.get_str_in("k.v", &p1).unwrap().as_deref(), Some("low"));
        assert_eq!(c.get_str_in("k.v", &p2).unwrap().as_deref(), Some("high"));
    }

    #[test]
    fn get_bool_native_toml_value() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        fs::write(&p, "[update]\nnarrow = true\n").unwrap();
        let c = cfg(&[&p]);
        assert_eq!(c.get_bool("update.narrow").unwrap(), Some(true));
    }

    #[test]
    fn get_bool_on_string_returns_type_mismatch() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        fs::write(&p, "[s]\nv = \"true\"\n").unwrap();
        let c = cfg(&[&p]);
        assert!(matches!(
            c.get_bool("s.v"),
            Err(ConfigError::TypeMismatch { .. })
        ));
    }

    #[test]
    fn get_i64_native_only() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        fs::write(&p, "[n]\nint = 42\nas_str = \"7\"\n").unwrap();
        let c = cfg(&[&p]);
        assert_eq!(c.get_i64("n.int").unwrap(), Some(42));
        assert!(matches!(
            c.get_i64("n.as_str"),
            Err(ConfigError::TypeMismatch { .. })
        ));
    }

    #[test]
    fn get_f64_native_only_no_int_upcoercion() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        fs::write(&p, "[n]\nfloat = 1.5\nint = 42\nas_str = \"2.5\"\n").unwrap();
        let c = cfg(&[&p]);
        assert_eq!(c.get_f64("n.float").unwrap(), Some(1.5));
        assert!(matches!(
            c.get_f64("n.int"),
            Err(ConfigError::TypeMismatch { .. })
        ));
        assert!(matches!(
            c.get_f64("n.as_str"),
            Err(ConfigError::TypeMismatch { .. })
        ));
    }

    #[test]
    fn get_str_on_bool_returns_type_mismatch() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        fs::write(&p, "[s]\nv = true\n").unwrap();
        let c = cfg(&[&p]);
        assert!(matches!(
            c.get_str("s.v"),
            Err(ConfigError::TypeMismatch { .. })
        ));
    }

    #[test]
    fn get_returns_raw_config_value() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        fs::write(
            &p,
            "[s]\ns = \"x\"\nb = true\ni = 42\nf = 1.5\nl = [\"a\", \"b\"]\n",
        )
        .unwrap();
        let c = cfg(&[&p]);
        assert_eq!(c.get("s.s").unwrap(), Some(ConfigValue::String("x".into())));
        assert_eq!(c.get("s.b").unwrap(), Some(ConfigValue::Bool(true)));
        assert_eq!(c.get("s.i").unwrap(), Some(ConfigValue::Integer(42)));
        assert_eq!(c.get("s.f").unwrap(), Some(ConfigValue::Float(1.5)));
        assert_eq!(
            c.get("s.l").unwrap(),
            Some(ConfigValue::list_of_strings(["a", "b"]))
        );
        assert!(c.get("s.missing").unwrap().is_none());
    }

    #[test]
    fn with_inline_overrides_file_value() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        fs::write(&p, "[s]\nv = \"file\"\n").unwrap();

        let inline_doc: DocumentMut = "[s]\nv = \"inline\"\n".parse().unwrap();
        let c = cfg(&[&p]).with_inline(inline_doc);
        assert_eq!(c.get_str("s.v").unwrap().as_deref(), Some("inline"));
    }

    #[test]
    fn with_inline_does_not_affect_writes() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        let inline_doc: DocumentMut = "[s]\nv = \"inline\"\n".parse().unwrap();
        let mut c = cfg(&[&p]).with_inline(inline_doc);

        c.set("s.v", ConfigValue::String("written".into()), &p)
            .unwrap();

        // The inline override still wins on read.
        assert_eq!(c.get_str("s.v").unwrap().as_deref(), Some("inline"));
        // The file got the new value, not the inline.
        let on_disk = fs::read_to_string(&p).unwrap();
        assert!(on_disk.contains(r#"v = "written""#));
        assert!(!on_disk.contains("inline"));
    }

    #[test]
    fn with_inline_appears_in_items_at_top_precedence() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        fs::write(&p, "[s]\nshared = \"file\"\nfile_only = \"f\"\n").unwrap();
        let inline_doc: DocumentMut = "[s]\nshared = \"inline\"\ninline_only = \"i\"\n"
            .parse()
            .unwrap();
        let c = cfg(&[&p]).with_inline(inline_doc);
        let map: std::collections::HashMap<_, _> = c.items().into_iter().collect();
        assert_eq!(
            map.get("s.shared"),
            Some(&ConfigValue::String("inline".into()))
        );
        assert_eq!(
            map.get("s.file_only"),
            Some(&ConfigValue::String("f".into()))
        );
        assert_eq!(
            map.get("s.inline_only"),
            Some(&ConfigValue::String("i".into()))
        );
    }

    #[test]
    fn parse_value_native_types() {
        assert_eq!(ConfigValue::parse("42").unwrap(), ConfigValue::Integer(42));
        assert_eq!(ConfigValue::parse("-7").unwrap(), ConfigValue::Integer(-7));
        assert_eq!(ConfigValue::parse("1.5").unwrap(), ConfigValue::Float(1.5));
        assert_eq!(ConfigValue::parse("true").unwrap(), ConfigValue::Bool(true));
        assert_eq!(
            ConfigValue::parse("false").unwrap(),
            ConfigValue::Bool(false)
        );
    }

    #[test]
    fn parse_value_quoted_string() {
        assert_eq!(
            ConfigValue::parse(r#""42""#).unwrap(),
            ConfigValue::String("42".into())
        );
        assert_eq!(
            ConfigValue::parse(r#""hello""#).unwrap(),
            ConfigValue::String("hello".into())
        );
    }

    #[test]
    fn parse_value_bare_string_fallback() {
        assert_eq!(
            ConfigValue::parse("zephyr").unwrap(),
            ConfigValue::String("zephyr".into())
        );
        assert_eq!(
            ConfigValue::parse("hello world").unwrap(),
            ConfigValue::String("hello world".into())
        );
        assert_eq!(
            ConfigValue::parse("path/to/thing").unwrap(),
            ConfigValue::String("path/to/thing".into())
        );
        assert_eq!(
            ConfigValue::parse("").unwrap(),
            ConfigValue::String("".into())
        );
    }

    #[test]
    fn parse_value_arrays() {
        assert_eq!(
            ConfigValue::parse(r#"["+foo","-bar"]"#).unwrap(),
            ConfigValue::list_of_strings(["+foo", "-bar"])
        );
        assert_eq!(
            ConfigValue::parse("[1,2,3]").unwrap(),
            ConfigValue::List(vec![
                ConfigValue::Integer(1),
                ConfigValue::Integer(2),
                ConfigValue::Integer(3),
            ])
        );
        assert_eq!(ConfigValue::parse("[]").unwrap(), ConfigValue::List(vec![]));
    }

    #[test]
    fn parse_value_rejects_ambiguous_toml_constructs() {
        assert!(matches!(
            ConfigValue::parse("[bad"),
            Err(ConfigError::InvalidValue { .. })
        ));
        assert!(matches!(
            ConfigValue::parse(r#""mismatched"#),
            Err(ConfigError::InvalidValue { .. })
        ));
        assert!(matches!(
            ConfigValue::parse("{not-toml"),
            Err(ConfigError::InvalidValue { .. })
        ));
    }

    #[test]
    fn set_unknown_path_returns_unknown_layer() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("a.toml");
        let other = tmp.path().join("not-loaded.toml");
        let mut c = cfg(&[&p]);
        let err = c.set("k.v", ConfigValue::Bool(true), &other).unwrap_err();
        assert!(matches!(err, ConfigError::UnknownLayer(_)));
    }

    #[test]
    fn delete_topmost_walks_high_to_low() {
        let tmp = TempDir::new().unwrap();
        let p1 = tmp.path().join("a.toml");
        let p2 = tmp.path().join("b.toml");
        let mut c = cfg(&[&p1, &p2]);

        c.set("k.v", ConfigValue::String("low".into()), &p1)
            .unwrap();
        c.set("k.v", ConfigValue::String("high".into()), &p2)
            .unwrap();

        c.delete_topmost("k.v").unwrap();
        assert_eq!(c.get_str("k.v").unwrap().as_deref(), Some("low"));

        c.delete_topmost("k.v").unwrap();
        assert!(c.get_str("k.v").unwrap().is_none());

        assert!(matches!(
            c.delete_topmost("k.v"),
            Err(ConfigError::NotFound(_))
        ));
    }

    #[test]
    fn delete_empties_section_removes_section() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        let mut c = cfg(&[&p]);
        c.set("only.k", ConfigValue::Integer(1), &p).unwrap();
        c.delete("only.k", &p).unwrap();
        let written = fs::read_to_string(&p).unwrap();
        assert!(
            !written.contains("[only]"),
            "section should be gone: {written:?}"
        );
    }

    #[test]
    fn dotted_key_three_levels_round_trips_via_nested_tables() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        let mut c = cfg(&[&p]);
        c.set("foo.bar.baz", ConfigValue::String("v".into()), &p)
            .unwrap();
        let written = fs::read_to_string(&p).unwrap();
        // 3-level dotted keys serialize as nested TOML tables.
        assert!(written.contains("[foo.bar]"), "got: {written}");
        assert!(written.contains(r#"baz = "v""#), "got: {written}");

        // Round-trip on reload.
        let c2 = cfg(&[&p]);
        assert_eq!(c2.get_str("foo.bar.baz").unwrap().as_deref(), Some("v"));
    }

    #[test]
    fn nested_dotted_keys_share_intermediate_tables() {
        // Setting `tool.git.binary` and `tool.git.shallow` should produce a
        // single `[tool.git]` table with two values, not two separate ones.
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        let mut c = cfg(&[&p]);
        c.set(
            "tool.git.binary",
            ConfigValue::String("/usr/bin/git".into()),
            &p,
        )
        .unwrap();
        c.set("tool.git.shallow", ConfigValue::Bool(true), &p)
            .unwrap();

        let written = fs::read_to_string(&p).unwrap();
        // One [tool.git] table with two keys.
        let occurrences = written.matches("[tool.git]").count();
        assert_eq!(occurrences, 1, "got: {written}");
        assert_eq!(
            c.get_str("tool.git.binary").unwrap().as_deref(),
            Some("/usr/bin/git")
        );
        assert_eq!(c.get_bool("tool.git.shallow").unwrap(), Some(true));
    }

    #[test]
    fn nested_delete_cleans_up_empty_parent_tables() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        let mut c = cfg(&[&p]);
        c.set(
            "tool.git.binary",
            ConfigValue::String("/usr/bin/git".into()),
            &p,
        )
        .unwrap();
        c.delete("tool.git.binary", &p).unwrap();
        let written = fs::read_to_string(&p).unwrap();
        // Both `[tool.git]` and `[tool]` should be gone since both became empty.
        assert!(!written.contains("[tool"), "got: {written}");
    }

    #[test]
    fn items_merged_in_precedence_order() {
        let tmp = TempDir::new().unwrap();
        let p1 = tmp.path().join("a.toml");
        let p2 = tmp.path().join("b.toml");
        let mut c = cfg(&[&p1, &p2]);
        c.set("a.x", ConfigValue::Integer(1), &p1).unwrap();
        c.set("a.y", ConfigValue::Integer(2), &p1).unwrap();
        c.set("a.x", ConfigValue::Integer(99), &p2).unwrap();
        c.set("b.z", ConfigValue::Bool(true), &p2).unwrap();

        let items: Vec<_> = c.items();
        let map: std::collections::HashMap<_, _> = items.into_iter().collect();
        assert_eq!(map.get("a.x"), Some(&ConfigValue::Integer(99)));
        assert_eq!(map.get("a.y"), Some(&ConfigValue::Integer(2)));
        assert_eq!(map.get("b.z"), Some(&ConfigValue::Bool(true)));
    }

    #[test]
    fn malformed_toml_returns_error_with_path() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("bad.toml");
        fs::write(&p, "[unclosed\nno = good\n").unwrap();
        let err = Configuration::load([p.clone()]).unwrap_err();
        match err {
            ConfigError::MalformedToml { path, .. } => assert_eq!(path, p),
            other => panic!("expected MalformedToml, got {other:?}"),
        }
    }

    #[test]
    fn read_legacy_ini_file_fails_with_clear_error() {
        // Older configparser-style files use bare `key = value` (no quotes
        // around string values). That's not valid TOML — `value` parses
        // as a bare key/identifier — and we want a clean MalformedToml
        // diagnostic rather than a confusing partial-parse.
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("v1-config");
        fs::write(&p, "[manifest]\npath = zephyr\nfile = west.yml\n").unwrap();
        let err = Configuration::load([p.clone()]).unwrap_err();
        assert!(
            matches!(err, ConfigError::MalformedToml { ref path, .. } if path == &p),
            "expected MalformedToml carrying path, got {err:?}"
        );
    }

    #[test]
    fn invalid_keys_rejected() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        let mut c = cfg(&[&p]);
        assert!(matches!(
            c.set("nodot", ConfigValue::Bool(true), &p),
            Err(ConfigError::InvalidKey(_))
        ));
        assert!(matches!(
            c.set("foo.", ConfigValue::Bool(true), &p),
            Err(ConfigError::InvalidKey(_))
        ));
        assert!(matches!(
            c.set(".bar", ConfigValue::Bool(true), &p),
            Err(ConfigError::InvalidKey(_))
        ));
    }

    #[test]
    fn set_list_of_strings_round_trips() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        let mut c = cfg(&[&p]);
        c.set(
            "manifest.project-filter",
            ConfigValue::list_of_strings(["+foo", "-bar", "baz"]),
            &p,
        )
        .unwrap();

        let written = fs::read_to_string(&p).unwrap();
        assert!(
            written.contains(r#"project-filter = ["+foo", "-bar", "baz"]"#),
            "unexpected TOML: {written}"
        );

        let c2 = cfg(&[&p]);
        assert_eq!(
            c2.get_list_str("manifest.project-filter").unwrap(),
            Some(vec!["+foo".into(), "-bar".into(), "baz".into()])
        );
    }

    #[test]
    fn get_list_on_scalar_returns_type_mismatch() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        fs::write(&p, "[s]\nv = \"scalar\"\n").unwrap();
        let c = cfg(&[&p]);
        assert!(matches!(
            c.get_list("s.v"),
            Err(ConfigError::TypeMismatch { .. })
        ));
        assert!(matches!(
            c.get_list_str("s.v"),
            Err(ConfigError::TypeMismatch { .. })
        ));
    }

    #[test]
    fn get_str_on_list_returns_type_mismatch() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        fs::write(&p, "[s]\nv = [\"a\", \"b\"]\n").unwrap();
        let c = cfg(&[&p]);
        assert!(matches!(
            c.get_str("s.v"),
            Err(ConfigError::TypeMismatch { .. })
        ));
    }

    #[test]
    fn get_bool_on_list_returns_type_mismatch() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        fs::write(&p, "[s]\nv = [true, false]\n").unwrap();
        let c = cfg(&[&p]);
        assert!(matches!(
            c.get_bool("s.v"),
            Err(ConfigError::TypeMismatch { .. })
        ));
    }

    #[test]
    fn empty_list_round_trips() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        let mut c = cfg(&[&p]);
        c.set("s.v", ConfigValue::List(vec![]), &p).unwrap();

        let c2 = cfg(&[&p]);
        assert_eq!(c2.get_list("s.v").unwrap(), Some(vec![]));
        assert_eq!(c2.get_list_str("s.v").unwrap(), Some(vec![]));
    }

    #[test]
    fn mixed_type_list_round_trips_via_get_list() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        fs::write(&p, "[s]\nv = [1, \"two\", true]\n").unwrap();
        let c = cfg(&[&p]);
        assert_eq!(
            c.get_list("s.v").unwrap(),
            Some(vec![
                ConfigValue::Integer(1),
                ConfigValue::String("two".into()),
                ConfigValue::Bool(true),
            ])
        );
    }

    #[test]
    fn get_list_str_strict_rejects_mixed_types() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        fs::write(&p, "[s]\nv = [1, \"two\", true]\n").unwrap();
        let c = cfg(&[&p]);
        assert!(matches!(
            c.get_list_str("s.v"),
            Err(ConfigError::TypeMismatch { .. })
        ));
    }

    #[test]
    fn get_list_str_rejects_non_coercible_element() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        // Inline tables aren't representable as strings.
        fs::write(&p, "[s]\nv = [\"ok\", { a = 1 }]\n").unwrap();
        let c = cfg(&[&p]);
        assert!(matches!(
            c.get_list_str("s.v"),
            Err(ConfigError::TypeMismatch { .. })
        ));
    }

    #[test]
    fn items_preserves_list_variant() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        let mut c = cfg(&[&p]);
        c.set("s.v", ConfigValue::list_of_strings(["a", "b"]), &p)
            .unwrap();
        let items = c.items();
        let map: std::collections::HashMap<_, _> = items.into_iter().collect();
        match map.get("s.v") {
            Some(ConfigValue::List(items)) => {
                assert_eq!(items.len(), 2);
                assert_eq!(items[0], ConfigValue::String("a".into()));
                assert_eq!(items[1], ConfigValue::String("b".into()));
            }
            other => panic!("expected List variant, got {other:?}"),
        }
    }

    #[test]
    fn precedence_list_replaces_not_merges() {
        let tmp = TempDir::new().unwrap();
        let p1 = tmp.path().join("a.toml");
        let p2 = tmp.path().join("b.toml");
        let mut c = cfg(&[&p1, &p2]);

        c.set("k.v", ConfigValue::list_of_strings(["low-1", "low-2"]), &p1)
            .unwrap();
        c.set("k.v", ConfigValue::list_of_strings(["high"]), &p2)
            .unwrap();

        // Higher-precedence layer fully replaces the lower one — no element-wise merge.
        assert_eq!(c.get_list_str("k.v").unwrap(), Some(vec!["high".into()]));
    }
}
