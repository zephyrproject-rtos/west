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
    assert_eq!(
        lines.len(),
        1,
        "expected one active project, got: {lines:?}"
    );
    assert!(lines[0].starts_with("p1"), "got: {:?}", lines[0]);
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
    assert_eq!(lines, vec!["p1:p1:main".to_owned()]);
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
    assert_eq!(lines, vec!["p1".to_owned(), "p2".to_owned()]);
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
        stderr.contains("--inactive cannot be combined with project names"),
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
    assert_eq!(stdout_lines(&out), vec!["p1      |      p1".to_owned()]);
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
    assert_eq!(lines.len(), 1);
    assert_eq!(
        lines[0].len(),
        40,
        "expected 40-char sha, got: {:?}",
        lines[0]
    );
    assert!(lines[0].chars().all(|c| c.is_ascii_hexdigit()));
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

    // Before update: not-cloned.
    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "list", "-f", "{cloned}"])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    assert_eq!(stdout_lines(&out), vec!["not-cloned".to_owned()]);

    // After update: cloned.
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
    assert_eq!(stdout_lines(&out), vec!["cloned".to_owned()]);
}
