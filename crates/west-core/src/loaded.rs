//! Workspace-aware view of a loaded manifest.
//!
//! [`Manifest`] is the data layer — it carries everything that
//! came out of the YAML/TOML/JSON and nothing else. Per-workspace
//! state (the `manifest.group-filter` and `manifest.project-filter`
//! configuration options) is encoded here, in [`LoadedManifest`].
//!
//! Commands that gate behavior on "is this project active?" must
//! consult [`LoadedManifest::is_active`] rather than
//! [`Manifest::is_active`] — the latter only knows about
//! manifest-embedded group filters, not the workspace-config
//! project filter. Routing through this type makes that contract a
//! type error: there is no way to ask "active?" without the
//! project-filter being considered.
//!
//! This mirrors west v1's `Manifest.is_active`, which applied
//! `manifest.project-filter` first and fell through to group
//! filtering only on no-match.
//!
//! The parsed [`ProjectFilterEntry`]s validate `manifest.project-filter`
//! against the legacy contract:
//!   - Each entry starts with `+` or `-`.
//!   - The remainder is a syntactically valid regular expression.
//!   - At evaluation time the entries are walked in order against
//!     the project name (`re.fullmatch`-style), and the *last*
//!     match's sign decides the project's activity state. Entries
//!     that don't match leave the decision to the group filter.
//!
//! Configuration accepts the option as either a native TOML array
//! of strings or a legacy comma-separated string; both shapes flow
//! through [`ProjectFilter::from_config`] identically.

use std::sync::Arc;

use regex::Regex;

use crate::config::{ConfigError, ConfigValue, Configuration};
use crate::manifest::{GroupFilterEntry, Manifest, Project, parse_cli_group_filter};

/// One `+regex` / `-regex` element of `manifest.project-filter`.
#[derive(Debug, Clone)]
pub struct ProjectFilterEntry {
    /// Pre-compiled name pattern. Matched with `fullmatch` semantics
    /// at evaluation time.
    pub pattern: Arc<Regex>,
    /// `true` for `+regex` (forces active), `false` for `-regex`
    /// (forces inactive).
    pub make_active: bool,
}

/// Workspace-derived project-filter, parsed once at load time.
#[derive(Debug, Clone, Default)]
pub struct ProjectFilter {
    entries: Vec<ProjectFilterEntry>,
}

impl ProjectFilter {
    /// Empty filter — every project is left to the group-filter
    /// decision.
    pub fn empty() -> Self {
        Self::default()
    }

    pub fn entries(&self) -> &[ProjectFilterEntry] {
        &self.entries
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    /// Parse `manifest.project-filter` from `config`. Accepts either
    /// a native TOML array of strings or a legacy comma-separated
    /// string. Returns [`ProjectFilter::empty`] when the option is
    /// unset.
    pub fn from_config(config: &Configuration) -> Result<Self, ProjectFilterError> {
        // Prefer the array form. Fall back to a CSV string only if
        // the option is stored as a single string scalar. The
        // diagnostic shape preserves the user's original CSV string
        // (whitespace and all) when stored as a string, since
        // legacy callers test error messages by substring against
        // the raw form they passed in.
        let (raw_entries, raw_for_msg): (Vec<String>, String) =
            match config.get_list_str("manifest.project-filter") {
                Ok(Some(list)) => {
                    let display = list.join(",");
                    (list, display)
                }
                Ok(None) => return Ok(Self::empty()),
                Err(ConfigError::TypeMismatch { .. }) => match config
                    .get_str("manifest.project-filter")
                    .map_err(ProjectFilterError::Config)?
                {
                    Some(csv) => {
                        let entries: Vec<String> = csv
                            .split(',')
                            .map(|s| s.trim().to_owned())
                            .filter(|s| !s.is_empty())
                            .collect();
                        (entries, csv)
                    }
                    None => return Ok(Self::empty()),
                },
                Err(e) => return Err(ProjectFilterError::Config(e)),
            };
        let mut entries = Vec::with_capacity(raw_entries.len());
        for element in raw_entries {
            if !element.starts_with('+') && !element.starts_with('-') {
                return Err(ProjectFilterError::BadEntry {
                    raw: raw_for_msg,
                    reason: format!("element {element:?} does not start with \"+\" or \"-\""),
                });
            }
            let make_active = element.starts_with('+');
            let pattern_str = &element[1..];
            if pattern_str.is_empty() {
                return Err(ProjectFilterError::BadEntry {
                    raw: raw_for_msg,
                    reason: "a bare \"+\" or \"-\" contains no regular expression".to_owned(),
                });
            }
            let pattern = Regex::new(pattern_str).map_err(|e| ProjectFilterError::BadEntry {
                raw: raw_for_msg.clone(),
                reason: format!("invalid regular expression {pattern_str:?}: {e}"),
            })?;
            entries.push(ProjectFilterEntry {
                pattern: Arc::new(pattern),
                make_active,
            });
        }
        Ok(Self { entries })
    }

    /// Walk entries in order; the last `fullmatch` against `name`
    /// wins. Returns `Some(true)` for an explicit `+`, `Some(false)`
    /// for an explicit `-`, or `None` if no entry matched.
    pub fn decide(&self, name: &str) -> Option<bool> {
        let mut decision: Option<bool> = None;
        for entry in &self.entries {
            if is_fullmatch(&entry.pattern, name) {
                decision = Some(entry.make_active);
            }
        }
        decision
    }
}

/// Errors produced when parsing or validating `manifest.project-filter`.
#[derive(Debug, thiserror::Error)]
pub enum ProjectFilterError {
    /// A specific entry failed validation. The message format follows
    /// the legacy contract `invalid "manifest.project-filter" option
    /// value "<raw>": <reason>` once `Display`-ed.
    #[error("invalid \"manifest.project-filter\" option value \"{raw}\": {reason}")]
    BadEntry { raw: String, reason: String },
    /// Reading the option from `Configuration` itself failed (I/O, syntax).
    #[error(transparent)]
    Config(#[from] ConfigError),
}

/// Workspace-aware view of a parsed [`Manifest`].
///
/// Holds the manifest plus the workspace-config-derived filters
/// (`manifest.group-filter`, `manifest.project-filter`). All
/// "is this project active?" checks go through
/// [`LoadedManifest::is_active`], which composes both layers so the
/// project-filter cannot be silently bypassed.
#[derive(Debug, Clone)]
pub struct LoadedManifest {
    pub manifest: Manifest,
    /// Workspace-config `manifest.group-filter`, layered *under* any
    /// command-line group-filter passed at evaluation time.
    pub config_group_filter: Vec<GroupFilterEntry>,
    pub project_filter: ProjectFilter,
}

impl LoadedManifest {
    /// Wrap a `manifest` with explicit filter state. Prefer
    /// [`LoadedManifest::from_manifest_and_config`] when you have a
    /// workspace `Configuration` in hand — it pulls both filters from
    /// the canonical place once.
    pub fn new(
        manifest: Manifest,
        config_group_filter: Vec<GroupFilterEntry>,
        project_filter: ProjectFilter,
    ) -> Self {
        Self {
            manifest,
            config_group_filter,
            project_filter,
        }
    }

    /// Build from an already-parsed `manifest` plus a workspace
    /// `Configuration`. Reads `manifest.group-filter` and
    /// `manifest.project-filter` from `config`, validating each.
    pub fn from_manifest_and_config(
        manifest: Manifest,
        config: &Configuration,
    ) -> Result<Self, LoadError> {
        let config_group_filter =
            read_manifest_group_filter(config).map_err(LoadError::GroupFilter)?;
        let project_filter = ProjectFilter::from_config(config)?;
        Ok(Self::new(manifest, config_group_filter, project_filter))
    }

    /// `true` if `project` should be considered active in this
    /// workspace. Mirrors west v1's evaluation order:
    /// 1. If `manifest.project-filter` has an entry whose regex
    ///    `fullmatch`es the project name, the *last* such entry's
    ///    sign decides — `+` ⇒ active, `-` ⇒ inactive.
    /// 2. Otherwise fall through to group-filter evaluation, using
    ///    the manifest's embedded `group-filter:` composed with
    ///    [`Self::config_group_filter`] and any caller-supplied
    ///    `cli_filter`.
    pub fn is_active(&self, project: &Project, cli_filter: &[GroupFilterEntry]) -> bool {
        if let Some(decision) = self.project_filter.decide(&project.name) {
            return decision;
        }
        let mut extras: Vec<GroupFilterEntry> =
            Vec::with_capacity(self.config_group_filter.len() + cli_filter.len());
        extras.extend(self.config_group_filter.iter().cloned());
        extras.extend(cli_filter.iter().cloned());
        self.manifest.is_active(project, &extras)
    }
}

/// Read the `manifest.group-filter` workspace-config key as a list of
/// parsed [`GroupFilterEntry`] values. Accepts either a comma-separated
/// string (`"+optional,-noisy"`) or a TOML array of strings; the unset
/// option yields an empty list. The returned filter is meant to be
/// stored on [`LoadedManifest::config_group_filter`] and composed with
/// any caller-supplied CLI filter at evaluation time.
pub fn read_manifest_group_filter(config: &Configuration) -> Result<Vec<GroupFilterEntry>, String> {
    let raw: Vec<String> = match config
        .get("manifest.group-filter")
        .map_err(|e| e.to_string())?
    {
        None => return Ok(Vec::new()),
        Some(ConfigValue::String(s)) => vec![s],
        Some(ConfigValue::List(items)) => {
            let mut out = Vec::with_capacity(items.len());
            for item in items {
                match item {
                    ConfigValue::String(s) => out.push(s),
                    other => {
                        return Err(format!(
                            "manifest.group-filter entries must be strings, got {other:?}"
                        ));
                    }
                }
            }
            out
        }
        Some(other) => {
            return Err(format!(
                "manifest.group-filter must be a string or list, got {other:?}"
            ));
        }
    };
    parse_cli_group_filter(&raw).map_err(|e| e.to_string())
}

/// Aggregate error for [`LoadedManifest::from_manifest_and_config`].
#[derive(Debug, thiserror::Error)]
pub enum LoadError {
    #[error("{0}")]
    GroupFilter(String),
    #[error(transparent)]
    ProjectFilter(#[from] ProjectFilterError),
}

/// `Regex::is_match` is partial; project-filter wants `fullmatch` semantics.
/// Anchoring with `\A` / `\z` would alter the user's pattern; compare against
/// captured match bounds instead.
fn is_fullmatch(re: &Regex, name: &str) -> bool {
    match re.find(name) {
        Some(m) => m.start() == 0 && m.end() == name.len(),
        None => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Configuration;

    fn empty_config() -> Configuration {
        Configuration::load(Vec::<std::path::PathBuf>::new()).unwrap()
    }

    #[test]
    fn read_manifest_group_filter_unset_returns_empty() {
        let cfg = empty_config();
        assert!(read_manifest_group_filter(&cfg).unwrap().is_empty());
    }

    #[test]
    fn read_manifest_group_filter_comma_string() {
        let mut cfg = empty_config();
        cfg.set_inline(
            "manifest.group-filter",
            ConfigValue::String("+optional, -noisy".into()),
        )
        .unwrap();
        let parsed = read_manifest_group_filter(&cfg).unwrap();
        assert_eq!(parsed.len(), 2);
        assert_eq!(parsed[0].group, "optional");
        assert!(!parsed[0].disabled);
        assert_eq!(parsed[1].group, "noisy");
        assert!(parsed[1].disabled);
    }

    #[test]
    fn read_manifest_group_filter_list_of_strings() {
        let mut cfg = empty_config();
        cfg.set_inline(
            "manifest.group-filter",
            ConfigValue::List(vec![
                ConfigValue::String("+optional".into()),
                ConfigValue::String("-noisy".into()),
            ]),
        )
        .unwrap();
        let parsed = read_manifest_group_filter(&cfg).unwrap();
        assert_eq!(parsed.len(), 2);
        assert_eq!(parsed[0].group, "optional");
        assert!(!parsed[0].disabled);
        assert_eq!(parsed[1].group, "noisy");
        assert!(parsed[1].disabled);
    }

    #[test]
    fn read_manifest_group_filter_rejects_non_string_list_entry() {
        let mut cfg = empty_config();
        cfg.set_inline(
            "manifest.group-filter",
            ConfigValue::List(vec![
                ConfigValue::String("+optional".into()),
                ConfigValue::Integer(7),
            ]),
        )
        .unwrap();
        let err = read_manifest_group_filter(&cfg).unwrap_err();
        assert!(err.contains("entries must be strings"));
    }

    #[test]
    fn read_manifest_group_filter_rejects_scalar_non_string() {
        let mut cfg = empty_config();
        cfg.set_inline("manifest.group-filter", ConfigValue::Bool(true))
            .unwrap();
        let err = read_manifest_group_filter(&cfg).unwrap_err();
        assert!(err.contains("must be a string or list"));
    }

    #[test]
    fn project_filter_decide_last_match_wins() {
        // Build directly; `from_config` is exercised through the
        // workspace-loading tests in tests/.
        let pat = |s| Arc::new(Regex::new(s).unwrap());
        let pf = ProjectFilter {
            entries: vec![
                ProjectFilterEntry {
                    pattern: pat("foo"),
                    make_active: false,
                },
                ProjectFilterEntry {
                    pattern: pat("foo"),
                    make_active: true,
                },
            ],
        };
        assert_eq!(pf.decide("foo"), Some(true)); // last `+foo` wins
        assert_eq!(pf.decide("bar"), None);
    }

    #[test]
    fn project_filter_decide_uses_fullmatch() {
        let pat = |s| Arc::new(Regex::new(s).unwrap());
        let pf = ProjectFilter {
            entries: vec![ProjectFilterEntry {
                pattern: pat("foo"),
                make_active: false,
            }],
        };
        // "foobar" must NOT match "foo" — fullmatch semantics.
        assert_eq!(pf.decide("foobar"), None);
        assert_eq!(pf.decide("foo"), Some(false));
    }
}
