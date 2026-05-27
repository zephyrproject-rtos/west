//! Integration tests for `west compare`. Same fixture pattern as
//! `tests/diff.rs` / `tests/status.rs`. Covers each of compare's
//! three divergence signals (HEAD≠manifest-rev, dirty,
//! branch-checked-out) plus the `--exit-code` and
//! `--ignore-branches` flags.

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
fn compare_clean_workspace_emits_no_output() {
    // After `west update`, projects are detached at manifest-rev,
    // working trees clean, no local branches → all aligned. Even
    // with default flags (no --ignore-branches), nothing should
    // print.
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
        .args(["-C", ws.to_str().unwrap(), "compare", "--color", "never"])
        .assert()
        .success();
    let stderr = String::from_utf8_lossy(out.get_output().stderr.as_slice()).into_owned();
    // Banner is chrome → stderr; an aligned workspace emits nothing.
    assert!(
        !stderr.contains("=== alpha"),
        "expected no alpha banner on aligned workspace: {stderr:?}"
    );
}

#[test]
#[serial]
fn compare_dirty_project_emits_output() {
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
        .args(["-C", ws.to_str().unwrap(), "compare", "--color", "never"])
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(out.get_output().stdout.as_slice()).into_owned();
    let stderr = String::from_utf8_lossy(out.get_output().stderr.as_slice()).into_owned();
    // `=== alpha` banner is chrome → stderr; the comparison body
    // (rev info + status) → stdout.
    assert!(
        stderr.contains("=== alpha"),
        "missing alpha banner on stderr: {stderr:?}"
    );
    assert!(
        !stdout.contains("=== alpha"),
        "banner leaked onto stdout: {stdout:?}"
    );
    assert!(
        stdout.contains("--- manifest-rev:"),
        "missing manifest-rev sub-banner: {stdout:?}"
    );
    assert!(
        stdout.contains("HEAD:"),
        "missing HEAD sub-banner: {stdout:?}"
    );
    assert!(
        stdout.contains("--- status:"),
        "missing status sub-banner: {stdout:?}"
    );
}

#[test]
#[serial]
fn compare_head_diverges_from_manifest_rev_emits_output() {
    // After update, HEAD is at manifest-rev. Land a local commit
    // → HEAD diverges from manifest-rev. Working tree stays
    // clean, no branch (still detached). The divergence signal
    // alone should produce output.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let yaml = manifest_yaml(&[("alpha", &alpha)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);

    let alpha_dir = ws.join("alpha");
    std::fs::write(alpha_dir.join("R"), "LOCAL").unwrap();
    git(&["add", "."], &alpha_dir);
    git(&["commit", "-q", "-m", "local"], &alpha_dir);

    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "compare", "--color", "never"])
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(out.get_output().stdout.as_slice()).into_owned();
    let stderr = String::from_utf8_lossy(out.get_output().stderr.as_slice()).into_owned();
    assert!(
        stderr.contains("=== alpha"),
        "missing banner on divergent HEAD on stderr: {stderr:?}"
    );
    assert!(
        stdout.contains("--- manifest-rev:") && stdout.contains("HEAD:"),
        "missing rev-info sub-banners: {stdout:?}"
    );
}

#[test]
#[serial]
fn compare_branch_only_signal_shows_by_default() {
    // Check out a branch on top of manifest-rev (no other
    // changes). Default behaviour (no --ignore-branches) emits
    // output because the user is on a branch — mirrors v1.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let yaml = manifest_yaml(&[("alpha", &alpha)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);

    let alpha_dir = ws.join("alpha");
    // `git checkout -b` from the detached state preserves HEAD's
    // commit but binds it to a branch.
    git(&["checkout", "-b", "feature"], &alpha_dir);

    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "compare", "--color", "never"])
        .assert()
        .success();
    let stderr = String::from_utf8_lossy(out.get_output().stderr.as_slice()).into_owned();
    assert!(
        stderr.contains("=== alpha"),
        "expected banner for branch-only signal on stderr: {stderr:?}"
    );
}

#[test]
#[serial]
fn compare_ignore_branches_skips_branch_only_signal() {
    // Same scenario as above, but --ignore-branches suppresses
    // the output (HEAD matches manifest-rev, working tree clean,
    // only signal is the branch — which we now ignore).
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let yaml = manifest_yaml(&[("alpha", &alpha)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);

    let alpha_dir = ws.join("alpha");
    git(&["checkout", "-b", "feature"], &alpha_dir);

    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "compare",
            "--ignore-branches",
            "--color",
            "never",
        ])
        .assert()
        .success();
    let stderr = String::from_utf8_lossy(out.get_output().stderr.as_slice()).into_owned();
    assert!(
        !stderr.contains("=== alpha"),
        "expected no banner with --ignore-branches: {stderr:?}"
    );
}

#[test]
#[serial]
fn compare_ignore_branches_pair_last_one_wins() {
    // `--ignore-branches` and `--no-ignore-branches` compose
    // like python's `argparse.BooleanOptionalAction`: whichever
    // comes LAST on argv decides. clap's `overrides_with` on
    // both flags enforces this — only the last-set one survives
    // parsing.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let yaml = manifest_yaml(&[("alpha", &alpha)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);

    let alpha_dir = ws.join("alpha");
    git(&["checkout", "-b", "feature"], &alpha_dir);

    // Case 1: `--ignore-branches --no-ignore-branches` — last
    // is `--no-ignore-branches` → branch signal SHOWS.
    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "compare",
            "--ignore-branches",
            "--no-ignore-branches",
            "--color",
            "never",
        ])
        .assert()
        .success();
    let stderr = String::from_utf8_lossy(out.get_output().stderr.as_slice()).into_owned();
    assert!(
        stderr.contains("=== alpha"),
        "last-wins broken: --no-ignore-branches at end should re-enable branch signal: {stderr:?}"
    );

    // Case 2: `--no-ignore-branches --ignore-branches` — last
    // is `--ignore-branches` → branch signal SUPPRESSED.
    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "compare",
            "--no-ignore-branches",
            "--ignore-branches",
            "--color",
            "never",
        ])
        .assert()
        .success();
    let stderr = String::from_utf8_lossy(out.get_output().stderr.as_slice()).into_owned();
    assert!(
        !stderr.contains("=== alpha"),
        "last-wins broken: --ignore-branches at end should suppress branch signal: {stderr:?}"
    );
}

#[test]
#[serial]
fn compare_no_ignore_branches_overrides_config() {
    // `compare.ignore-branches = true` in config, then
    // `--no-ignore-branches` on the CLI: the CLI wins, branch
    // signal triggers output.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let yaml = manifest_yaml(&[("alpha", &alpha)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);
    sb.west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "config",
            "set",
            "compare.ignore-branches",
            "true",
        ])
        .assert()
        .success();

    let alpha_dir = ws.join("alpha");
    git(&["checkout", "-b", "feature"], &alpha_dir);

    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "compare",
            "--no-ignore-branches",
            "--color",
            "never",
        ])
        .assert()
        .success();
    let stderr = String::from_utf8_lossy(out.get_output().stderr.as_slice()).into_owned();
    assert!(
        stderr.contains("=== alpha"),
        "expected banner with --no-ignore-branches override: {stderr:?}"
    );
}

#[test]
#[serial]
fn compare_exit_code_returns_1_when_output_printed() {
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
        .args([
            "-C",
            ws.to_str().unwrap(),
            "compare",
            "--exit-code",
            "--color",
            "never",
        ])
        .assert()
        .code(1);
}

#[test]
#[serial]
fn compare_exit_code_returns_0_when_aligned() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let yaml = manifest_yaml(&[("alpha", &alpha)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);

    sb.west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "compare",
            "--exit-code",
            "--ignore-branches",
        ])
        .assert()
        .success();
}

#[test]
#[serial]
fn compare_filters_projects_by_positional_name() {
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
        .args([
            "-C",
            ws.to_str().unwrap(),
            "compare",
            "alpha",
            "--color",
            "never",
        ])
        .assert()
        .success();
    let stderr = String::from_utf8_lossy(out.get_output().stderr.as_slice()).into_owned();
    assert!(
        stderr.contains("=== alpha"),
        "missing alpha banner on stderr: {stderr:?}"
    );
    assert!(
        !stderr.contains("=== beta"),
        "unexpected beta banner: {stderr:?}"
    );
}

#[test]
#[serial]
fn compare_color_always_emits_colored_banner() {
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
        .args(["-C", ws.to_str().unwrap(), "compare", "--color", "always"])
        .assert()
        .success();
    let stderr = String::from_utf8_lossy(out.get_output().stderr.as_slice()).into_owned();
    let banner_line = stderr
        .lines()
        .find(|l| l.contains("=== alpha"))
        .unwrap_or_else(|| panic!("missing banner on stderr: {stderr:?}"));
    assert!(
        banner_line.starts_with("\x1b["),
        "banner not coloured under --color always: {banner_line:?}"
    );
}

#[test]
#[serial]
fn compare_uncloned_positional_errors() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let beta = make_bare_with_one_commit(sb.root(), "beta", "B");
    let yaml = manifest_yaml(&[("alpha", &alpha), ("beta", &beta)]);
    let ws = init_workspace(&sb, &yaml);
    // Skip update_all — projects aren't cloned.
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "compare", "beta"])
        .assert()
        .code(2);
}
