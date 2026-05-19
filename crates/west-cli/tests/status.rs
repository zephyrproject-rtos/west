//! Integration tests for `west status`. Sandbox + bare-repo
//! fixture pattern shared with `tests/diff.rs` / `tests/forall.rs`.
//! Covers: clean workspace, dirty-project rendering, --exit-code,
//! --long, project filter, uncloned-positional error, --color,
//! parallel non-interleave.

use std::path::{Path, PathBuf};
use std::process::Command;

use assert_cmd::Command as AssertCmd;
use serial_test::serial;
use tempfile::TempDir;

const BIN: &str = "west";

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

fn manifest_yaml(projects: &[(&str, &Path)]) -> String {
    let mut s = String::from("manifest:\n  self:\n    path: my-manifest\n  projects:\n");
    for (name, bare) in projects {
        s.push_str(&format!(
            "    - name: {name}\n      url: {url}\n      revision: main\n",
            url = bare.display()
        ));
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

fn update_all(sb: &Sandbox, ws: &Path) {
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update", "-j", "1"])
        .assert()
        .success();
}

fn touch(ws: &Path, project: &str, file: &str, content: &str) {
    let p = ws.join(project).join(file);
    std::fs::write(&p, content).unwrap_or_else(|e| panic!("write {p:?}: {e}"));
}

// ============================================================================
// Tests
// ============================================================================

#[test]
#[serial]
fn status_clean_workspace_emits_no_banner() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let yaml = manifest_yaml(&[("alpha", &alpha)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);

    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "status"])
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(out.get_output().stdout.as_slice()).into_owned();
    assert!(
        !stdout.contains("=== status of"),
        "no banner expected for clean tree, got: {stdout:?}"
    );
}

#[test]
#[serial]
fn status_dirty_project_emits_banner_and_body() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let yaml = manifest_yaml(&[("alpha", &alpha)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);
    touch(&ws, "alpha", "R", "CHANGED");

    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "status"])
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(out.get_output().stdout.as_slice()).into_owned();
    assert!(
        stdout.contains("=== status of alpha"),
        "missing banner: {stdout:?}"
    );
    // Porcelain v1: ` M R` (modified, unstaged) — accept either
    // single-letter or two-column shape, just look for `R` and
    // the modification marker.
    assert!(
        stdout.contains("M R") || stdout.contains(" M R") || stdout.contains("M  R"),
        "expected short-status body for modified `R` file in: {stdout:?}"
    );
}

#[test]
#[serial]
fn status_exit_code_returns_1_when_dirty() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let yaml = manifest_yaml(&[("alpha", &alpha)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);
    touch(&ws, "alpha", "R", "X");

    sb.west()
        .args(["-C", ws.to_str().unwrap(), "status", "--exit-code"])
        .assert()
        .code(1);
}

#[test]
#[serial]
fn status_exit_code_returns_0_when_clean() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let yaml = manifest_yaml(&[("alpha", &alpha)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);

    sb.west()
        .args(["-C", ws.to_str().unwrap(), "status", "--exit-code"])
        .assert()
        .success();
}

#[test]
#[serial]
fn status_long_shows_every_project_including_clean() {
    if !git_available() {
        return;
    }
    // Two projects, one dirty + one clean. In `--long` mode both
    // should appear (the long-form value is per-project context).
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let beta = make_bare_with_one_commit(sb.root(), "beta", "B");
    let yaml = manifest_yaml(&[("alpha", &alpha), ("beta", &beta)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);
    touch(&ws, "alpha", "R", "CHANGED");

    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "status",
            "--long",
            "--color",
            "never",
        ])
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(out.get_output().stdout.as_slice()).into_owned();
    assert!(
        stdout.contains("=== status of alpha"),
        "missing alpha banner: {stdout:?}"
    );
    assert!(
        stdout.contains("=== status of beta"),
        "missing beta banner (clean projects must show under --long): {stdout:?}"
    );
    // Long form contains the "nothing to commit" phrase for the
    // clean side and a modification marker for the dirty side.
    assert!(
        stdout.contains("nothing to commit"),
        "expected long-form clean message in: {stdout:?}"
    );
}

#[test]
#[serial]
fn status_filters_projects_by_positional_name() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let beta = make_bare_with_one_commit(sb.root(), "beta", "B");
    let yaml = manifest_yaml(&[("alpha", &alpha), ("beta", &beta)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);
    touch(&ws, "alpha", "R", "X");
    touch(&ws, "beta", "R", "Y");

    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "status", "alpha"])
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(out.get_output().stdout.as_slice()).into_owned();
    assert!(
        stdout.contains("=== status of alpha"),
        "missing alpha banner: {stdout:?}"
    );
    assert!(
        !stdout.contains("=== status of beta"),
        "unexpected beta banner: {stdout:?}"
    );
}

#[test]
#[serial]
fn status_uncloned_positional_errors() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let beta = make_bare_with_one_commit(sb.root(), "beta", "B");
    let yaml = manifest_yaml(&[("alpha", &alpha), ("beta", &beta)]);
    let ws = init_workspace(&sb, &yaml);
    // Skip update_all on purpose — projects aren't cloned.
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "status", "beta"])
        .assert()
        .code(2);
}

#[test]
#[serial]
fn status_short_color_always_includes_ansi() {
    // Short mode now honours `--color always` — regression
    // check for the bug where the default mode was always
    // colorless even in an interactive terminal.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let yaml = manifest_yaml(&[("alpha", &alpha)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);
    touch(&ws, "alpha", "R", "CHANGED");

    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "status",
            "--color",
            "always",
        ])
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(out.get_output().stdout.as_slice()).into_owned();
    assert!(
        stdout.contains("\x1b["),
        "expected ANSI escapes in short mode with --color always: {stdout:?}"
    );
}

#[test]
#[serial]
fn status_long_color_always_includes_ansi() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let yaml = manifest_yaml(&[("alpha", &alpha)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);
    touch(&ws, "alpha", "R", "CHANGED");

    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "status",
            "--long",
            "--color",
            "always",
        ])
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(out.get_output().stdout.as_slice()).into_owned();
    assert!(
        stdout.contains("\x1b["),
        "expected ANSI escapes with --long --color always: {stdout:?}"
    );
}

#[test]
#[serial]
fn status_parallel_does_not_interleave_output() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let beta = make_bare_with_one_commit(sb.root(), "beta", "B");
    let yaml = manifest_yaml(&[("alpha", &alpha), ("beta", &beta)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);
    touch(&ws, "alpha", "R", "A-X");
    touch(&ws, "beta", "R", "B-Y");

    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "status", "-j", "2"])
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(out.get_output().stdout.as_slice()).into_owned();
    let alpha_pos = stdout
        .find("=== status of alpha")
        .unwrap_or_else(|| panic!("missing alpha banner: {stdout:?}"));
    let beta_pos = stdout
        .find("=== status of beta")
        .unwrap_or_else(|| panic!("missing beta banner: {stdout:?}"));
    assert!(alpha_pos < beta_pos, "out-of-order banners: {stdout:?}");
}

#[test]
#[serial]
fn status_quiet_suppresses_banner() {
    if !git_available() {
        return;
    }
    // `-q` is the global flag from Cli; both positions should
    // work. Reusing the same primitive as `west diff` means the
    // banner gating lives in `Settings::quiet`.
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let yaml = manifest_yaml(&[("alpha", &alpha)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);
    touch(&ws, "alpha", "R", "CHANGED");

    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "status", "-q"])
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(out.get_output().stdout.as_slice()).into_owned();
    assert!(
        !stdout.contains("=== status of"),
        "banner present despite -q: {stdout:?}"
    );
    // Body still printed.
    assert!(
        stdout.contains("R"),
        "expected short-status body under -q: {stdout:?}"
    );
}
