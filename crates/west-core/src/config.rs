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
}

impl ConfigValue {
    fn into_toml_value(self) -> Value {
        match self {
            ConfigValue::String(s) => Value::from(s),
            ConfigValue::Bool(b) => Value::from(b),
            ConfigValue::Integer(i) => Value::from(i),
            ConfigValue::Float(f) => Value::from(f),
        }
    }

    fn from_toml_value(v: &Value) -> Option<Self> {
        match v {
            Value::String(s) => Some(ConfigValue::String(s.value().clone())),
            Value::Boolean(b) => Some(ConfigValue::Bool(*b.value())),
            Value::Integer(i) => Some(ConfigValue::Integer(*i.value())),
            Value::Float(f) => Some(ConfigValue::Float(*f.value())),
            _ => None,
        }
    }
}

#[derive(Debug)]
pub enum ConfigError {
    InvalidKey(String),
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
        Ok(Configuration { layers })
    }

    pub fn get_str(&self, option: &str) -> Option<String> {
        let (section, key) = parse_key(option).ok()?;
        for layer in self.layers.iter().rev() {
            if let Some(v) = lookup(&layer.doc, section, key) {
                return value_as_string(v);
            }
        }
        None
    }

    pub fn get_bool(&self, option: &str) -> Result<Option<bool>, ConfigError> {
        let (section, key) = parse_key(option)?;
        for layer in self.layers.iter().rev() {
            if let Some(v) = lookup(&layer.doc, section, key) {
                return value_as_bool(v)
                    .map(Some)
                    .ok_or_else(|| ConfigError::TypeMismatch {
                        option: option.to_owned(),
                        expected: "bool",
                    });
            }
        }
        Ok(None)
    }

    pub fn get_i64(&self, option: &str) -> Result<Option<i64>, ConfigError> {
        let (section, key) = parse_key(option)?;
        for layer in self.layers.iter().rev() {
            if let Some(v) = lookup(&layer.doc, section, key) {
                return value_as_i64(v)
                    .map(Some)
                    .ok_or_else(|| ConfigError::TypeMismatch {
                        option: option.to_owned(),
                        expected: "integer",
                    });
            }
        }
        Ok(None)
    }

    pub fn get_f64(&self, option: &str) -> Result<Option<f64>, ConfigError> {
        let (section, key) = parse_key(option)?;
        for layer in self.layers.iter().rev() {
            if let Some(v) = lookup(&layer.doc, section, key) {
                return value_as_f64(v)
                    .map(Some)
                    .ok_or_else(|| ConfigError::TypeMismatch {
                        option: option.to_owned(),
                        expected: "float",
                    });
            }
        }
        Ok(None)
    }

    pub fn get_str_in(&self, option: &str, layer: &Path) -> Result<Option<String>, ConfigError> {
        let (section, key) = parse_key(option)?;
        let idx = self.find_layer(layer)?;
        Ok(lookup(&self.layers[idx].doc, section, key).and_then(value_as_string))
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

    pub fn set(
        &mut self,
        option: &str,
        value: ConfigValue,
        layer: &Path,
    ) -> Result<(), ConfigError> {
        let (section, key) = parse_key(option)?;
        let idx = self.find_layer(layer)?;
        let l = &mut self.layers[idx];

        let section_item = l
            .doc
            .entry(section)
            .or_insert_with(|| Item::Table(Table::new()));
        let table = section_item
            .as_table_mut()
            .ok_or_else(|| ConfigError::TypeMismatch {
                option: option.to_owned(),
                expected: "table",
            })?;
        table[key] = Item::Value(value.into_toml_value());

        write_atomic(&l.path, &l.doc)?;
        l.exists = true;
        Ok(())
    }

    pub fn delete(&mut self, option: &str, layer: &Path) -> Result<(), ConfigError> {
        let (section, key) = parse_key(option)?;
        let idx = self.find_layer(layer)?;
        let l = &mut self.layers[idx];

        let section_item = l
            .doc
            .get_mut(section)
            .ok_or_else(|| ConfigError::NotFound(option.to_owned()))?;
        let table = section_item
            .as_table_mut()
            .ok_or_else(|| ConfigError::NotFound(option.to_owned()))?;

        if table.remove(key).is_none() {
            return Err(ConfigError::NotFound(option.to_owned()));
        }

        if table.is_empty() {
            l.doc.remove(section);
        }

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
    /// override lower-precedence ones.
    pub fn items(&self) -> Vec<(String, ConfigValue)> {
        let mut merged = BTreeMap::new();
        for layer in &self.layers {
            for (k, v) in collect_items(&layer.doc) {
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

fn lookup<'a>(doc: &'a DocumentMut, section: &str, key: &str) -> Option<&'a Value> {
    let section_item = doc.get(section)?;
    if let Some(table) = section_item.as_table() {
        return table.get(key).and_then(|i| i.as_value());
    }
    if let Some(inline) = section_item.as_inline_table() {
        return inline.get(key);
    }
    None
}

fn value_as_string(v: &Value) -> Option<String> {
    match v {
        Value::String(s) => Some(s.value().clone()),
        Value::Boolean(b) => Some(b.value().to_string()),
        Value::Integer(i) => Some(i.value().to_string()),
        Value::Float(f) => Some(f.value().to_string()),
        _ => None,
    }
}

fn value_as_bool(v: &Value) -> Option<bool> {
    match v {
        Value::Boolean(b) => Some(*b.value()),
        Value::String(s) => match s.value().to_ascii_lowercase().as_str() {
            "1" | "yes" | "true" | "on" => Some(true),
            "0" | "no" | "false" | "off" => Some(false),
            _ => None,
        },
        _ => None,
    }
}

fn value_as_i64(v: &Value) -> Option<i64> {
    match v {
        Value::Integer(i) => Some(*i.value()),
        Value::String(s) => s.value().parse().ok(),
        _ => None,
    }
}

fn value_as_f64(v: &Value) -> Option<f64> {
    match v {
        Value::Float(f) => Some(*f.value()),
        Value::Integer(i) => Some(*i.value() as f64),
        Value::String(s) => s.value().parse().ok(),
        _ => None,
    }
}

fn collect_items(doc: &DocumentMut) -> Vec<(String, ConfigValue)> {
    let mut out = Vec::new();
    for (section, item) in doc.iter() {
        if let Some(table) = item.as_table() {
            for (key, value_item) in table.iter() {
                if let Some(v) = value_item.as_value() {
                    if let Some(cv) = ConfigValue::from_toml_value(v) {
                        out.push((format!("{section}.{key}"), cv));
                    }
                }
            }
        } else if let Some(inline) = item.as_inline_table() {
            for (key, v) in inline.iter() {
                if let Some(cv) = ConfigValue::from_toml_value(v) {
                    out.push((format!("{section}.{key}"), cv));
                }
            }
        }
    }
    out
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
        assert!(c.get_str("foo.bar").is_none());
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

        assert_eq!(c.get_str("k.v").as_deref(), Some("high"));
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
    fn get_bool_string_coercion() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        fs::write(
            &p,
            "[s]\na = \"yes\"\nb = \"1\"\nc = \"FALSE\"\nd = \"off\"\nbad = \"maybe\"\n",
        )
        .unwrap();
        let c = cfg(&[&p]);
        assert_eq!(c.get_bool("s.a").unwrap(), Some(true));
        assert_eq!(c.get_bool("s.b").unwrap(), Some(true));
        assert_eq!(c.get_bool("s.c").unwrap(), Some(false));
        assert_eq!(c.get_bool("s.d").unwrap(), Some(false));
        assert!(matches!(
            c.get_bool("s.bad"),
            Err(ConfigError::TypeMismatch { .. })
        ));
    }

    #[test]
    fn get_i64_and_get_f64_native_and_coerce() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        fs::write(
            &p,
            "[n]\nint = 42\nfloat = 1.5\nint_str = \"7\"\nfloat_str = \"2.5\"\n",
        )
        .unwrap();
        let c = cfg(&[&p]);
        assert_eq!(c.get_i64("n.int").unwrap(), Some(42));
        assert_eq!(c.get_i64("n.int_str").unwrap(), Some(7));
        assert_eq!(c.get_f64("n.float").unwrap(), Some(1.5));
        assert_eq!(c.get_f64("n.int").unwrap(), Some(42.0));
        assert_eq!(c.get_f64("n.float_str").unwrap(), Some(2.5));
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
        assert_eq!(c.get_str("k.v").as_deref(), Some("low"));

        c.delete_topmost("k.v").unwrap();
        assert!(c.get_str("k.v").is_none());

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
    fn dotted_key_three_levels_round_trips() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        let mut c = cfg(&[&p]);
        c.set("foo.bar.baz", ConfigValue::String("v".into()), &p)
            .unwrap();
        let written = fs::read_to_string(&p).unwrap();
        assert!(written.contains("[foo]"), "got: {written}");
        assert!(written.contains(r#""bar.baz" = "v""#), "got: {written}");

        // Round-trip on reload
        let c2 = cfg(&[&p]);
        assert_eq!(c2.get_str("foo.bar.baz").as_deref(), Some("v"));
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
    fn read_old_python_ini_file_fails_with_clear_error() {
        // Python's configparser writes `key = value` (no quotes around string values).
        // That's not valid TOML — `value` is parsed as a bare key/identifier.
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
}
