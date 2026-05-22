//! Integration tests for `west update` with manifest import resolution.
//!
//! Each test stands up a small graph of bare git repos: a manifest repo
//! plus N project repos. The manifest declares a project that has an
//! `import:` directive; the importing project's working tree contains a
//! sub-manifest that pulls in additional projects. After running
//! `west update`, we assert on the resulting workspace state.

use std::path::{Path, PathBuf};
use std::process::Command;

use assert_cmd::Command as AssertCmd;
use serial_test::serial;
use tempfile::TempDir;

const BIN: &str = "west";

// ============================================================================
// Helpers (mirroring tests/update.rs sandbox pattern)
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

fn git_capture(args: &[&str], cwd: &Path) -> String {
    let out = Command::new("git")
        .args(args)
        .current_dir(cwd)
        .output()
        .unwrap_or_else(|e| panic!("git {args:?}: {e}"));
    assert!(out.status.success(), "git {args:?}: {out:?}");
    String::from_utf8(out.stdout).unwrap().trim().to_owned()
}

/// Build a bare repo whose initial commit contains the named files (path → body).
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

/// Run `west init --url <bare> <ws>` and return the workspace path.
fn init_workspace(sb: &Sandbox, manifest_bare: &Path) -> PathBuf {
    let workspace = sb.root().join("ws");
    sb.west()
        .args([
            "init",
            "--url",
            manifest_bare.to_str().unwrap(),
            workspace.to_str().unwrap(),
        ])
        .assert()
        .success();
    workspace
}

// ============================================================================
// Tests
// ============================================================================

#[test]
#[serial]
fn update_resolves_per_project_import() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();

    // Project Q is the leaf — pulled in by P's import.
    let q = make_bare_with_files(sb.root(), "q", &[("README", "q\n")]);

    // Project P contains its own west.yml that declares Q as a project.
    // P also has a normal file so we can verify P itself was checked out.
    let p_yml = format!(
        "manifest:\n  projects:\n    - name: q\n      url: {url}\n      revision: main\n",
        url = q.display(),
    );
    let p = make_bare_with_files(sb.root(), "p", &[("README", "p\n"), ("west.yml", &p_yml)]);

    // Manifest repo: declares P with `import: true`.
    let manifest_yml = format!(
        "manifest:\n  self:\n    path: my-manifest\n  projects:\n    - name: p\n      url: {url}\n      revision: main\n      import: true\n",
        url = p.display(),
    );
    let manifest_bare = make_bare_with_files(sb.root(), "manifest", &[("west.yml", &manifest_yml)]);

    let ws = init_workspace(&sb, &manifest_bare);
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update", "-j", "1"])
        .assert()
        .success();

    // Both P and Q should be cloned with manifest-rev recorded.
    assert!(ws.join("p/README").exists(), "P must be present");
    assert!(ws.join("q/README").exists(), "Q (imported) must be present");
    let p_mr = git_capture(&["rev-parse", "refs/heads/manifest-rev"], &ws.join("p"));
    let q_mr = git_capture(&["rev-parse", "refs/heads/manifest-rev"], &ws.join("q"));
    assert_eq!(p_mr.len(), 40);
    assert_eq!(q_mr.len(), 40);
}

#[test]
#[serial]
fn update_with_import_name_blocklist_skips_blocked() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let q = make_bare_with_files(sb.root(), "q", &[("README", "q\n")]);
    let r = make_bare_with_files(sb.root(), "r", &[("README", "r\n")]);
    let p_yml = format!(
        "manifest:\n  projects:\n    - name: q\n      url: {qu}\n      revision: main\n    - name: r\n      url: {ru}\n      revision: main\n",
        qu = q.display(),
        ru = r.display(),
    );
    let p = make_bare_with_files(sb.root(), "p", &[("west.yml", &p_yml)]);
    let manifest_yml = format!(
        r#"manifest:
  self:
    path: my-manifest
  projects:
    - name: p
      url: {url}
      revision: main
      import:
        name-blocklist: [r]
"#,
        url = p.display(),
    );
    let manifest_bare = make_bare_with_files(sb.root(), "manifest", &[("west.yml", &manifest_yml)]);
    let ws = init_workspace(&sb, &manifest_bare);
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update", "-j", "1"])
        .assert()
        .success();
    assert!(ws.join("p/west.yml").exists());
    assert!(ws.join("q/README").exists(), "Q should be imported");
    assert!(!ws.join("r").exists(), "R should be blocked");
}

#[test]
#[serial]
fn update_per_project_import_missing_file_silently_skipped() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    // P contains a README but no west.yml — its import: true points at a
    // non-existent file. Resolution should silently skip; P updates fine.
    let p = make_bare_with_files(sb.root(), "p", &[("README", "p\n")]);
    let manifest_yml = format!(
        "manifest:\n  self:\n    path: my-manifest\n  projects:\n    - name: p\n      url: {url}\n      revision: main\n      import: true\n",
        url = p.display(),
    );
    let manifest_bare = make_bare_with_files(sb.root(), "manifest", &[("west.yml", &manifest_yml)]);
    let ws = init_workspace(&sb, &manifest_bare);
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update", "-j", "1"])
        .assert()
        .success();
    assert!(ws.join("p/README").exists());
}

#[test]
#[serial]
fn list_reads_per_project_imports_from_manifest_rev_not_worktree() {
    // Mirrors the python `tests/test_manifest.py::test_import_project_list`:
    // P's west.yml (importing Q) exists at the commit `manifest-rev` points
    // at, but is *absent* from the current working tree. `west list` must
    // still surface Q — proves the import resolver reads from git at
    // `manifest-rev`, not the working tree, which is the v1
    // `_manifest_content_at` contract.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();

    let q = make_bare_with_files(sb.root(), "q", &[("README", "q\n")]);
    let p_yml = format!(
        "manifest:\n  projects:\n    - name: q\n      url: {url}\n      revision: main\n",
        url = q.display(),
    );
    let p = make_bare_with_files(sb.root(), "p", &[("README", "p\n"), ("west.yml", &p_yml)]);
    let manifest_yml = format!(
        "manifest:\n  self:\n    path: my-manifest\n  projects:\n    - name: p\n      url: {url}\n      revision: main\n      import: true\n",
        url = p.display(),
    );
    let manifest_bare = make_bare_with_files(sb.root(), "manifest", &[("west.yml", &manifest_yml)]);

    let ws = init_workspace(&sb, &manifest_bare);
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update", "-j", "1"])
        .assert()
        .success();

    // After `west update`, P is at the commit `manifest-rev` points at and
    // P/west.yml exists on disk. Stash the worktree copy aside so anything
    // that still reads from disk returns "missing" — only `manifest-rev`
    // can answer truthfully.
    let p_yml_path = ws.join("p/west.yml");
    assert!(p_yml_path.exists(), "precondition: west update should land P/west.yml");
    std::fs::remove_file(&p_yml_path).unwrap();
    // Verify `manifest-rev` still has it. The pre-fix resolver would now
    // silently lose Q because it reads from the missing worktree path.
    let from_git = git_capture(
        &["show", "refs/heads/manifest-rev:west.yml"],
        &ws.join("p"),
    );
    assert!(
        from_git.contains("name: q"),
        "precondition: west.yml at manifest-rev still references Q; got {from_git:?}"
    );

    let assert = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "list",
            "-f",
            "{name}",
        ])
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(&assert.get_output().stdout).into_owned();
    assert!(
        stdout.lines().any(|l| l.trim() == "q"),
        "expected Q in `west list` output even after p/west.yml was deleted from worktree; got: {stdout:?}",
    );
}

#[test]
#[serial]
fn update_resolves_per_project_directory_import() {
    // Mirrors python `tests/test_manifest.py::test_import_project_directory`:
    // P's import names a directory (`d`) rather than a single file. The
    // directory contains two YAML sub-manifests at `manifest-rev` plus a
    // non-YAML file that must be filtered out. The resolver should pull
    // in projects from BOTH YAML files and ignore the non-YAML entry.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();

    let q = make_bare_with_files(sb.root(), "q", &[("README", "q\n")]);
    let r = make_bare_with_files(sb.root(), "r", &[("README", "r\n")]);

    // P's `d/` directory holds the sub-manifests. `ignore.txt` proves
    // the resolver filters by extension instead of swallowing everything.
    let m1 = format!(
        "manifest:\n  projects:\n    - name: q\n      url: {url}\n      revision: main\n",
        url = q.display(),
    );
    let m2 = format!(
        "manifest:\n  projects:\n    - name: r\n      url: {url}\n      revision: main\n",
        url = r.display(),
    );
    let p = make_bare_with_files(
        sb.root(),
        "p",
        &[
            ("README", "p\n"),
            ("d/m1.yml", m1.as_str()),
            ("d/m2.yml", m2.as_str()),
            ("d/ignore.txt", "not a manifest\n"),
        ],
    );

    let manifest_yml = format!(
        "manifest:\n  self:\n    path: my-manifest\n  projects:\n    - name: p\n      url: {url}\n      revision: main\n      import: d\n",
        url = p.display(),
    );
    let manifest_bare = make_bare_with_files(sb.root(), "manifest", &[("west.yml", &manifest_yml)]);

    let ws = init_workspace(&sb, &manifest_bare);
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update", "-j", "1"])
        .assert()
        .success();

    // Both Q and R should be cloned (pulled in by P's directory
    // import); the non-YAML entry must not have caused parse errors.
    assert!(ws.join("p/d/m1.yml").exists(), "P/d/m1.yml must be present");
    assert!(ws.join("q/README").exists(), "Q (from d/m1.yml) must be cloned");
    assert!(ws.join("r/README").exists(), "R (from d/m2.yml) must be cloned");
}
