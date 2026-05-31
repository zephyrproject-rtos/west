//! `west-commands` file parser, format-generic.
//!
//! A west-commands file lives inside a project (referenced from the
//! manifest's per-project `west-commands:` field or the manifest's
//! `self.west-commands:` field) and declares one or more python
//! extension commands the project ships. The same schema serialises
//! as YAML, TOML, or JSON; the [`Format`] enum (re-exported from
//! `west_core`) drives dispatch. Example, by convention written in
//! YAML — equivalent TOML / JSON encodings parse identically:
//!
//! ```yaml
//! west-commands:
//!   - file: scripts/west_commands/build.py
//!     commands:
//!       - name: build
//!         class: Build
//!         help: build a Zephyr application
//! ```
//!
//! The data layer just parses; discovery / dispatch / spawning is
//! a CLI concern and lives in `west-cli`.
//!
//! Schema is enforced by garde derives on the private `SchemaFile`
//! struct in this module — the python pykwalify schema this once
//! mirrored has been retired alongside the python wrapper rewrite.

use std::ffi::OsStr;
use std::fs;
use std::path::{Path, PathBuf};

use garde::Validate;
use serde::Deserialize;

use crate::Format;

/// A parsed `west-commands` file. The on-disk format (YAML / TOML /
/// JSON) doesn't survive parsing — the same shape comes out of every
/// parser.
#[derive(Debug, Clone, PartialEq)]
pub struct WestCommandsFile {
    /// All entries declared at top level under `west-commands:`.
    pub entries: Vec<WestCommandsEntry>,
}

/// One entry — one python file + the commands it implements.
#[derive(Debug, Clone, PartialEq)]
pub struct WestCommandsEntry {
    /// Path to the python file, relative to the *project* that owns
    /// the `west-commands` file. Resolution to an absolute path is
    /// the caller's concern.
    pub file: PathBuf,
    /// Commands declared in `file`.
    pub commands: Vec<WestCommand>,
}

/// One command declaration.
#[derive(Debug, Clone, PartialEq)]
pub struct WestCommand {
    /// Command name as the user types it (`west <name>`).
    pub name: String,
    /// Python class to instantiate. Defaults to `name` when absent
    /// in the input — matches python's `command_desc.get('class', name)`.
    pub class: String,
    /// One-line help string for `west help` / `--help` listings.
    pub help: Option<String>,
}

/// Errors produced by [`WestCommandsFile`] parsing.
#[derive(Debug, thiserror::Error)]
pub enum WestCommandsError {
    /// Body failed serde decoding. `format` says which parser ran;
    /// `source` is the underlying serde error, boxed because the
    /// three formats use unrelated concrete error types and no
    /// consumer in the tree needs to downcast (verified by grep at
    /// authoring time).
    #[error("{} parse error: {source}", format.name())]
    Parse {
        format: Format,
        #[source]
        source: Box<dyn std::error::Error + Send + Sync>,
    },
    #[error("unsupported west-commands format: {0:?} (expected .yaml/.yml/.toml/.json)")]
    UnsupportedFormat(String),
    #[error("validation failed: {0}")]
    Validation(String),
    #[error("io error on {}: {source}", path.display())]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
}

impl WestCommandsFile {
    /// Parse `body` interpreted as `format`. Use [`Self::from_path`]
    /// when reading from disk and you'd like the extension to pick
    /// the format for you; this entry is the format-known
    /// counterpart for in-memory tests and callers that get the
    /// format from elsewhere.
    pub fn from_str(format: Format, body: &str) -> Result<Self, WestCommandsError> {
        let file: SchemaFile = match format {
            Format::Yaml => serde_saphyr::from_str(body).map_err(|e| WestCommandsError::Parse {
                format,
                source: Box::new(e),
            })?,
            Format::Toml => toml_edit::de::from_str(body).map_err(|e| WestCommandsError::Parse {
                format,
                source: Box::new(e),
            })?,
            Format::Json => serde_json::from_str(body).map_err(|e| WestCommandsError::Parse {
                format,
                source: Box::new(e),
            })?,
        };
        Self::validate_and_resolve(file)
    }

    /// Parse a YAML body. Convenience wrapper around
    /// [`Self::from_str`] for callers who know the format at the
    /// call site.
    pub fn from_yaml_str(s: &str) -> Result<Self, WestCommandsError> {
        Self::from_str(Format::Yaml, s)
    }

    /// Parse a TOML body. Convenience wrapper around
    /// [`Self::from_str`].
    pub fn from_toml_str(s: &str) -> Result<Self, WestCommandsError> {
        Self::from_str(Format::Toml, s)
    }

    /// Parse a JSON body. Convenience wrapper around
    /// [`Self::from_str`].
    pub fn from_json_str(s: &str) -> Result<Self, WestCommandsError> {
        Self::from_str(Format::Json, s)
    }

    /// Read + parse a `west-commands` file from disk. The format is
    /// chosen by the file extension via [`Format::from_extension`]
    /// — same dispatch the manifest loader uses, so a workspace can
    /// pick the same authoring preference for both files.
    pub fn from_path(path: &Path) -> Result<Self, WestCommandsError> {
        let body = fs::read_to_string(path).map_err(|e| WestCommandsError::Io {
            path: path.to_owned(),
            source: e,
        })?;
        let ext = path.extension().and_then(OsStr::to_str);
        let format = Format::from_extension(ext).ok_or_else(|| {
            WestCommandsError::UnsupportedFormat(ext.unwrap_or("").to_owned())
        })?;
        Self::from_str(format, &body)
    }

    fn validate_and_resolve(file: SchemaFile) -> Result<Self, WestCommandsError> {
        file.validate()
            .map_err(|e| WestCommandsError::Validation(e.to_string()))?;
        Ok(file.resolve())
    }
}

// =====================================================================
// Schema types (private — serde + garde shapes)
// =====================================================================

#[derive(Debug, Clone, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
struct SchemaFile {
    #[garde(dive)]
    #[serde(rename = "west-commands")]
    west_commands: Vec<SchemaEntry>,
}

#[derive(Debug, Clone, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
struct SchemaEntry {
    #[garde(length(min = 1))]
    file: String,
    #[garde(dive, length(min = 1))]
    commands: Vec<SchemaCommand>,
}

#[derive(Debug, Clone, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
struct SchemaCommand {
    #[garde(length(min = 1))]
    name: String,
    #[garde(skip)]
    #[serde(default)]
    class: Option<String>,
    #[garde(skip)]
    #[serde(default)]
    help: Option<String>,
}

impl SchemaFile {
    fn resolve(self) -> WestCommandsFile {
        WestCommandsFile {
            entries: self
                .west_commands
                .into_iter()
                .map(|e| WestCommandsEntry {
                    file: PathBuf::from(e.file),
                    commands: e
                        .commands
                        .into_iter()
                        .map(|c| WestCommand {
                            class: c.class.clone().unwrap_or_else(|| c.name.clone()),
                            name: c.name,
                            help: c.help,
                        })
                        .collect(),
                })
                .collect(),
        }
    }
}

// =====================================================================
// Tests
// =====================================================================

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_single_entry() {
        let s = r#"
west-commands:
  - file: scripts/build.py
    commands:
      - name: build
        class: Build
        help: build a Zephyr application
"#;
        let f = WestCommandsFile::from_yaml_str(s).unwrap();
        assert_eq!(f.entries.len(), 1);
        let e = &f.entries[0];
        assert_eq!(e.file, PathBuf::from("scripts/build.py"));
        assert_eq!(e.commands.len(), 1);
        assert_eq!(e.commands[0].name, "build");
        assert_eq!(e.commands[0].class, "Build");
        assert_eq!(
            e.commands[0].help.as_deref(),
            Some("build a Zephyr application")
        );
    }

    #[test]
    fn class_defaults_to_name() {
        let s = r#"
west-commands:
  - file: scripts/foo.py
    commands:
      - name: foo
"#;
        let f = WestCommandsFile::from_yaml_str(s).unwrap();
        // class missing → class = name (matches python).
        assert_eq!(f.entries[0].commands[0].class, "foo");
        assert!(f.entries[0].commands[0].help.is_none());
    }

    #[test]
    fn multiple_files_each_with_commands() {
        let s = r#"
west-commands:
  - file: a.py
    commands:
      - name: a
        class: A
  - file: b.py
    commands:
      - name: b1
        class: B1
      - name: b2
        class: B2
"#;
        let f = WestCommandsFile::from_yaml_str(s).unwrap();
        assert_eq!(f.entries.len(), 2);
        assert_eq!(f.entries[0].file, PathBuf::from("a.py"));
        assert_eq!(f.entries[1].commands.len(), 2);
        let names: Vec<&str> = f.entries[1]
            .commands
            .iter()
            .map(|c| c.name.as_str())
            .collect();
        assert_eq!(names, vec!["b1", "b2"]);
    }

    #[test]
    fn rejects_unknown_top_level_key() {
        let s = r#"
west-commands:
  - file: x.py
    commands:
      - name: x
extra: nope
"#;
        let err = WestCommandsFile::from_yaml_str(s).unwrap_err();
        assert!(matches!(
            err,
            WestCommandsError::Parse {
                format: Format::Yaml,
                ..
            }
        ));
    }

    #[test]
    fn rejects_empty_commands_list() {
        let s = r#"
west-commands:
  - file: x.py
    commands: []
"#;
        let err = WestCommandsFile::from_yaml_str(s).unwrap_err();
        assert!(matches!(err, WestCommandsError::Validation(_)));
    }

    #[test]
    fn rejects_missing_name() {
        let s = r#"
west-commands:
  - file: x.py
    commands:
      - class: X
"#;
        let err = WestCommandsFile::from_yaml_str(s).unwrap_err();
        assert!(matches!(
            err,
            WestCommandsError::Parse {
                format: Format::Yaml,
                ..
            }
        ));
    }

    #[test]
    fn rejects_empty_file_string() {
        let s = r#"
west-commands:
  - file: ""
    commands:
      - name: x
"#;
        let err = WestCommandsFile::from_yaml_str(s).unwrap_err();
        assert!(matches!(err, WestCommandsError::Validation(_)));
    }

    #[test]
    fn parses_real_zephyr_shape() {
        // The shape from zephyr/scripts/west-commands.yml — every
        // command has name + class + help.
        let s = r#"
west-commands:
  - file: scripts/west_commands/completion.py
    commands:
      - name: completion
        class: Completion
        help: output shell completion scripts
  - file: scripts/west_commands/boards.py
    commands:
      - name: boards
        class: Boards
        help: display information about supported boards
"#;
        let f = WestCommandsFile::from_yaml_str(s).unwrap();
        assert_eq!(f.entries.len(), 2);
        assert_eq!(f.entries[0].commands[0].name, "completion");
        assert_eq!(f.entries[1].commands[0].name, "boards");
    }

    #[test]
    fn parses_toml_shape() {
        let s = r#"
[[west-commands]]
file = "scripts/build.py"

[[west-commands.commands]]
name = "build"
class = "Build"
help = "build a Zephyr application"
"#;
        let f = WestCommandsFile::from_toml_str(s).unwrap();
        assert_eq!(f.entries.len(), 1);
        assert_eq!(f.entries[0].file, PathBuf::from("scripts/build.py"));
        assert_eq!(f.entries[0].commands[0].name, "build");
        assert_eq!(f.entries[0].commands[0].class, "Build");
    }

    #[test]
    fn parses_json_shape() {
        let s = r#"{
  "west-commands": [
    {
      "file": "scripts/build.py",
      "commands": [
        { "name": "build", "class": "Build" }
      ]
    }
  ]
}"#;
        let f = WestCommandsFile::from_json_str(s).unwrap();
        assert_eq!(f.entries.len(), 1);
        assert_eq!(f.entries[0].commands[0].name, "build");
        assert_eq!(f.entries[0].commands[0].class, "Build");
    }

    #[test]
    fn from_path_dispatches_on_extension() {
        let tmp = tempfile::TempDir::new().unwrap();

        // YAML
        let yaml = tmp.path().join("wc.yaml");
        std::fs::write(
            &yaml,
            r#"west-commands:
  - file: a.py
    commands:
      - name: a
"#,
        )
        .unwrap();
        let f = WestCommandsFile::from_path(&yaml).unwrap();
        assert_eq!(f.entries[0].commands[0].name, "a");

        // TOML
        let toml = tmp.path().join("wc.toml");
        std::fs::write(
            &toml,
            r#"[[west-commands]]
file = "b.py"
[[west-commands.commands]]
name = "b"
"#,
        )
        .unwrap();
        let f = WestCommandsFile::from_path(&toml).unwrap();
        assert_eq!(f.entries[0].commands[0].name, "b");

        // JSON
        let json = tmp.path().join("wc.json");
        std::fs::write(
            &json,
            r#"{"west-commands": [{"file": "c.py", "commands": [{"name": "c"}]}]}"#,
        )
        .unwrap();
        let f = WestCommandsFile::from_path(&json).unwrap();
        assert_eq!(f.entries[0].commands[0].name, "c");
    }

    #[test]
    fn schema_is_format_equivalent() {
        // The same logical shape must come out of all three parsers.
        // If a future change adds a serde-level skew between formats
        // (extra fields, rename casing, default handling), this test
        // is the first thing that breaks.
        let yaml = r#"
west-commands:
  - file: scripts/build.py
    commands:
      - name: build
        class: Build
        help: build a Zephyr application
"#;
        let toml = r#"
[[west-commands]]
file = "scripts/build.py"

[[west-commands.commands]]
name = "build"
class = "Build"
help = "build a Zephyr application"
"#;
        let json = r#"{
  "west-commands": [
    {
      "file": "scripts/build.py",
      "commands": [
        {"name": "build", "class": "Build", "help": "build a Zephyr application"}
      ]
    }
  ]
}"#;
        let y = WestCommandsFile::from_str(Format::Yaml, yaml).unwrap();
        let t = WestCommandsFile::from_str(Format::Toml, toml).unwrap();
        let j = WestCommandsFile::from_str(Format::Json, json).unwrap();
        assert_eq!(y, t);
        assert_eq!(t, j);
    }

    #[test]
    fn from_path_rejects_unsupported_extension() {
        let tmp = tempfile::TempDir::new().unwrap();
        let p = tmp.path().join("wc.xml");
        std::fs::write(&p, "<xml/>").unwrap();
        let err = WestCommandsFile::from_path(&p).unwrap_err();
        assert!(matches!(err, WestCommandsError::UnsupportedFormat(s) if s == "xml"));
    }
}
