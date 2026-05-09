//! Project selection for project-iterating commands (`update`, `list`,
//! eventually `forall`, `diff`, `status`). Composes `Manifest::is_active` and
//! `Manifest::resolve_projects` with one CLI rule on top: positional
//! project names bypass the active-group filter (matches Python's intent —
//! a user who names a project explicitly wants it even if its group is off).

use west_core::config::{ConfigValue, Configuration};
use west_core::manifest::{
    GroupFilterEntry, Manifest, ManifestError, Project, Submodules, parse_cli_group_filter,
};

/// Resolve the set of projects to update.
///
/// - empty `selectors` ⇒ every project that's active under the manifest's
///   group-filter combined with `cli_filter`.
/// - non-empty `selectors` ⇒ exactly those projects (matched by name then
///   by relative path), **bypassing** the active-group filter so a user
///   can update a named project even if it's in an inactive group.
///   Unknown selectors error.
pub fn select_projects<'m, S>(
    manifest: &'m Manifest,
    selectors: &[S],
    cli_filter: &[GroupFilterEntry],
) -> Result<Vec<&'m Project>, ManifestError>
where
    S: AsRef<str>,
{
    if selectors.is_empty() {
        Ok(manifest
            .projects
            .iter()
            .filter(|p| manifest.is_active(p, cli_filter))
            .collect())
    } else {
        manifest.resolve_projects(selectors.iter().map(|s| s.as_ref()))
    }
}

/// Build the synthetic project record for the manifest repo itself.
/// Mirrors python's `ManifestProject` (index 0 in `Manifest.projects`):
/// name `"manifest"` (a reserved name no real project can use),
/// revision `"HEAD"`, no url. Path is the manifest repo's `self.path`.
/// Used by `list`, `forall`, and other project-iterating commands that
/// need to emit / operate on the manifest repo as if it were a project.
pub(crate) fn synthetic_manifest_project(manifest: &Manifest) -> Project {
    Project {
        name: "manifest".into(),
        url: String::new(),
        revision: "HEAD".into(),
        path: manifest.self_.path.clone(),
        description: None,
        groups: Vec::new(),
        clone_depth: None,
        west_commands: manifest.self_.west_commands.clone(),
        remote_name: String::new(),
        submodules: Submodules::None,
    }
}

/// Read the `manifest.group-filter` workspace-config key and parse it
/// into a list of group-filter entries. Accepts either a string
/// (comma-separated, e.g. `+optional,-noisy`) or a list of strings;
/// returns an empty filter when the key is unset. Mirrors Python's
/// `_config_group_filter` — every command that gates on group activity
/// should compose this with its own CLI-supplied filter (if any).
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

#[cfg(test)]
mod tests {
    use super::*;
    use west_core::manifest::Manifest;

    fn yaml(src: &str) -> Manifest {
        Manifest::from_yaml_str(src).unwrap()
    }

    #[test]
    fn empty_selectors_returns_active_projects() {
        let m = yaml(
            r#"
manifest:
  group-filter: [-noisy]
  projects:
    - name: a
      url: https://x
    - name: b
      url: https://y
      groups: [noisy]
"#,
        );
        let picked: Vec<&str> = select_projects(&m, &[] as &[&str], &[])
            .unwrap()
            .iter()
            .map(|p| p.name.as_str())
            .collect();
        assert_eq!(picked, vec!["a"]);
    }

    #[test]
    fn positionals_bypass_group_filter() {
        let m = yaml(
            r#"
manifest:
  group-filter: [-noisy]
  projects:
    - name: a
      url: https://x
    - name: b
      url: https://y
      groups: [noisy]
"#,
        );
        let picked: Vec<&str> = select_projects(&m, &["b"], &[])
            .unwrap()
            .iter()
            .map(|p| p.name.as_str())
            .collect();
        assert_eq!(picked, vec!["b"]);
    }

    #[test]
    fn unknown_positional_errors() {
        let m = yaml(
            r#"
manifest:
  projects:
    - name: a
      url: https://x
"#,
        );
        let err = select_projects(&m, &["nope"], &[]).unwrap_err();
        assert!(matches!(err, ManifestError::UnknownProject(s) if s == "nope"));
    }
}
