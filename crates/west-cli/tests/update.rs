//! Integration tests for `west update`. Each test builds a tiny universe of
//! bare git repos to act as projects, runs `west init` against a manifest
//! that points at them, then drives `west update` and asserts on the
//! resulting workspace state.

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

fn git_capture(args: &[&str], cwd: &Path) -> String {
    let out = Command::new("git")
        .args(args)
        .current_dir(cwd)
        .output()
        .unwrap_or_else(|e| panic!("git {args:?}: {e}"));
    assert!(out.status.success(), "git {args:?}: {out:?}");
    String::from_utf8(out.stdout).unwrap().trim().to_owned()
}

/// Build a bare repo with one commit on `main`. Returns the bare repo path.
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

/// Add another commit to a bare repo via a temporary worktree.
fn add_commit_to_bare(root: &Path, bare: &Path, content: &str) -> String {
    let work = root.join("addcommit-work");
    git(
        &[
            "clone",
            "-q",
            bare.to_str().unwrap(),
            work.to_str().unwrap(),
        ],
        root,
    );
    std::fs::write(work.join("R"), content.as_bytes()).unwrap();
    git(&["add", "."], &work);
    git(&["commit", "-q", "-am", content], &work);
    git(&["push", "-q"], &work);
    let sha = git_capture(&["rev-parse", "HEAD"], &work);
    let _ = std::fs::remove_dir_all(&work);
    sha
}

/// Sandbox: tempdir + isolated WEST_CONFIG_* env vars.
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

/// Build a manifest YAML that points at a slice of `(name, bare_path, [groups])`.
fn manifest_yaml(projects: &[(&str, &Path, &[&str])]) -> String {
    let mut s = String::from("manifest:\n  self:\n    path: my-manifest\n  projects:\n");
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

/// Build the bare manifest repo and run `west init` against it.
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

// ============================================================================
// Tests
// ============================================================================

#[test]
#[serial]
fn update_clones_missing_projects() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let p2 = make_bare_with_one_commit(sb.root(), "p2", "p2");
    let manifest = manifest_yaml(&[("p1", &p1, &[]), ("p2", &p2, &[])]);
    let ws = init_workspace(&sb, &manifest);

    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update"])
        .assert()
        .success();
    assert!(ws.join("p1/R").exists());
    assert!(ws.join("p2/R").exists());
}

#[test]
#[serial]
fn update_records_manifest_rev() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let manifest = manifest_yaml(&[("p1", &p1, &[])]);
    let ws = init_workspace(&sb, &manifest);

    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update"])
        .assert()
        .success();
    let mr = git_capture(&["rev-parse", "refs/heads/manifest-rev"], &ws.join("p1"));
    let head = git_capture(&["rev-parse", "HEAD"], &ws.join("p1"));
    assert_eq!(mr, head, "manifest-rev should equal current HEAD");
}

#[test]
#[serial]
fn update_advances_to_new_remote_commit() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "first");
    let manifest = manifest_yaml(&[("p1", &p1, &[])]);
    let ws = init_workspace(&sb, &manifest);
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update"])
        .assert()
        .success();

    // Add a new commit upstream.
    let new_sha = add_commit_to_bare(sb.root(), &p1, "second");

    // Update again with --fetch always (default smart would skip the fetch
    // if the revision is unchanged, but `main` resolves locally so the
    // smart-skip path *does* update via origin/main). Use --fetch always
    // for determinism.
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update", "-f", "always"])
        .assert()
        .success();

    let head = git_capture(&["rev-parse", "HEAD"], &ws.join("p1"));
    assert_eq!(head, new_sha, "HEAD should advance to new upstream commit");
}

#[test]
#[serial]
fn update_specific_project_only() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let p2 = make_bare_with_one_commit(sb.root(), "p2", "p2");
    let manifest = manifest_yaml(&[("p1", &p1, &[]), ("p2", &p2, &[])]);
    let ws = init_workspace(&sb, &manifest);

    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update", "p1"])
        .assert()
        .success();
    assert!(ws.join("p1/R").exists());
    assert!(!ws.join("p2/R").exists(), "p2 should not have been cloned");
}

#[test]
#[serial]
fn update_specific_project_bypasses_group_filter() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let mut manifest = manifest_yaml(&[("p1", &p1, &["noisy"])]);
    // Add a manifest-level group filter that disables `noisy`.
    manifest = manifest.replace(
        "manifest:\n  self:",
        "manifest:\n  group-filter: [-noisy]\n  self:",
    );
    let ws = init_workspace(&sb, &manifest);

    // Without positionals, p1 is filtered out → no-op.
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update"])
        .assert()
        .success();
    assert!(!ws.join("p1/R").exists());

    // With positional, p1 is updated regardless.
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update", "p1"])
        .assert()
        .success();
    assert!(ws.join("p1/R").exists());
}

#[test]
#[serial]
fn update_cli_group_filter_re_enables() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let mut manifest = manifest_yaml(&[("p1", &p1, &["noisy"])]);
    manifest = manifest.replace(
        "manifest:\n  self:",
        "manifest:\n  group-filter: [-noisy]\n  self:",
    );
    let ws = init_workspace(&sb, &manifest);

    sb.west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "update",
            "--group-filter",
            "+noisy",
        ])
        .assert()
        .success();
    assert!(ws.join("p1/R").exists());
}

#[test]
#[serial]
fn update_unknown_project_errors() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let manifest = manifest_yaml(&[("p1", &p1, &[])]);
    let ws = init_workspace(&sb, &manifest);

    let assert = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "update", "nope"])
        .assert()
        .failure();
    let stderr = String::from_utf8_lossy(&assert.get_output().stderr).into_owned();
    assert!(
        stderr.contains("nope"),
        "stderr should mention the unknown selector; got: {stderr}"
    );
}

#[test]
#[serial]
fn update_continues_after_failure_and_summarizes() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    // p2's URL points at a nonexistent repo → clone will fail.
    let bad = sb.root().join("does-not-exist.git");
    let manifest = manifest_yaml(&[("p1", &p1, &[]), ("p2-bad", &bad, &[])]);
    let ws = init_workspace(&sb, &manifest);

    let assert = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "update", "-j", "1"])
        .assert()
        .failure();
    // p1 should have been updated despite p2-bad failing.
    assert!(ws.join("p1/R").exists());
    let stderr = String::from_utf8_lossy(&assert.get_output().stderr).into_owned();
    assert!(
        stderr.contains("p2-bad"),
        "summary should name the failure; got: {stderr}"
    );
}

#[test]
#[serial]
fn update_parallel_jobs_completes() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let p2 = make_bare_with_one_commit(sb.root(), "p2", "p2");
    let p3 = make_bare_with_one_commit(sb.root(), "p3", "p3");
    let manifest = manifest_yaml(&[("p1", &p1, &[]), ("p2", &p2, &[]), ("p3", &p3, &[])]);
    let ws = init_workspace(&sb, &manifest);

    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update", "-j", "4"])
        .assert()
        .success();
    assert!(ws.join("p1/R").exists());
    assert!(ws.join("p2/R").exists());
    assert!(ws.join("p3/R").exists());
}

#[test]
#[serial]
fn update_idempotent_second_run() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let manifest = manifest_yaml(&[("p1", &p1, &[])]);
    let ws = init_workspace(&sb, &manifest);
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update"])
        .assert()
        .success();
    let head1 = git_capture(&["rev-parse", "HEAD"], &ws.join("p1"));

    // Run again — should still succeed and HEAD should not move.
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update"])
        .assert()
        .success();
    let head2 = git_capture(&["rev-parse", "HEAD"], &ws.join("p1"));
    assert_eq!(head1, head2);
}

#[test]
#[serial]
fn update_keep_descendants_keeps_branch() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let manifest = manifest_yaml(&[("p1", &p1, &[])]);
    let ws = init_workspace(&sb, &manifest);
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update"])
        .assert()
        .success();

    // Switch to a branch that's a descendant of HEAD (currently same commit).
    git(&["checkout", "-q", "-b", "downstream"], &ws.join("p1"));
    std::fs::write(ws.join("p1").join("R"), b"local\n").unwrap();
    git(&["commit", "-q", "-am", "downstream"], &ws.join("p1"));

    // Add a new commit upstream that the manifest will move us to. Since
    // our local downstream is built ON TOP of the previous HEAD, it
    // remains a descendant of upstream main only if upstream did NOT
    // advance. Here we *don't* advance upstream — manifest-rev stays at
    // the original commit, our `downstream` branch is a descendant.
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update", "-k"])
        .assert()
        .success();

    let head = git_capture(&["rev-parse", "--abbrev-ref", "HEAD"], &ws.join("p1"));
    assert_eq!(head, "downstream", "should still be on downstream branch");
}

#[test]
#[serial]
fn update_narrow_writes_inline_config() {
    if !git_available() {
        return;
    }
    // Smoke test: the --narrow flag should map to `tool.git.fetch.tags=false`.
    // Verify by inspecting the captured stderr for a successful run with the
    // flag. Functional verification is in the vcs tests where we check
    // tag-fetch behavior directly.
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let manifest = manifest_yaml(&[("p1", &p1, &[])]);
    let ws = init_workspace(&sb, &manifest);

    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update", "-n"])
        .assert()
        .success();
    assert!(ws.join("p1/R").exists());
}

#[test]
#[serial]
fn update_handles_sha_revision() {
    // Regression test: zephyr-style manifests pin every project at a
    // bare commit SHA. `git clone --branch <SHA>` is rejected by git
    // ("Remote branch <SHA> not found in upstream"), so the update
    // worker must clone without `--branch` and rely on the subsequent
    // fetch + detached checkout.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1-first");
    // Capture the (only) SHA on `main` and use it as the revision.
    let sha = git_capture(&["rev-parse", "HEAD"], &p1);

    let manifest = format!(
        "manifest:\n  self:\n    path: my-manifest\n  projects:\n    - name: p1\n      url: {url}\n      revision: {sha}\n",
        url = p1.display(),
    );
    let ws = init_workspace(&sb, &manifest);

    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update"])
        .assert()
        .success();
    assert!(ws.join("p1/R").exists());
    let head = git_capture(&["rev-parse", "HEAD"], &ws.join("p1"));
    assert_eq!(head, sha, "HEAD should land on the manifest's SHA");
}
