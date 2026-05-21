//! Project selection for project-iterating commands (`update`, `list`,
//! eventually `forall`, `diff`, `status`). Composes `Manifest::is_active` and
//! `Manifest::resolve_projects` with one CLI rule on top: positional
//! project names bypass the active-group filter (matches Python's intent —
//! a user who names a project explicitly wants it even if its group is off).

use west_core::loaded::LoadedManifest;
use west_core::manifest::{
    GroupFilterEntry, Manifest, ManifestError, Project, Submodules,
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
    loaded: &'m LoadedManifest,
    selectors: &[S],
    cli_filter: &[GroupFilterEntry],
) -> Result<Vec<&'m Project>, ManifestError>
where
    S: AsRef<str>,
{
    if selectors.is_empty() {
        Ok(loaded
            .manifest
            .projects
            .iter()
            .filter(|p| loaded.is_active(p, cli_filter))
            .collect())
    } else {
        loaded
            .manifest
            .resolve_projects(selectors.iter().map(|s| s.as_ref()))
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
        userdata: manifest.self_.userdata.clone(),
    }
}

// `read_manifest_group_filter` was hoisted into
// `west_core::loaded::read_manifest_group_filter` so it can be shared by
// `LoadedManifest::from_manifest_and_config` and external callers. The
// re-export below preserves the historical import path for in-CLI uses
// (`super::select::read_manifest_group_filter`).
pub use west_core::loaded::read_manifest_group_filter;

#[cfg(test)]
mod tests {
    use super::*;
    use west_core::loaded::ProjectFilter;
    use west_core::manifest::Manifest;

    fn yaml(src: &str) -> Manifest {
        Manifest::from_yaml_str(src).unwrap()
    }

    fn loaded(src: &str) -> LoadedManifest {
        LoadedManifest::new(yaml(src), Vec::new(), ProjectFilter::empty())
    }

    #[test]
    fn empty_selectors_returns_active_projects() {
        let lm = loaded(
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
        let picked: Vec<&str> = select_projects(&lm, &[] as &[&str], &[])
            .unwrap()
            .iter()
            .map(|p| p.name.as_str())
            .collect();
        assert_eq!(picked, vec!["a"]);
    }

    #[test]
    fn positionals_bypass_group_filter() {
        let lm = loaded(
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
        let picked: Vec<&str> = select_projects(&lm, &["b"], &[])
            .unwrap()
            .iter()
            .map(|p| p.name.as_str())
            .collect();
        assert_eq!(picked, vec!["b"]);
    }

    #[test]
    fn unknown_positional_errors() {
        let lm = loaded(
            r#"
manifest:
  projects:
    - name: a
      url: https://x
"#,
        );
        let err = select_projects(&lm, &["nope"], &[]).unwrap_err();
        assert!(matches!(err, ManifestError::UnknownProject(s) if s == "nope"));
    }

    // `read_manifest_group_filter`'s tests live next to its definition
    // in `west_core::loaded`.
}
