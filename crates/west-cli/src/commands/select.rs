//! Project selection for project-iterating commands (`update`, `list`,
//! eventually `forall`, `diff`, `status`). Composes `Manifest::is_active` and
//! `Manifest::resolve_projects` with one CLI rule on top: positional
//! project names bypass the active-group filter (matches Python's intent —
//! a user who names a project explicitly wants it even if its group is off).

use west_core::manifest::{GroupFilterEntry, Manifest, ManifestError, Project};

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
