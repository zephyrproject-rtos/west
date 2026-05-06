//! Integration tests for `west_core::vcs`. Drives a real `git` binary
//! against tempdir-backed repositories. The whole module is skipped if
//! `git --version` doesn't run.

use std::path::{Path, PathBuf};
use std::process::Command;

use tempfile::TempDir;

use west_core::config::Configuration;
use west_core::vcs::{self, GitClient, GitOptions, Vcs, VcsError};

// ---------- helpers ----------

fn git_available() -> bool {
    Command::new("git")
        .arg("--version")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

/// Run a raw git command (used by test setup). Panics on failure.
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
    assert!(out.status.success(), "git {args:?} failed: {:?}", out);
    String::from_utf8(out.stdout).unwrap().trim().to_owned()
}

/// Build a bare source repo at `<root>/source.git` containing one commit
/// on the default branch. Returns the path to the bare repo.
fn bare_source_with_one_commit(root: &Path) -> PathBuf {
    let work = root.join("work");
    std::fs::create_dir_all(&work).unwrap();
    git(&["init", "-q", "--initial-branch=main", "."], &work);
    std::fs::write(work.join("README"), b"hello\n").unwrap();
    git(&["add", "README"], &work);
    git(&["commit", "-q", "-m", "initial"], &work);

    let bare = root.join("source.git");
    git(&["clone", "-q", "--bare", "work", "source.git"], root);
    let _ = std::fs::remove_dir_all(&work);
    bare
}

fn empty_config() -> Configuration {
    Configuration::load(Vec::<PathBuf>::new()).unwrap()
}

fn config_with(toml_body: &str) -> (TempDir, Configuration) {
    let tmp = TempDir::new().unwrap();
    let p = tmp.path().join("config.toml");
    std::fs::write(&p, toml_body).unwrap();
    let cfg = Configuration::load([p]).unwrap();
    (tmp, cfg)
}

// ---------- selector ----------

#[test]
fn from_config_default_is_git() {
    let cfg = empty_config();
    let v = vcs::from_config(&cfg).unwrap();
    assert_eq!(v.name(), "git");
}

#[test]
fn from_config_unknown_client_errors() {
    let (_t, cfg) = config_with(
        r#"[vcs]
client = "fictional"
"#,
    );
    let err = vcs::from_config(&cfg).unwrap_err();
    assert!(matches!(err, VcsError::UnknownClient(name) if name == "fictional"),);
}

#[test]
fn from_config_reads_tool_git_binary() {
    let (_t, cfg) = config_with(
        r#"[tool.git]
binary = "/some/where/git"
"#,
    );
    let git = GitClient::from_config(&cfg).unwrap();
    // No public accessor; verify via Debug.
    let dbg = format!("{git:?}");
    assert!(dbg.contains("/some/where/git"), "got: {dbg}");
}

// ---------- Vcs ops (need a real git binary) ----------

#[test]
fn is_repo_true_on_real_repo() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    git(
        &["init", "-q", tmp.path().to_str().unwrap()],
        &std::env::current_dir().unwrap(),
    );

    let v = GitClient::new(GitOptions::default());
    assert!(v.is_repo(tmp.path()).unwrap());
}

#[test]
fn is_repo_false_on_empty_dir() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let v = GitClient::new(GitOptions::default());
    assert!(!v.is_repo(tmp.path()).unwrap());
}

#[test]
fn is_repo_false_on_nonexistent_path() {
    let tmp = TempDir::new().unwrap();
    let v = GitClient::new(GitOptions::default());
    assert!(!v.is_repo(&tmp.path().join("does-not-exist")).unwrap());
}

#[test]
fn clone_round_trip() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let bare = bare_source_with_one_commit(tmp.path());
    let dest = tmp.path().join("clone");

    let v = GitClient::new(GitOptions::default());
    v.clone(bare.to_str().unwrap(), &dest, None, None).unwrap();

    assert!(v.is_repo(&dest).unwrap());
    let head = v.sha(&dest, "HEAD").unwrap();
    let bare_head = git_capture(&["rev-parse", "HEAD"], &bare);
    assert_eq!(head, bare_head);
}

#[test]
fn clone_with_branch() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let work = tmp.path().join("work");
    std::fs::create_dir_all(&work).unwrap();
    git(&["init", "-q", "--initial-branch=main", "."], &work);
    std::fs::write(work.join("R"), b"a\n").unwrap();
    git(&["add", "."], &work);
    git(&["commit", "-q", "-m", "main"], &work);
    git(&["checkout", "-q", "-b", "feature"], &work);
    std::fs::write(work.join("R"), b"b\n").unwrap();
    git(&["commit", "-q", "-am", "feature"], &work);
    let bare = tmp.path().join("source.git");
    git(&["clone", "-q", "--bare", "work", "source.git"], tmp.path());

    let dest = tmp.path().join("clone");
    let v = GitClient::new(GitOptions::default());
    v.clone(bare.to_str().unwrap(), &dest, Some("feature"), None)
        .unwrap();

    let current = git_capture(&["rev-parse", "--abbrev-ref", "HEAD"], &dest);
    assert_eq!(current, "feature");
}

#[test]
fn clone_with_custom_origin() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let bare = bare_source_with_one_commit(tmp.path());
    let dest = tmp.path().join("clone");

    let v = GitClient::new(GitOptions::default());
    v.clone(bare.to_str().unwrap(), &dest, None, Some("upstream"))
        .unwrap();

    let url = git_capture(&["remote", "get-url", "upstream"], &dest);
    assert_eq!(url, bare.to_str().unwrap());
}

#[test]
fn sha_resolves_head_and_short_ref() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let bare = bare_source_with_one_commit(tmp.path());
    let dest = tmp.path().join("clone");
    let v = GitClient::new(GitOptions::default());
    v.clone(bare.to_str().unwrap(), &dest, None, None).unwrap();

    let head = v.sha(&dest, "HEAD").unwrap();
    assert_eq!(head.len(), 40);
    let head_short = &head[..7];
    let by_short = v.sha(&dest, head_short).unwrap();
    assert_eq!(by_short, head);
}

#[test]
fn is_ancestor_true_then_false() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let work = tmp.path().join("work");
    std::fs::create_dir_all(&work).unwrap();
    git(&["init", "-q", "--initial-branch=main", "."], &work);
    std::fs::write(work.join("R"), b"a\n").unwrap();
    git(&["add", "."], &work);
    git(&["commit", "-q", "-m", "A"], &work);
    let a = git_capture(&["rev-parse", "HEAD"], &work);
    std::fs::write(work.join("R"), b"b\n").unwrap();
    git(&["commit", "-q", "-am", "B"], &work);
    let b = git_capture(&["rev-parse", "HEAD"], &work);

    let v = GitClient::new(GitOptions::default());
    assert!(v.is_ancestor(&work, &a, &b).unwrap());
    assert!(!v.is_ancestor(&work, &b, &a).unwrap());
}

#[test]
fn command_failed_carries_stderr_and_argv() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let v = GitClient::new(GitOptions::default());
    // sha on a non-repo dir → CommandFailed.
    let err = v.sha(tmp.path(), "HEAD").unwrap_err();
    match err {
        VcsError::CommandFailed {
            client,
            argv,
            stderr,
            ..
        } => {
            assert_eq!(client, "git");
            assert!(argv.iter().any(|s| s == "rev-parse"));
            assert!(!stderr.is_empty(), "expected non-empty stderr");
        }
        other => panic!("expected CommandFailed, got {other:?}"),
    }
}
