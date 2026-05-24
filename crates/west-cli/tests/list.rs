//! Integration tests for `west list`. Sandbox + bare-repo fixture
//! pattern mirroring `tests/update.rs`.

use std::path::{Path, PathBuf};
use std::process::Command;

use assert_cmd::Command as AssertCmd;
use serial_test::serial;
use tempfile::TempDir;

const BIN: &str = "west";

// ============================================================================
// Helpers
// ============================================================================

fn git_available() -> bool {
    Command::new("git")
        .arg("--version")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

fn git(args: &[&str], cwd: &Path) {
    let status = Command::new("git")
        .args(args)
        .current_dir(cwd)
        .env("GIT_AUTHOR_NAME", "test")
        .env("GIT_AUTHOR_EMAIL", "test@example.com")
        .env("GIT_COMMITTER_NAME", "test")
        .env("GIT_COMMITTER_EMAIL", "test@example.com")
        .status()
        .unwrap_or_else(|e| panic!("git {args:?}: {e}"));
    assert!(status.success(), "git {args:?} failed");
}

fn make_bare_with_one_commit(root: &Path, name: &str, content: &str) -> PathBuf {
    let work = root.join(format!("work-{name}"));
    std::fs::create_dir_all(&work).unwrap();
    git(&["init", "-q", "--initial-branch=main", "."], &work);
    std::fs::write(work.join("R"), content.as_bytes()).unwrap();
    git(&["add", "."], &work);
    git(&["commit", "-q", "-m", "initial"], &work);
    let bare = root.join(format!("{name}.git"));
    git(
        &[
            "clone",
            "-q",
            "--bare",
            work.to_str().unwrap(),
            bare.to_str().unwrap(),
        ],
        root,
    );
    let _ = std::fs::remove_dir_all(&work);
    bare
}

struct Sandbox {
    tmp: TempDir,
}

impl Sandbox {
    fn new() -> Self {
        Self {
            tmp: TempDir::new().unwrap(),
        }
    }
    fn root(&self) -> &Path {
        self.tmp.path()
    }
    fn west(&self) -> AssertCmd {
        let mut c = AssertCmd::cargo_bin(BIN).unwrap();
        c.env("WEST_CONFIG_GLOBAL", self.root().join("glob.toml"))
            .env("WEST_CONFIG_SYSTEM", self.root().join("sys.toml"))
            .env_remove("WEST_CONFIG_LOCAL")
            .env_remove("XDG_CONFIG_HOME");
        c
    }
}

/// Build a manifest YAML containing the named projects, optionally with
/// a `group-filter:` and per-project `groups:`.
fn manifest_yaml(group_filter: Option<&str>, projects: &[(&str, &Path, &[&str])]) -> String {
    let mut s = String::from("manifest:\n");
    if let Some(gf) = group_filter {
        s.push_str(&format!("  group-filter: [{gf}]\n"));
    }
    s.push_str("  self:\n    path: my-manifest\n  projects:\n");
    for (name, bare, groups) in projects {
        s.push_str(&format!(
            "    - name: {name}\n      url: {url}\n      revision: main\n",
            url = bare.display()
        ));
        if !groups.is_empty() {
            s.push_str("      groups:\n");
            for g in *groups {
                s.push_str(&format!("        - {g}\n"));
            }
        }
    }
    s
}

fn init_workspace(sb: &Sandbox, manifest_yaml: &str) -> PathBuf {
    let manifest_work = sb.root().join("manifest-work");
    std::fs::create_dir_all(&manifest_work).unwrap();
    git(
        &["init", "-q", "--initial-branch=main", "."],
        &manifest_work,
    );
    std::fs::write(manifest_work.join("west.yml"), manifest_yaml).unwrap();
    git(&["add", "."], &manifest_work);
    git(&["commit", "-q", "-m", "manifest"], &manifest_work);
    let bare = sb.root().join("manifest.git");
    git(
        &[
            "clone",
            "-q",
            "--bare",
            manifest_work.to_str().unwrap(),
            bare.to_str().unwrap(),
        ],
        sb.root(),
    );
    let _ = std::fs::remove_dir_all(&manifest_work);

    let workspace = sb.root().join("ws");
    sb.west()
        .args([
            "init",
            "--url",
            bare.to_str().unwrap(),
            workspace.to_str().unwrap(),
        ])
        .assert()
        .success();
    workspace
}

fn stdout_lines(out: &[u8]) -> Vec<String> {
    String::from_utf8_lossy(out)
        .lines()
        .map(|l| l.to_owned())
        .collect()
}

// ============================================================================
// Tests
// ============================================================================

#[test]
#[serial]
fn list_default_format_shows_active_projects() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let p2 = make_bare_with_one_commit(sb.root(), "p2", "p2");
    let manifest = manifest_yaml(Some("-noisy"), &[("p1", &p1, &[]), ("p2", &p2, &["noisy"])]);
    let ws = init_workspace(&sb, &manifest);

    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "list"])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let lines = stdout_lines(&out);
    // Synthetic manifest project (matching Python's index-0 `ManifestProject`)
    // sits at the top, followed by the real active project.
    assert_eq!(lines.len(), 2, "expected synthetic + p1, got: {lines:?}");
    assert!(lines[0].starts_with("manifest"), "got: {:?}", lines[0]);
    assert!(lines[1].starts_with("p1"), "got: {:?}", lines[1]);
}

#[test]
#[serial]
fn list_format_substitutes_keys() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let manifest = manifest_yaml(None, &[("p1", &p1, &[])]);
    let ws = init_workspace(&sb, &manifest);

    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "list",
            "-f",
            "{name}:{path}:{revision}",
        ])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let lines = stdout_lines(&out);
    assert_eq!(
        lines,
        vec![
            "manifest:my-manifest:HEAD".to_owned(),
            "p1:p1:main".to_owned(),
        ]
    );
}

#[test]
#[serial]
fn list_all_includes_inactive() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let p2 = make_bare_with_one_commit(sb.root(), "p2", "p2");
    let manifest = manifest_yaml(Some("-noisy"), &[("p1", &p1, &[]), ("p2", &p2, &["noisy"])]);
    let ws = init_workspace(&sb, &manifest);

    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "list", "-a", "-f", "{name}"])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let mut lines = stdout_lines(&out);
    lines.sort();
    assert_eq!(
        lines,
        vec!["manifest".to_owned(), "p1".to_owned(), "p2".to_owned()]
    );
}

#[test]
#[serial]
fn list_inactive_only_excludes_active() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let p2 = make_bare_with_one_commit(sb.root(), "p2", "p2");
    let manifest = manifest_yaml(Some("-noisy"), &[("p1", &p1, &[]), ("p2", &p2, &["noisy"])]);
    let ws = init_workspace(&sb, &manifest);

    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "list", "-i", "-f", "{name}"])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    assert_eq!(stdout_lines(&out), vec!["p2".to_owned()]);
}

#[test]
#[serial]
fn list_positional_filters_to_named_project() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let p2 = make_bare_with_one_commit(sb.root(), "p2", "p2");
    let manifest = manifest_yaml(None, &[("p1", &p1, &[]), ("p2", &p2, &[])]);
    let ws = init_workspace(&sb, &manifest);

    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "list", "-f", "{name}", "p1"])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    assert_eq!(stdout_lines(&out), vec!["p1".to_owned()]);
}

#[test]
#[serial]
fn list_positional_bypasses_active_filter() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let p2 = make_bare_with_one_commit(sb.root(), "p2", "p2");
    let manifest = manifest_yaml(Some("-noisy"), &[("p1", &p1, &[]), ("p2", &p2, &["noisy"])]);
    let ws = init_workspace(&sb, &manifest);

    // p2 is in an inactive group, so plain `west list` would omit it.
    // Naming it positionally bypasses the gate.
    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "list", "-f", "{name}", "p2"])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    assert_eq!(stdout_lines(&out), vec!["p2".to_owned()]);
}

#[test]
#[serial]
fn list_unknown_selector_errors() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let manifest = manifest_yaml(None, &[("p1", &p1, &[])]);
    let ws = init_workspace(&sb, &manifest);

    let assert = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "list", "nope"])
        .assert()
        .failure();
    let stderr = String::from_utf8_lossy(&assert.get_output().stderr).into_owned();
    assert!(stderr.contains("nope"), "got: {stderr}");
}

#[test]
#[serial]
fn list_inactive_with_positional_errors() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let manifest = manifest_yaml(None, &[("p1", &p1, &[])]);
    let ws = init_workspace(&sb, &manifest);

    let assert = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "list", "-i", "p1"])
        .assert()
        .failure();
    let stderr = String::from_utf8_lossy(&assert.get_output().stderr).into_owned();
    assert!(
        stderr.contains("-i cannot be combined with an explicit project list"),
        "got: {stderr}"
    );
}

#[test]
#[serial]
fn list_format_width_and_alignment() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let manifest = manifest_yaml(None, &[("p1", &p1, &[])]);
    let ws = init_workspace(&sb, &manifest);

    // {name:<8} pads `p1` to 8 chars with spaces; {path:>8} right-aligns.
    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "list",
            "-f",
            "{name:<8}|{path:>8}",
        ])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    assert_eq!(
        stdout_lines(&out),
        vec![
            "manifest|my-manifest".to_owned(),
            "p1      |      p1".to_owned(),
        ]
    );
}

#[test]
#[serial]
fn list_unknown_format_key_errors() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let manifest = manifest_yaml(None, &[("p1", &p1, &[])]);
    let ws = init_workspace(&sb, &manifest);

    let assert = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "list", "-f", "{bogus}"])
        .assert()
        .failure();
    let stderr = String::from_utf8_lossy(&assert.get_output().stderr).into_owned();
    assert!(stderr.contains("bogus"), "got: {stderr}");
}

#[test]
#[serial]
fn list_sha_for_cloned_project() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let manifest = manifest_yaml(None, &[("p1", &p1, &[])]);
    let ws = init_workspace(&sb, &manifest);

    // Clone p1 first so {sha} resolves.
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update", "-j", "1"])
        .assert()
        .success();

    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "list", "-f", "{sha}"])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let lines = stdout_lines(&out);
    // Two output lines: the synthetic manifest project renders as
    // "N/A" (it has no manifest-controlled revision), and p1's HEAD
    // resolves to a 40-char hex SHA.
    assert_eq!(lines.len(), 2);
    assert_eq!(lines[0], "N/A", "synthetic manifest sha should be N/A");
    assert_eq!(lines[1].len(), 40, "expected 40-char sha, got: {:?}", lines[1]);
    assert!(lines[1].chars().all(|c| c.is_ascii_hexdigit()));
}

#[test]
#[serial]
fn list_cloned_key_reflects_clone_state() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let manifest = manifest_yaml(None, &[("p1", &p1, &[])]);
    let ws = init_workspace(&sb, &manifest);

    // Before update: synthetic manifest is cloned (init created it);
    // p1 isn't yet.
    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "list", "-f", "{cloned}"])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    assert_eq!(
        stdout_lines(&out),
        vec!["cloned".to_owned(), "not-cloned".to_owned()]
    );

    // After update: both cloned.
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update", "-j", "1"])
        .assert()
        .success();
    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "list", "-f", "{cloned}"])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    assert_eq!(
        stdout_lines(&out),
        vec!["cloned".to_owned(), "cloned".to_owned()]
    );
}

#[test]
#[serial]
fn list_warns_and_exits_nonzero_when_per_project_import_is_uncloned() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let q = make_bare_with_one_commit(sb.root(), "q", "q");

    // Project P contains its own west.yml that pulls in Q. With
    // `import: true`, P's west.yml needs to be readable from disk —
    // which only happens after `west update`. We deliberately don't
    // run update here; we expect `west list` to print P, warn that
    // P's import was skipped, and exit non-zero.
    let p_yml = format!(
        "manifest:\n  projects:\n    - name: q\n      url: {}\n      revision: main\n",
        q.display()
    );
    let p = make_bare_with_files(sb.root(), "p", &[("README", "p\n"), ("west.yml", &p_yml)]);

    let manifest_yml = format!(
        "manifest:\n  self:\n    path: my-manifest\n  projects:\n    - name: p\n      url: {}\n      revision: main\n      import: true\n",
        p.display()
    );
    let manifest_work = sb.root().join("manifest-work");
    std::fs::create_dir_all(&manifest_work).unwrap();
    git(
        &["init", "-q", "--initial-branch=main", "."],
        &manifest_work,
    );
    std::fs::write(manifest_work.join("west.yml"), &manifest_yml).unwrap();
    git(&["add", "."], &manifest_work);
    git(&["commit", "-q", "-m", "manifest"], &manifest_work);
    let bare = sb.root().join("manifest.git");
    git(
        &[
            "clone",
            "-q",
            "--bare",
            manifest_work.to_str().unwrap(),
            bare.to_str().unwrap(),
        ],
        sb.root(),
    );
    let _ = std::fs::remove_dir_all(&manifest_work);
    let workspace = sb.root().join("ws");
    sb.west()
        .args([
            "init",
            "--url",
            bare.to_str().unwrap(),
            workspace.to_str().unwrap(),
        ])
        .assert()
        .success();

    // Don't run `west update` — P stays uncloned. `west list` should
    // produce P's line, warn about the skipped import, and exit non-zero.
    let assert = sb
        .west()
        .args(["-C", workspace.to_str().unwrap(), "list", "-f", "{name}"])
        .assert()
        .failure();
    let stdout = stdout_lines(&assert.get_output().stdout);
    assert_eq!(stdout, vec!["manifest".to_owned(), "p".to_owned()]);
    let stderr = String::from_utf8_lossy(&assert.get_output().stderr).into_owned();
    assert!(
        stderr.contains("skipped import") && stderr.contains("\"p\"") || stderr.contains(": p"),
        "expected a skipped-import warning naming `p`; got: {stderr:?}"
    );
}

/// Helper used by the warning test above — same shape as
/// `tests/update_import.rs::make_bare_with_files`.
fn make_bare_with_files(root: &Path, name: &str, files: &[(&str, &str)]) -> PathBuf {
    let work = root.join(format!("work-{name}"));
    std::fs::create_dir_all(&work).unwrap();
    git(&["init", "-q", "--initial-branch=main", "."], &work);
    for (filename, body) in files {
        let path = work.join(filename);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).unwrap();
        }
        std::fs::write(path, body).unwrap();
    }
    git(&["add", "."], &work);
    git(&["commit", "-q", "-m", "initial"], &work);
    let bare = root.join(format!("{name}.git"));
    git(
        &[
            "clone",
            "-q",
            "--bare",
            work.to_str().unwrap(),
            bare.to_str().unwrap(),
        ],
        root,
    );
    let _ = std::fs::remove_dir_all(&work);
    bare
}

#[test]
#[serial]
fn list_synthetic_manifest_matches_by_name_and_path() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let manifest = manifest_yaml(None, &[("p1", &p1, &[])]);
    let ws = init_workspace(&sb, &manifest);

    // `west list manifest` matches the synthetic project by name.
    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "list",
            "-f",
            "{name}",
            "manifest",
        ])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    assert_eq!(stdout_lines(&out), vec!["manifest".to_owned()]);

    // `west list <self.path>` matches the synthetic project by path.
    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "list",
            "-f",
            "{name}",
            "my-manifest",
        ])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    assert_eq!(stdout_lines(&out), vec!["manifest".to_owned()]);
}

#[test]
#[serial]
fn list_inactive_excludes_synthetic_manifest() {
    // The synthetic manifest project is always "active" — `--inactive`
    // (only-inactive) should omit it.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let p2 = make_bare_with_one_commit(sb.root(), "p2", "p2");
    let manifest = manifest_yaml(Some("-noisy"), &[("p1", &p1, &[]), ("p2", &p2, &["noisy"])]);
    let ws = init_workspace(&sb, &manifest);

    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "list", "-i", "-f", "{name}"])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    assert_eq!(stdout_lines(&out), vec!["p2".to_owned()]);
}

#[test]
#[serial]
fn list_resolves_self_import_directory_with_yaml_extension() {
    // Regression: directory-form `self.import:` accepts both `*.yml`
    // and `*.yaml`. zephyr's real manifest uses `submanifests/` with
    // `.yaml` files (see `submanifests/optional.yaml`); previously the
    // glob matched `*.yml` only and silently dropped them.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let p2 = make_bare_with_one_commit(sb.root(), "p2", "p2");

    let manifest_work = sb.root().join("manifest-work");
    std::fs::create_dir_all(manifest_work.join("submanifests")).unwrap();
    git(
        &["init", "-q", "--initial-branch=main", "."],
        &manifest_work,
    );
    let root_yml = format!(
        "manifest:\n  self:\n    path: my-manifest\n    import: submanifests\n  projects:\n    - name: p1\n      url: {}\n      revision: main\n",
        p1.display()
    );
    let extras_yaml = format!(
        "manifest:\n  projects:\n    - name: p2\n      url: {}\n      revision: main\n",
        p2.display()
    );
    std::fs::write(manifest_work.join("west.yml"), root_yml).unwrap();
    std::fs::write(
        manifest_work.join("submanifests").join("extras.yaml"),
        extras_yaml,
    )
    .unwrap();
    git(&["add", "."], &manifest_work);
    git(&["commit", "-q", "-m", "manifest"], &manifest_work);
    let bare = sb.root().join("manifest.git");
    git(
        &[
            "clone",
            "-q",
            "--bare",
            manifest_work.to_str().unwrap(),
            bare.to_str().unwrap(),
        ],
        sb.root(),
    );
    let _ = std::fs::remove_dir_all(&manifest_work);

    let workspace = sb.root().join("ws");
    sb.west()
        .args([
            "init",
            "--url",
            bare.to_str().unwrap(),
            workspace.to_str().unwrap(),
        ])
        .assert()
        .success();

    let out = sb
        .west()
        .args(["-C", workspace.to_str().unwrap(), "list", "-f", "{name}"])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let mut lines = stdout_lines(&out);
    lines.sort();
    assert_eq!(
        lines,
        vec!["manifest".to_owned(), "p1".to_owned(), "p2".to_owned()]
    );
}

#[test]
#[serial]
fn list_resolves_self_import_without_warnings() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let p2 = make_bare_with_one_commit(sb.root(), "p2", "p2");

    // Build a manifest repo where the root yml uses `self.import:` to
    // pull in extras.yml — both files live in the manifest repo, no
    // vcs needed to resolve. The list command should enumerate both
    // projects without printing the "import: is unsupported" warning
    // that the lenient loader emits.
    let manifest_work = sb.root().join("manifest-work");
    std::fs::create_dir_all(&manifest_work).unwrap();
    git(
        &["init", "-q", "--initial-branch=main", "."],
        &manifest_work,
    );
    let root_yml = format!(
        "manifest:\n  self:\n    path: my-manifest\n    import: extras.yml\n  projects:\n    - name: p1\n      url: {}\n      revision: main\n",
        p1.display()
    );
    let extras_yml = format!(
        "manifest:\n  projects:\n    - name: p2\n      url: {}\n      revision: main\n",
        p2.display()
    );
    std::fs::write(manifest_work.join("west.yml"), root_yml).unwrap();
    std::fs::write(manifest_work.join("extras.yml"), extras_yml).unwrap();
    git(&["add", "."], &manifest_work);
    git(&["commit", "-q", "-m", "manifest"], &manifest_work);
    let bare = sb.root().join("manifest.git");
    git(
        &[
            "clone",
            "-q",
            "--bare",
            manifest_work.to_str().unwrap(),
            bare.to_str().unwrap(),
        ],
        sb.root(),
    );
    let _ = std::fs::remove_dir_all(&manifest_work);

    let workspace = sb.root().join("ws");
    sb.west()
        .args([
            "init",
            "--url",
            bare.to_str().unwrap(),
            workspace.to_str().unwrap(),
        ])
        .assert()
        .success();

    let assert = sb
        .west()
        .args(["-C", workspace.to_str().unwrap(), "list", "-f", "{name}"])
        .assert()
        .success();
    let stderr = String::from_utf8_lossy(&assert.get_output().stderr).into_owned();
    assert!(
        !stderr.contains("self.import"),
        "expected no self-import warning; got stderr: {stderr:?}"
    );
    let mut lines = stdout_lines(&assert.get_output().stdout);
    lines.sort();
    assert_eq!(
        lines,
        vec!["manifest".to_owned(), "p1".to_owned(), "p2".to_owned()]
    );
}
