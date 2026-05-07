//! Integration tests for `west_core::vcs`. Drives a real `git` binary
//! against tempdir-backed repositories. The whole module is skipped if
//! `git --version` doesn't run.

use std::path::{Path, PathBuf};
use std::process::Command;

use tempfile::TempDir;

use west_core::config::Configuration;
use west_core::vcs::{
    self, CheckoutTarget, FetchSpec, FetchStrategy, GitClient, GitOptions, Vcs, VcsError,
};

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

// ---------- new options on GitClient::from_config ----------

#[test]
fn from_config_reads_fetch_strategy() {
    let (_t, cfg) = config_with(
        r#"[tool.git.fetch]
strategy = "always"
"#,
    );
    let git = GitClient::from_config(&cfg).unwrap();
    let dbg = format!("{git:?}");
    assert!(dbg.contains("Always"), "got: {dbg}");
}

#[test]
fn from_config_default_fetch_strategy_is_smart() {
    let cfg = empty_config();
    let git = GitClient::from_config(&cfg).unwrap();
    let dbg = format!("{git:?}");
    assert!(dbg.contains("Smart"), "got: {dbg}");
}

#[test]
fn from_config_invalid_fetch_strategy_errors() {
    let (_t, cfg) = config_with(
        r#"[tool.git.fetch]
strategy = "fast"
"#,
    );
    let err = GitClient::from_config(&cfg).unwrap_err();
    assert!(matches!(err, VcsError::BadOption { ref key, .. } if key == "tool.git.fetch.strategy"));
}

#[test]
fn from_config_reads_fetch_tags_and_depth() {
    let (_t, cfg) = config_with(
        r#"[tool.git.fetch]
tags = false
depth = 5
"#,
    );
    let git = GitClient::from_config(&cfg).unwrap();
    let dbg = format!("{git:?}");
    assert!(dbg.contains("fetch_tags: Some(false)"), "got: {dbg}");
    assert!(dbg.contains("fetch_depth: Some(5)"), "got: {dbg}");
}

#[test]
fn from_config_negative_fetch_depth_errors() {
    let (_t, cfg) = config_with(
        r#"[tool.git.fetch]
depth = -1
"#,
    );
    let err = GitClient::from_config(&cfg).unwrap_err();
    assert!(matches!(err, VcsError::BadOption { ref key, .. } if key == "tool.git.fetch.depth"));
}

// ---------- fetch / checkout / is_clean / manifest-rev ----------

/// Create a clone of `bare` into `<root>/clone` and return the clone path.
fn clone_into(root: &Path, bare: &Path) -> PathBuf {
    let dest = root.join("clone");
    let v = GitClient::new(GitOptions::default());
    v.clone(bare.to_str().unwrap(), &dest, None, None).unwrap();
    dest
}

/// Add a new commit to a bare repo (round-trip via a temporary worktree).
fn add_commit_to_bare(root: &Path, bare: &Path, content: &str) -> String {
    let work = root.join("bare-work");
    git(&["clone", "-q", bare.to_str().unwrap(), "bare-work"], root);
    std::fs::write(work.join("R"), content.as_bytes()).unwrap();
    git(&["add", "."], &work);
    git(&["commit", "-q", "-am", content], &work);
    git(&["push", "-q"], &work);
    let sha = git_capture(&["rev-parse", "HEAD"], &work);
    let _ = std::fs::remove_dir_all(&work);
    sha
}

/// `git clone` doesn't create `FETCH_HEAD`; only `git fetch` does. So its
/// existence is a reliable signal that a real fetch happened.
fn fetch_head_present(repo: &Path) -> bool {
    repo.join(".git/FETCH_HEAD").exists()
}

#[test]
fn fetch_smart_skips_when_revision_is_local() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let bare = bare_source_with_one_commit(tmp.path());
    let dest = clone_into(tmp.path(), &bare);
    let local_head = git_capture(&["rev-parse", "HEAD"], &dest);
    assert!(!fetch_head_present(&dest), "no fetch yet");

    let v = GitClient::new(GitOptions {
        fetch_strategy: FetchStrategy::Smart,
        ..GitOptions::default()
    });
    let spec = FetchSpec {
        remote: "origin",
        revision: Some(&local_head),
    };
    v.fetch(&dest, &spec).unwrap();

    assert!(
        !fetch_head_present(&dest),
        "smart strategy should have skipped the fetch"
    );
}

#[test]
fn fetch_always_runs_even_when_revision_is_local() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let bare = bare_source_with_one_commit(tmp.path());
    let dest = clone_into(tmp.path(), &bare);
    let local_head = git_capture(&["rev-parse", "HEAD"], &dest);

    let v = GitClient::new(GitOptions {
        fetch_strategy: FetchStrategy::Always,
        ..GitOptions::default()
    });
    let spec = FetchSpec {
        remote: "origin",
        revision: Some(&local_head),
    };
    v.fetch(&dest, &spec).unwrap();

    assert!(
        fetch_head_present(&dest),
        "always strategy should have fetched"
    );
}

#[test]
fn fetch_smart_runs_when_revision_is_unknown() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let bare = bare_source_with_one_commit(tmp.path());
    let dest = clone_into(tmp.path(), &bare);

    // Add a new commit upstream — its sha can't possibly be local yet.
    let new_sha = add_commit_to_bare(tmp.path(), &bare, "second");

    let v = GitClient::new(GitOptions::default()); // smart by default
    let spec = FetchSpec {
        remote: "origin",
        revision: Some(&new_sha),
    };
    v.fetch(&dest, &spec).unwrap();

    // Smart strategy fell through to a real fetch; the new sha should now
    // be locally resolvable.
    assert_eq!(v.sha(&dest, &new_sha).unwrap(), new_sha);
}

#[test]
fn checkout_detached_lands_off_branch() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let bare = bare_source_with_one_commit(tmp.path());
    let dest = clone_into(tmp.path(), &bare);
    let head = git_capture(&["rev-parse", "HEAD"], &dest);

    let v = GitClient::new(GitOptions::default());
    v.checkout(&dest, &CheckoutTarget::Detached(&head)).unwrap();

    // Detached HEAD: symbolic-ref fails; HEAD still resolves.
    let sym = Command::new("git")
        .args(["-C", dest.to_str().unwrap(), "symbolic-ref", "-q", "HEAD"])
        .output()
        .unwrap();
    assert!(!sym.status.success(), "expected detached HEAD");
    assert_eq!(v.sha(&dest, "HEAD").unwrap(), head);
}

#[test]
fn checkout_branch_switches_to_named_branch() {
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

    let v = GitClient::new(GitOptions::default());
    v.checkout(&work, &CheckoutTarget::Branch("main")).unwrap();
    let cur = git_capture(&["rev-parse", "--abbrev-ref", "HEAD"], &work);
    assert_eq!(cur, "main");
}

#[test]
fn is_clean_distinguishes_clean_and_dirty() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let bare = bare_source_with_one_commit(tmp.path());
    let dest = clone_into(tmp.path(), &bare);
    let v = GitClient::new(GitOptions::default());
    assert!(v.is_clean(&dest).unwrap());

    // Modify a tracked file → dirty.
    std::fs::write(dest.join("README"), b"changed\n").unwrap();
    assert!(!v.is_clean(&dest).unwrap());
}

#[test]
fn manifest_rev_round_trip() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let bare = bare_source_with_one_commit(tmp.path());
    let dest = clone_into(tmp.path(), &bare);
    let head = git_capture(&["rev-parse", "HEAD"], &dest);

    let v = GitClient::new(GitOptions::default());
    assert!(v.manifest_rev(&dest).unwrap().is_none());

    v.set_manifest_rev(&dest, &head).unwrap();
    assert_eq!(
        v.manifest_rev(&dest).unwrap().as_deref(),
        Some(head.as_str())
    );

    // Confirm the underlying ref is the conventional location.
    let by_ref = git_capture(&["rev-parse", "refs/heads/manifest-rev"], &dest);
    assert_eq!(by_ref, head);
}
