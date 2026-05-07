//! Project selection for `west update`. Thin wrapper around
//! `Manifest::resolve_projects` and `Manifest::is_active` plus the
//! `--group-filter` CLI parser. Lives in the update command rather than
//! `west_core::manifest` because it composes those data-layer methods
//! into the command's specific selection rules.

use west_core::manifest::{GroupFilterEntry, Manifest, ManifestError, Project};

/// Resolve the set of projects to update, applying Python's selection
/// semantics:
/// - empty `selectors` ⇒ every project that's active under the manifest's
///   group-filter combined with `cli_filter`.
/// - non-empty `selectors` ⇒ exactly those projects (matched by name then
///   by relative path), **bypassing** the active-group filter — Python
///   intentionally lets you update a project even if it's in an inactive
///   group. Unknown selectors error.
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
