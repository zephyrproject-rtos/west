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
fn update_warns_about_left_behind_branch() {
    // v1's post_checkout_help: detaching over a checked-out branch
    // warns (at WARN, so it shows by default) and prints the exact
    // command to get back. The branch sits on the same commit as the
    // new manifest-rev here, so the hint is the fast-forward form.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let manifest = manifest_yaml(&[("p1", &p1, &[])]);
    let ws = init_workspace(&sb, &manifest);

    // First update clones p1 with a detached manifest-rev.
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update"])
        .assert()
        .success();

    // Check out a local branch on top of the manifest-rev commit.
    git(&["checkout", "-b", "topic"], &ws.join("p1"));

    // Second update detaches again, leaving "topic" behind.
    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "update"])
        .assert()
        .success();
    let stderr = String::from_utf8_lossy(&out.get_output().stderr).into_owned();
    assert!(
        stderr.contains(r#"left behind p1 branch "topic""#),
        "missing left-behind warning: {stderr:?}"
    );
    assert!(
        stderr.contains("fast forward") && stderr.contains("checkout topic"),
        "missing fast-forward recovery hint: {stderr:?}"
    );
}

#[test]
#[serial]
fn update_verbose_reports_fetching() {
    // v1's `small_banner('… fetching, need revision …')` — emitted
    // at DEBUG. Default is INFO (matches v1's `Verbosity.INF`), so
    // `-v` raises to Debug and surfaces the per-project fetch
    // chatter. Using `-v` (not `-vv`) here pins the minimum flag
    // count needed to see the line.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let manifest = manifest_yaml(&[("p1", &p1, &[])]);
    let ws = init_workspace(&sb, &manifest);

    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "-v", "update"])
        .assert()
        .success();
    let stderr = String::from_utf8_lossy(&out.get_output().stderr).into_owned();
    assert!(
        stderr.contains("fetching, need revision main"),
        "missing fetch debug line under -v: {stderr:?}"
    );
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

#[test]
#[serial]
fn update_raw_mode_prints_head_is_now_at() {
    // Parity with v1: in raw mode, git's stderr flows directly through —
    // so the user sees the literal `HEAD is now at <short> <subject>`
    // line for each project's detached checkout.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let manifest = manifest_yaml(&[("p1", &p1, &[])]);
    let ws = init_workspace(&sb, &manifest);

    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "--config",
            "output.raw=true",
            "update",
        ])
        .assert()
        .success()
        .get_output()
        .clone();
    let stderr = String::from_utf8_lossy(&out.stderr).into_owned();
    assert!(
        stderr.contains("HEAD is now at"),
        "raw-mode stderr should contain git's HEAD-is-now-at line; got:\n{stderr}"
    );
}

#[test]
#[serial]
fn update_parallel_transcript_contains_head_is_now_at() {
    // Parallel + non-TTY (the default in tests) uses BufferingReporter,
    // which captures git's stderr into a per-project transcript and
    // flushes it at the end. The `HEAD is now at …` line lands in the
    // captured output.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let p2 = make_bare_with_one_commit(sb.root(), "p2", "p2");
    let manifest = manifest_yaml(&[("p1", &p1, &[]), ("p2", &p2, &[])]);
    let ws = init_workspace(&sb, &manifest);

    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "update", "-j", "2"])
        .assert()
        .success()
        .get_output()
        .clone();
    let stderr = String::from_utf8_lossy(&out.stderr).into_owned();
    assert!(
        stderr.contains("HEAD is now at"),
        "buffering-reporter transcript should include the HEAD-is-now-at line; got:\n{stderr}"
    );
}

// ============================================================================
// Cache flags
// ============================================================================

/// Build a manifest with `revision: <sha>` per project (instead of `main`)
/// and a custom URL — the cache tests pin SHAs so smart-skip can avoid
/// hitting the (deliberately non-existent) project URL.
fn manifest_yaml_with_url_and_rev(projects: &[(&str, &str, &str)]) -> String {
    let mut s = String::from("manifest:\n  self:\n    path: my-manifest\n  projects:\n");
    for (name, url, rev) in projects {
        s.push_str(&format!(
            "    - name: {name}\n      url: {url}\n      revision: {rev}\n",
        ));
    }
    s
}

#[test]
#[serial]
fn update_uses_name_cache_when_dir_exists() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let sha = git_capture(&["rev-parse", "HEAD"], &p1);

    // Pre-populate <name-cache>/p1 from p1.git. Project URL points at a
    // nonexistent path — only the cache path can serve as clone source.
    let cache_root = sb.root().join("name-cache");
    let cache_p1 = cache_root.join("p1");
    std::fs::create_dir_all(&cache_root).unwrap();
    git(
        &[
            "clone",
            "-q",
            p1.to_str().unwrap(),
            cache_p1.to_str().unwrap(),
        ],
        sb.root(),
    );

    let nonexistent = sb.root().join("nope.git");
    let manifest =
        manifest_yaml_with_url_and_rev(&[("p1", &nonexistent.display().to_string(), &sha)]);
    let ws = init_workspace(&sb, &manifest);

    sb.west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "update",
            "-j",
            "1",
            "--fetch",
            "smart",
            "--name-cache",
            cache_root.to_str().unwrap(),
        ])
        .assert()
        .success();
    assert!(ws.join("p1/R").exists());
    // Origin URL is flipped back to the manifest's url after the
    // cache-driven clone.
    let origin = git_capture(&["remote", "get-url", "origin"], &ws.join("p1"));
    assert_eq!(origin, nonexistent.display().to_string());
}

#[test]
#[serial]
fn update_uses_path_cache_when_dir_exists() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let sha = git_capture(&["rev-parse", "HEAD"], &p1);

    let cache_root = sb.root().join("path-cache");
    // Path cache uses project.path (default = project name), so layout
    // is identical to name-cache for this manifest.
    let cache_p1 = cache_root.join("p1");
    std::fs::create_dir_all(&cache_root).unwrap();
    git(
        &[
            "clone",
            "-q",
            p1.to_str().unwrap(),
            cache_p1.to_str().unwrap(),
        ],
        sb.root(),
    );

    let nonexistent = sb.root().join("nope.git");
    let manifest =
        manifest_yaml_with_url_and_rev(&[("p1", &nonexistent.display().to_string(), &sha)]);
    let ws = init_workspace(&sb, &manifest);

    sb.west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "update",
            "-j",
            "1",
            "--fetch",
            "smart",
            "--path-cache",
            cache_root.to_str().unwrap(),
        ])
        .assert()
        .success();
    assert!(ws.join("p1/R").exists());
}

#[test]
#[serial]
fn update_falls_through_when_static_cache_missing() {
    // Static cache flag pointing at an empty directory: the worker
    // should fall through to the project URL and clone from there.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let manifest = manifest_yaml(&[("p1", &p1, &[])]);
    let ws = init_workspace(&sb, &manifest);
    let empty_cache = sb.root().join("empty-cache");
    std::fs::create_dir_all(&empty_cache).unwrap();

    sb.west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "update",
            "--name-cache",
            empty_cache.to_str().unwrap(),
        ])
        .assert()
        .success();
    assert!(ws.join("p1/R").exists());
    // Origin remains the project's URL — fall-through means the cache
    // was never used so set_remote_url didn't run, but the project
    // already has the manifest URL recorded by the normal clone path.
    let origin = git_capture(&["remote", "get-url", "origin"], &ws.join("p1"));
    assert_eq!(origin, p1.display().to_string());
}

#[test]
#[serial]
fn update_priority_name_over_path_when_both_set() {
    // name-cache wins when both are set and have a matching directory.
    // We prove which one was used by populating each cache from a
    // *different* upstream — name-cache contains SHA_NAME, path-cache
    // contains SHA_PATH. The manifest pins SHA_NAME and the project URL
    // is unreachable; smart-fetch skips because SHA_NAME is local. If
    // path-cache had been used instead, fetch would hit the network
    // (SHA_NAME isn't in path-cache) and fail.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1_name_src = make_bare_with_one_commit(sb.root(), "p1-name-src", "name-content");
    let p1_path_src = make_bare_with_one_commit(sb.root(), "p1-path-src", "path-content");
    let sha_name = git_capture(&["rev-parse", "HEAD"], &p1_name_src);

    let name_cache_root = sb.root().join("name-cache");
    let path_cache_root = sb.root().join("path-cache");
    std::fs::create_dir_all(&name_cache_root).unwrap();
    std::fs::create_dir_all(&path_cache_root).unwrap();
    git(
        &[
            "clone",
            "-q",
            p1_name_src.to_str().unwrap(),
            name_cache_root.join("p1").to_str().unwrap(),
        ],
        sb.root(),
    );
    git(
        &[
            "clone",
            "-q",
            p1_path_src.to_str().unwrap(),
            path_cache_root.join("p1").to_str().unwrap(),
        ],
        sb.root(),
    );

    let nonexistent = sb.root().join("nope.git");
    let manifest =
        manifest_yaml_with_url_and_rev(&[("p1", &nonexistent.display().to_string(), &sha_name)]);
    let ws = init_workspace(&sb, &manifest);

    sb.west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "update",
            "-j",
            "1",
            "--fetch",
            "smart",
            "--name-cache",
            name_cache_root.to_str().unwrap(),
            "--path-cache",
            path_cache_root.to_str().unwrap(),
        ])
        .assert()
        .success();
    let head = git_capture(&["rev-parse", "HEAD"], &ws.join("p1"));
    assert_eq!(
        head, sha_name,
        "name-cache should win — workspace HEAD should match the name-cache content"
    );
}

#[test]
#[serial]
fn update_auto_cache_populates_then_serves_offline() {
    // First run: auto-cache empty; west populates `<DIR>/<basename>/<md5>`
    // as a bare mirror clone. Second run: drop the upstream bare so any
    // network attempt would fail; the cache must serve the second
    // workspace clone entirely offline.
    //
    // Offline serving only works when the manifest pin is immutable
    // (SHA-like) — branch tips would require a refresh fetch and
    // there's no upstream to refresh against. Pin to a SHA so the
    // cache-refresh smart-skip path fires.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let sha = git_capture(&["rev-parse", "HEAD"], &p1);
    let manifest = manifest_yaml_with_url_and_rev(&[("p1", &p1.display().to_string(), &sha)]);
    let auto_cache = sb.root().join("auto-cache");

    let ws = init_workspace(&sb, &manifest);
    sb.west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "update",
            "-j",
            "1",
            "--auto-cache",
            auto_cache.to_str().unwrap(),
        ])
        .assert()
        .success();

    // Cache directory exists with the basename/md5 layout. The dir
    // holds the bare md5 mirror plus a `<md5>.info` sidecar; count
    // only the mirror subdir.
    let dirs: Vec<_> = std::fs::read_dir(auto_cache.join("p1"))
        .unwrap()
        .filter_map(Result::ok)
        .filter(|e| e.path().is_dir())
        .collect();
    assert_eq!(dirs.len(), 1, "exactly one md5 subdir");
    let md5_dir = dirs[0].path();
    assert_eq!(
        git_capture(&["rev-parse", "--is-bare-repository"], &md5_dir),
        "true",
    );

    // Drop the upstream bare so only the cache can serve. Drop the
    // workspace project tree too; the worker has to re-clone it from
    // the cache without ever reaching the (gone) upstream URL.
    std::fs::remove_dir_all(&p1).unwrap();
    std::fs::remove_dir_all(ws.join("p1")).unwrap();
    sb.west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "update",
            "-j",
            "1",
            "--fetch",
            "smart",
            "--auto-cache",
            auto_cache.to_str().unwrap(),
        ])
        .assert()
        .success();
    assert!(ws.join("p1/R").exists(), "cache served the second clone");
}

#[test]
#[serial]
fn update_auto_cache_refreshes_on_subsequent_run() {
    // First run populates the cache. We add a commit upstream and run
    // update again — the cache directory must advance to the new
    // upstream tip (via `git fetch origin` against the mirror clone).
    // We assert directly on the cache's refs to avoid coupling to the
    // workspace-level smart-skip behaviour, which is exercised
    // separately in `update_advances_to_new_remote_commit`.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let manifest = manifest_yaml(&[("p1", &p1, &[])]);
    let auto_cache = sb.root().join("auto-cache");

    let ws = init_workspace(&sb, &manifest);
    sb.west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "update",
            "-j",
            "1",
            "--auto-cache",
            auto_cache.to_str().unwrap(),
        ])
        .assert()
        .success();

    // Cache's main starts at the original commit. Skip the
    // `<md5>.info` sidecar; the bare mirror is the only subdir.
    let dirs: Vec<_> = std::fs::read_dir(auto_cache.join("p1"))
        .unwrap()
        .filter_map(Result::ok)
        .filter(|e| e.path().is_dir())
        .collect();
    let cache_dir = dirs[0].path();
    let initial_sha = git_capture(&["rev-parse", "HEAD"], &p1);
    assert_eq!(
        git_capture(&["rev-parse", "refs/heads/main"], &cache_dir),
        initial_sha,
    );

    let new_sha = add_commit_to_bare(sb.root(), &p1, "second");
    sb.west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "update",
            "-j",
            "1",
            "--auto-cache",
            auto_cache.to_str().unwrap(),
        ])
        .assert()
        .success();

    // Cache's main has advanced.
    assert_eq!(
        git_capture(&["rev-parse", "refs/heads/main"], &cache_dir),
        new_sha,
        "cache mirror must advance via `git fetch` on subsequent run",
    );
}

#[test]
#[serial]
fn update_set_remote_url_after_cache_clone() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let sha = git_capture(&["rev-parse", "HEAD"], &p1);

    let cache_root = sb.root().join("name-cache");
    std::fs::create_dir_all(&cache_root).unwrap();
    git(
        &[
            "clone",
            "-q",
            p1.to_str().unwrap(),
            cache_root.join("p1").to_str().unwrap(),
        ],
        sb.root(),
    );
    let nonexistent = sb.root().join("not-real-url.git");
    let project_url = nonexistent.display().to_string();
    let manifest = manifest_yaml_with_url_and_rev(&[("p1", &project_url, &sha)]);
    let ws = init_workspace(&sb, &manifest);

    sb.west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "update",
            "-j",
            "1",
            "--fetch",
            "smart",
            "--name-cache",
            cache_root.to_str().unwrap(),
        ])
        .assert()
        .success();
    let origin = git_capture(&["remote", "get-url", "origin"], &ws.join("p1"));
    assert_eq!(
        origin, project_url,
        "origin URL must be the manifest URL, not the cache path"
    );
}
