//! Integration tests for `west grep`. Same sandbox + bare-repo
//! scaffolding as `tests/forall.rs` / `tests/list.rs` (architectural
//! review flagged extraction as overdue; not bundled with this change).

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

fn which(prog: &str) -> bool {
    Command::new(prog)
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

/// Build a bare repo with a single commit containing one file with
/// the given body. Returns the bare repo path.
fn make_bare_with_file(root: &Path, name: &str, file: &str, body: &str) -> PathBuf {
    let work = root.join(format!("work-{name}"));
    std::fs::create_dir_all(&work).unwrap();
    git(&["init", "-q", "--initial-branch=main", "."], &work);
    std::fs::write(work.join(file), body.as_bytes()).unwrap();
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

// ============================================================================
// Tests
// ============================================================================

#[test]
#[serial]
fn grep_default_git_grep_matches_emit_banner() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_file(sb.root(), "p1", "f.txt", "hello world\n");
    let p2 = make_bare_with_file(sb.root(), "p2", "f.txt", "nothing here\n");
    let manifest = manifest_yaml(&[("p1", &p1), ("p2", &p2)]);
    let ws = init_workspace(&sb, &manifest);
    update_all(&sb, &ws);

    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "grep",
            "--color=never",
            "--",
            "hello",
        ])
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(&out.get_output().stdout).into_owned();
    let stderr = String::from_utf8_lossy(&out.get_output().stderr).into_owned();
    // Banner (chrome) → stderr; matched lines (result) → stdout.
    assert!(
        stderr.contains("=== p1 (p1):"),
        "p1 banner missing in stderr: {stderr}"
    );
    assert!(
        stdout.contains("hello world"),
        "match line missing in stdout: {stdout}"
    );
    assert!(
        !stderr.contains("=== p2"),
        "p2 has no match — banner should be suppressed; got: {stderr}"
    );
}

#[test]
#[serial]
fn grep_no_match_silent_exit_zero() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_file(sb.root(), "p1", "f.txt", "hello\n");
    let manifest = manifest_yaml(&[("p1", &p1)]);
    let ws = init_workspace(&sb, &manifest);
    update_all(&sb, &ws);

    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "grep",
            "--color=never",
            "--",
            "definitely-not-there",
        ])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let s = String::from_utf8_lossy(&out);
    assert!(
        s.trim().is_empty(),
        "expected silent stdout on no match; got: {s:?}"
    );
}

#[test]
#[serial]
fn grep_passthrough_args_separator() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_file(sb.root(), "p1", "f.txt", "Hello World\n");
    let manifest = manifest_yaml(&[("p1", &p1)]);
    let ws = init_workspace(&sb, &manifest);
    update_all(&sb, &ws);

    // -i (case-insensitive) is the tool's own flag; we pass it through
    // via `--` so clap doesn't try to claim it.
    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "grep",
            "--color=never",
            "--",
            "-i",
            "hello",
        ])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let s = String::from_utf8_lossy(&out);
    assert!(s.contains("Hello World"), "expected -i match; got: {s:?}");
}

#[test]
#[serial]
fn grep_ripgrep_when_available() {
    if !git_available() || !which("rg") {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_file(sb.root(), "p1", "f.txt", "needle\n");
    let manifest = manifest_yaml(&[("p1", &p1)]);
    let ws = init_workspace(&sb, &manifest);
    update_all(&sb, &ws);

    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "grep",
            "-t",
            "ripgrep",
            "--color=never",
            "--",
            "needle",
        ])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let s = String::from_utf8_lossy(&out);
    assert!(s.contains("needle"), "ripgrep match missing: {s:?}");
}

#[test]
#[serial]
fn grep_system_grep_recurses() {
    // System `grep` doesn't recurse by default; the command's builtin
    // default args inject `--recursive` so it walks the tree.
    if !git_available() || !which("grep") {
        return;
    }
    let sb = Sandbox::new();
    // Helper only does flat files; build the nested fixture inline so
    // we can exercise grep's recursion (system `grep` needs `-r`).
    let work = sb.root().join("work-p1b");
    std::fs::create_dir_all(work.join("deeply/nested")).unwrap();
    git(&["init", "-q", "--initial-branch=main", "."], &work);
    std::fs::write(work.join("deeply/nested/f.txt"), b"deepneedle\n").unwrap();
    git(&["add", "."], &work);
    git(&["commit", "-q", "-m", "initial"], &work);
    let bare = sb.root().join("p1b.git");
    git(
        &[
            "clone",
            "-q",
            "--bare",
            work.to_str().unwrap(),
            bare.to_str().unwrap(),
        ],
        sb.root(),
    );
    let _ = std::fs::remove_dir_all(&work);

    let manifest = manifest_yaml(&[("p1b", &bare)]);
    let ws = init_workspace(&sb, &manifest);
    update_all(&sb, &ws);

    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "grep",
            "-t",
            "grep",
            "--color=never",
            "--",
            "deepneedle",
        ])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let s = String::from_utf8_lossy(&out);
    assert!(
        s.contains("deepneedle"),
        "system grep --recursive default should find deepneedle; got: {s:?}"
    );
}

#[test]
#[serial]
fn grep_tool_path_override_missing_errors() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_file(sb.root(), "p1", "f.txt", "hello\n");
    let manifest = manifest_yaml(&[("p1", &p1)]);
    let ws = init_workspace(&sb, &manifest);
    update_all(&sb, &ws);

    let assert = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "grep",
            "--tool-path",
            "/nonexistent/path/to/grep",
            "--",
            "hello",
        ])
        .assert()
        .failure();
    // The spawn fails per-project; we emit a per-project diagnostic
    // and an aggregate summary at the end.
    let stderr = String::from_utf8_lossy(&assert.get_output().stderr).into_owned();
    assert!(
        stderr.contains("grep failed"),
        "expected failure message; got: {stderr:?}"
    );
}

#[test]
#[serial]
fn grep_parallel_manifest_order() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    // Three projects, all containing the same pattern. Manifest order
    // = pa, pb, pc — expect that exact order in stdout banners.
    let pa = make_bare_with_file(sb.root(), "pa", "f.txt", "match\n");
    let pb = make_bare_with_file(sb.root(), "pb", "f.txt", "match\n");
    let pc = make_bare_with_file(sb.root(), "pc", "f.txt", "match\n");
    let manifest = manifest_yaml(&[("pa", &pa), ("pb", &pb), ("pc", &pc)]);
    let ws = init_workspace(&sb, &manifest);
    update_all(&sb, &ws);

    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "grep",
            "-j",
            "3",
            "--color=never",
            "--",
            "match",
        ])
        .assert()
        .success();
    // Banners (chrome) carry the project names and land on stderr in
    // manifest order.
    let s = String::from_utf8_lossy(&out.get_output().stderr).into_owned();
    let pa_pos = s.find("=== pa").expect("pa banner");
    let pb_pos = s.find("=== pb").expect("pb banner");
    let pc_pos = s.find("=== pc").expect("pc banner");
    assert!(
        pa_pos < pb_pos && pb_pos < pc_pos,
        "expected manifest order pa<pb<pc; got positions {pa_pos}/{pb_pos}/{pc_pos} in:\n{s}"
    );
}

#[test]
#[serial]
fn grep_default_includes_synthetic_manifest_project() {
    // The workspace's manifest repo (the "synthetic" project at
    // `my-manifest/`) should be searched by default — same as
    // forall/list. Without this, users grep'ing a workspace miss
    // every match that lives in the manifest repo itself.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_file(sb.root(), "p1", "f.txt", "hello\n");
    let manifest = manifest_yaml(&[("p1", &p1)]);
    let ws = init_workspace(&sb, &manifest);
    update_all(&sb, &ws);

    // `my-manifest` literal lives in west.yml's `self.path:` — a string
    // that only appears in the synthetic project's tree.
    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "grep",
            "--color=never",
            "--",
            "my-manifest",
        ])
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(&out.get_output().stdout).into_owned();
    let stderr = String::from_utf8_lossy(&out.get_output().stderr).into_owned();
    // Banner → stderr; matched line → stdout.
    assert!(
        stderr.contains("=== manifest (my-manifest):"),
        "synthetic manifest project banner missing in stderr: {stderr}"
    );
    assert!(
        stdout.contains("my-manifest"),
        "match line missing in stdout: {stdout}"
    );
}

#[test]
#[serial]
fn grep_pattern_without_separator() {
    // `west grep PATTERN` — no `--` needed when the pattern doesn't
    // look like a flag. Matches v1 argparse-REMAINDER behaviour and
    // is what `tests/test_project.py::test_grep` exercises.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_file(sb.root(), "p1", "f.txt", "needle\n");
    let manifest = manifest_yaml(&[("p1", &p1)]);
    let ws = init_workspace(&sb, &manifest);
    update_all(&sb, &ws);

    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "grep",
            "--color=never",
            "needle",
        ])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let s = String::from_utf8_lossy(&out);
    assert!(s.contains("needle"), "no-separator match missing: {s:?}");
}

#[test]
#[serial]
fn grep_project_flag_resolves_synthetic() {
    // `west grep -p manifest PATTERN` should resolve `manifest` to the
    // synthetic manifest project (not error as unknown). v1 grep uses
    // `-p/--project` rather than positional projects since the
    // positional space is the pattern + tool args.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_file(sb.root(), "p1", "f.txt", "hello\n");
    let manifest = manifest_yaml(&[("p1", &p1)]);
    let ws = init_workspace(&sb, &manifest);
    update_all(&sb, &ws);

    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "grep",
            "--color=never",
            "-p",
            "manifest",
            "--",
            "my-manifest",
        ])
        .assert()
        .success();
    // Banners (chrome) → stderr.
    let s = String::from_utf8_lossy(&out.get_output().stderr).into_owned();
    assert!(
        s.contains("=== manifest (my-manifest):"),
        "synthetic banner missing on `-p manifest`: {s}"
    );
    // p1 should NOT appear — we asked only for the manifest project.
    assert!(!s.contains("=== p1"), "p1 should not be searched: {s}");
}

#[test]
#[serial]
fn grep_quiet_suppresses_banner() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_file(sb.root(), "p1", "f.txt", "hello\n");
    let manifest = manifest_yaml(&[("p1", &p1)]);
    let ws = init_workspace(&sb, &manifest);
    update_all(&sb, &ws);

    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "-q",
            "grep",
            "--color=never",
            "--",
            "hello",
        ])
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(&out.get_output().stdout).into_owned();
    let stderr = String::from_utf8_lossy(&out.get_output().stderr).into_owned();
    // Banner lives on stderr now; `-q` must suppress it there.
    assert!(
        !stderr.contains("=== p1"),
        "-q should suppress banner; got: {stderr:?}"
    );
    assert!(
        stdout.contains("hello"),
        "body still expected on stdout; got: {stdout:?}"
    );
}
