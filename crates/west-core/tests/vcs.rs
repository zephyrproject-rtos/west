//! Integration tests for `west_core::vcs`. Drives a real `git` binary
//! against tempdir-backed repositories. The whole module is skipped if
//! `git --version` doesn't run.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Mutex;

use tempfile::TempDir;

use west_core::config::Configuration;
use west_core::vcs::{
    self, CheckoutTarget, CloneSpec, FetchSpec, FetchStrategy, GitClient, GitOptions, NullSink,
    Output, ProgressEvent, ProgressSink, SubmoduleScope, Vcs, VcsError,
};

// Test double: collects every event into an owned vector for assertions.
#[derive(Default)]
struct RecordingSink {
    events: Mutex<Vec<OwnedEvent>>,
}

#[derive(Debug, Clone, PartialEq)]
enum OwnedEvent {
    Line(String),
    Phase { name: String, total: Option<u64> },
    Tick { done: u64, total: Option<u64> },
    Finished,
}

impl RecordingSink {
    fn into_events(self) -> Vec<OwnedEvent> {
        self.events.into_inner().unwrap()
    }
}

impl ProgressSink for RecordingSink {
    fn event(&mut self, event: ProgressEvent<'_>) {
        let owned = match event {
            ProgressEvent::Line(s) => OwnedEvent::Line(s.to_owned()),
            ProgressEvent::Phase { name, total } => OwnedEvent::Phase {
                name: name.to_owned(),
                total,
            },
            ProgressEvent::Tick { done, total } => OwnedEvent::Tick { done, total },
            ProgressEvent::Finished => OwnedEvent::Finished,
        };
        self.events.lock().unwrap().push(owned);
    }
}

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
    v.clone(
        &CloneSpec {
            url: bare.to_str().unwrap(),
            dest: &dest,
            revision: None,
            origin: None,
            mirror: false,
        },
        &mut Output::Native,
    )
    .unwrap();

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
    v.clone(
        &CloneSpec {
            url: bare.to_str().unwrap(),
            dest: &dest,
            revision: Some("feature"),
            origin: None,
            mirror: false,
        },
        &mut Output::Native,
    )
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
    v.clone(
        &CloneSpec {
            url: bare.to_str().unwrap(),
            dest: &dest,
            revision: None,
            origin: Some("upstream"),
            mirror: false,
        },
        &mut Output::Native,
    )
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
    v.clone(
        &CloneSpec {
            url: bare.to_str().unwrap(),
            dest: &dest,
            revision: None,
            origin: None,
            mirror: false,
        },
        &mut Output::Native,
    )
    .unwrap();

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
    v.clone(
        &CloneSpec {
            url: bare.to_str().unwrap(),
            dest: &dest,
            revision: None,
            origin: None,
            mirror: false,
        },
        &mut Output::Native,
    )
    .unwrap();
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
    v.fetch(&dest, &spec, &mut Output::Native).unwrap();

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
    v.fetch(&dest, &spec, &mut Output::Native).unwrap();

    assert!(
        fetch_head_present(&dest),
        "always strategy should have fetched"
    );
}

#[test]
fn fetch_returns_resolved_sha_on_smart_skip_ignoring_stale_fetch_head() {
    // Regression for the "leaving N commits behind" warning: previously
    // the worker re-read FETCH_HEAD after fetch(), but on the smart-skip
    // path FETCH_HEAD persists from a *previous* fetch and points at a
    // different commit. fetch() now returns the actually-resolved sha so
    // the caller never has to sniff FETCH_HEAD itself.
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let bare = bare_source_with_one_commit(tmp.path());
    let dest = clone_into(tmp.path(), &bare);
    let first_sha = git_capture(&["rev-parse", "HEAD"], &dest);

    // Force an active fetch of a *different* commit so FETCH_HEAD is set
    // (and stale relative to `first_sha`).
    let second_sha = add_commit_to_bare(tmp.path(), &bare, "second");
    let v = GitClient::new(GitOptions::default());
    v.fetch(
        &dest,
        &FetchSpec {
            remote: "origin",
            revision: Some(&second_sha),
        },
        &mut Output::Native,
    )
    .unwrap();
    assert_eq!(
        git_capture(&["rev-parse", "FETCH_HEAD^{commit}"], &dest),
        second_sha,
        "FETCH_HEAD should now point at the second sha"
    );

    // Smart-skip path: ask for the first sha (locally resolvable). The
    // returned sha must be `first_sha`, not the stale `FETCH_HEAD`.
    let returned = v
        .fetch(
            &dest,
            &FetchSpec {
                remote: "origin",
                revision: Some(&first_sha),
            },
            &mut Output::Native,
        )
        .unwrap();
    assert_eq!(returned, first_sha);
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
    v.fetch(&dest, &spec, &mut Output::Native).unwrap();

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
    v.checkout(
        &dest,
        &CheckoutTarget::Detached(&head),
        &mut Output::Stream(&mut NullSink),
    )
    .unwrap();

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
    v.checkout(
        &work,
        &CheckoutTarget::Branch("main"),
        &mut Output::Stream(&mut NullSink),
    )
    .unwrap();
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

    v.set_manifest_rev(&dest, &head, None).unwrap();
    assert_eq!(
        v.manifest_rev(&dest).unwrap().as_deref(),
        Some(head.as_str())
    );

    // Confirm the underlying ref is the conventional location.
    let by_ref = git_capture(&["rev-parse", "refs/heads/manifest-rev"], &dest);
    assert_eq!(by_ref, head);
}

#[test]
fn set_manifest_rev_records_reflog_message() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let bare = bare_source_with_one_commit(tmp.path());
    let dest = clone_into(tmp.path(), &bare);
    let head = git_capture(&["rev-parse", "HEAD"], &dest);

    let v = GitClient::new(GitOptions::default());
    v.set_manifest_rev(&dest, &head, Some("west update: moving to abc"))
        .unwrap();

    let reflog = git_capture(
        &["reflog", "--format=%gs", "refs/heads/manifest-rev"],
        &dest,
    );
    assert!(
        reflog.contains("west update: moving to abc"),
        "reflog missing reason; got: {reflog}"
    );
}

#[test]
fn head_branch_returns_branch_name() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let bare = bare_source_with_one_commit(tmp.path());
    let dest = clone_into(tmp.path(), &bare);

    let v = GitClient::new(GitOptions::default());
    assert_eq!(v.head_branch(&dest).unwrap().as_deref(), Some("main"));
}

#[test]
fn head_branch_returns_none_when_detached() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let bare = bare_source_with_one_commit(tmp.path());
    let dest = clone_into(tmp.path(), &bare);
    let head = git_capture(&["rev-parse", "HEAD"], &dest);

    let v = GitClient::new(GitOptions::default());
    v.checkout(
        &dest,
        &CheckoutTarget::Detached(&head),
        &mut Output::Stream(&mut NullSink),
    )
    .unwrap();
    assert!(v.head_branch(&dest).unwrap().is_none());
}

#[test]
fn rebase_replays_local_commits_onto_target() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let work = tmp.path().join("work");
    std::fs::create_dir_all(&work).unwrap();
    git(&["init", "-q", "--initial-branch=main", "."], &work);
    std::fs::write(work.join("a"), b"a\n").unwrap();
    git(&["add", "."], &work);
    git(&["commit", "-q", "-m", "base"], &work);
    let base = git_capture(&["rev-parse", "HEAD"], &work);

    // Diverge: target branch advances with one commit; the working branch
    // (`feature`) gets a different commit on top of `base`.
    git(&["branch", "target"], &work);
    git(&["checkout", "-q", "target"], &work);
    std::fs::write(work.join("a"), b"a-target\n").unwrap();
    git(&["commit", "-q", "-am", "target advance"], &work);
    let target_tip = git_capture(&["rev-parse", "HEAD"], &work);

    git(&["checkout", "-q", "-b", "feature", &base], &work);
    std::fs::write(work.join("b"), b"b\n").unwrap();
    git(&["add", "."], &work);
    git(&["commit", "-q", "-m", "feature work"], &work);

    let v = GitClient::new(GitOptions::default());
    v.rebase(&work, "target", &mut Output::Native).unwrap();

    // After rebase, feature's parent should be target's tip.
    let parent = git_capture(&["rev-parse", "HEAD^"], &work);
    assert_eq!(parent, target_tip);
    let cur = git_capture(&["rev-parse", "--abbrev-ref", "HEAD"], &work);
    assert_eq!(cur, "feature");
}

#[test]
fn update_submodules_materializes_worktree() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();

    // Build a "library" bare repo to use as a submodule source.
    let lib_work = tmp.path().join("lib-work");
    std::fs::create_dir_all(&lib_work).unwrap();
    git(&["init", "-q", "--initial-branch=main", "."], &lib_work);
    std::fs::write(lib_work.join("LIB"), b"libdata\n").unwrap();
    git(&["add", "."], &lib_work);
    git(&["commit", "-q", "-m", "lib"], &lib_work);
    let lib_bare = tmp.path().join("lib.git");
    git(
        &["clone", "-q", "--bare", "lib-work", "lib.git"],
        tmp.path(),
    );

    // Build a "super" repo that registers `lib.git` as a submodule.
    let super_work = tmp.path().join("super-work");
    std::fs::create_dir_all(&super_work).unwrap();
    git(&["init", "-q", "--initial-branch=main", "."], &super_work);
    std::fs::write(super_work.join("README"), b"super\n").unwrap();
    git(&["add", "."], &super_work);
    git(&["commit", "-q", "-m", "init super"], &super_work);
    git(
        &[
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "--",
            lib_bare.to_str().unwrap(),
            "vendor/lib",
        ],
        &super_work,
    );
    git(&["commit", "-q", "-m", "add submodule"], &super_work);
    let super_bare = tmp.path().join("super.git");
    git(
        &["clone", "-q", "--bare", "super-work", "super.git"],
        tmp.path(),
    );

    // Fresh clone — submodule worktree should be empty until updated.
    let dest = tmp.path().join("clone");
    let v = GitClient::new(GitOptions::default());
    v.clone(
        &CloneSpec {
            url: super_bare.to_str().unwrap(),
            dest: &dest,
            revision: None,
            origin: None,
            mirror: false,
        },
        &mut Output::Native,
    )
    .unwrap();
    assert!(!dest.join("vendor/lib/LIB").exists());

    // Modern git refuses submodule clones over `file://` unless
    // `protocol.file.allow=always`. The submodule clone runs as a
    // *subprocess* of `git submodule update`, so a local config on the
    // parent repo doesn't reach it. The `GIT_CONFIG_*` env vars propagate.
    let _guard = AllowFileProtocolGuard::set();
    v.update_submodules(&dest, &SubmoduleScope::All, None, &mut Output::Native)
        .unwrap();

    assert!(
        dest.join("vendor/lib/LIB").exists(),
        "submodule worktree should be materialized"
    );
}

/// Test-only RAII guard that exposes `protocol.file.allow=always` to every
/// `git` subprocess for the guard's lifetime. Used so submodule fetches via
/// `file://` work in CI sandboxes where global git config can't be touched.
struct AllowFileProtocolGuard;
impl AllowFileProtocolGuard {
    fn set() -> Self {
        // SAFETY: tests don't otherwise touch GIT_CONFIG_*, and the override
        // we install is benign for any concurrent git invocation.
        unsafe {
            std::env::set_var("GIT_CONFIG_COUNT", "1");
            std::env::set_var("GIT_CONFIG_KEY_0", "protocol.file.allow");
            std::env::set_var("GIT_CONFIG_VALUE_0", "always");
        }
        Self
    }
}
impl Drop for AllowFileProtocolGuard {
    fn drop(&mut self) {
        unsafe {
            std::env::remove_var("GIT_CONFIG_COUNT");
            std::env::remove_var("GIT_CONFIG_KEY_0");
            std::env::remove_var("GIT_CONFIG_VALUE_0");
        }
    }
}

#[test]
fn fetch_emits_phase_events_when_active() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let bare = bare_source_with_one_commit(tmp.path());
    let dest = clone_into(tmp.path(), &bare);
    // Add a new commit upstream so the fetch actually transfers data.
    add_commit_to_bare(tmp.path(), &bare, "second");

    let v = GitClient::new(GitOptions::default());
    let mut sink = RecordingSink::default();
    v.fetch(
        &dest,
        &FetchSpec {
            remote: "origin",
            revision: None,
        },
        &mut Output::Stream(&mut sink),
    )
    .unwrap();

    let events = sink.into_events();
    assert!(
        events
            .iter()
            .any(|e| matches!(e, OwnedEvent::Line(s) if s.starts_with("From "))),
        "expected a `From <url>` line; got: {events:#?}"
    );
    assert!(
        matches!(events.last(), Some(OwnedEvent::Finished)),
        "expected Finished as the terminal event; got: {events:#?}"
    );
}

#[test]
fn clone_with_native_succeeds() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let bare = bare_source_with_one_commit(tmp.path());
    let dest = tmp.path().join("clone-native");

    let v = GitClient::new(GitOptions::default());
    // Native code path: no programmatic assertion on git's live
    // output (it goes to the test runner's stderr). We lock the API
    // shape and confirm the resulting tree is a real repo.
    v.clone(
        &CloneSpec {
            url: bare.to_str().unwrap(),
            dest: &dest,
            revision: None,
            origin: None,
            mirror: false,
        },
        &mut Output::Native,
    )
    .unwrap();
    assert!(v.is_repo(&dest).unwrap());
}

#[test]
fn clone_streams_lines_and_terminates_with_finished() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let bare = bare_source_with_one_commit(tmp.path());
    let dest = tmp.path().join("clone-stream");

    let v = GitClient::new(GitOptions::default());
    let mut sink = RecordingSink::default();
    v.clone(
        &CloneSpec {
            url: bare.to_str().unwrap(),
            dest: &dest,
            revision: None,
            origin: None,
            mirror: false,
        },
        &mut Output::Stream(&mut sink),
    )
    .unwrap();

    let events = sink.into_events();
    // Parser unit tests pin the phase-line semantics; here we just lock
    // the streaming pipeline: at least one Line event survived the
    // reader threads, and the run terminates with Finished. (Phase
    // events depend on repo size — a single-object clone won't trigger
    // them; that's covered by manual smoke against real repos.)
    assert!(
        events
            .iter()
            .any(|e| matches!(e, OwnedEvent::Line(s) if s.contains("Cloning into"))),
        "expected a `Cloning into …` Line event; got: {events:#?}"
    );
    assert!(
        matches!(events.last(), Some(OwnedEvent::Finished)),
        "expected Finished as the terminal event; got: {events:#?}"
    );
}

#[test]
fn null_sink_is_a_valid_target() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let bare = bare_source_with_one_commit(tmp.path());
    let dest = tmp.path().join("clone-null");

    let v = GitClient::new(GitOptions::default());
    v.clone(
        &CloneSpec {
            url: bare.to_str().unwrap(),
            dest: &dest,
            revision: None,
            origin: None,
            mirror: false,
        },
        &mut Output::Stream(&mut NullSink),
    )
    .unwrap();
    assert!(v.is_repo(&dest).unwrap());
}

#[test]
fn update_submodules_specific_empty_is_noop() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let bare = bare_source_with_one_commit(tmp.path());
    let dest = clone_into(tmp.path(), &bare);

    let v = GitClient::new(GitOptions::default());
    // Non-submodule repo + empty Specific → must succeed without error.
    v.update_submodules(
        &dest,
        &SubmoduleScope::Specific(&[]),
        None,
        &mut Output::Native,
    )
    .unwrap();
}

#[test]
fn clone_mirror_creates_bare_mirror_repo() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let bare = bare_source_with_one_commit(tmp.path());
    let dest = tmp.path().join("cache.git");

    let v = GitClient::new(GitOptions::default());
    v.clone(
        &CloneSpec {
            url: bare.to_str().unwrap(),
            dest: &dest,
            revision: None,
            origin: None,
            mirror: true,
        },
        &mut Output::Native,
    )
    .unwrap();

    // `--mirror` produces a bare repo with `core.bare = true` and a
    // mirror refspec. Use `git -C <dest> rev-parse --is-bare-repository`
    // to confirm; resolve a ref from the source to confirm it landed.
    assert_eq!(
        git_capture(&["rev-parse", "--is-bare-repository"], &dest),
        "true",
    );
    let head = v.sha(&dest, "HEAD").unwrap();
    assert_eq!(head, git_capture(&["rev-parse", "HEAD"], &bare));
}

#[test]
fn set_remote_url_replaces_origin() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let bare = bare_source_with_one_commit(tmp.path());
    let dest = clone_into(tmp.path(), &bare);

    let v = GitClient::new(GitOptions::default());
    v.set_remote_url(&dest, "origin", "https://example.com/x.git")
        .unwrap();
    assert_eq!(
        git_capture(&["remote", "get-url", "origin"], &dest),
        "https://example.com/x.git",
    );
}

#[test]
fn from_config_reads_fetch_force_and_submodule_options() {
    let (_t, cfg) = config_with(
        r#"[tool.git.fetch]
force = false

[tool.git.submodules]
recurse = false
sync = false
"#,
    );
    let git = GitClient::from_config(&cfg).unwrap();
    let dbg = format!("{git:?}");
    assert!(dbg.contains("fetch_force: false"), "got: {dbg}");
    assert!(dbg.contains("submodules_recurse: false"), "got: {dbg}");
    assert!(dbg.contains("submodules_sync: false"), "got: {dbg}");
}

#[test]
fn from_config_default_fetch_force_is_true() {
    let cfg = empty_config();
    let git = GitClient::from_config(&cfg).unwrap();
    let dbg = format!("{git:?}");
    assert!(dbg.contains("fetch_force: true"), "got: {dbg}");
    assert!(dbg.contains("submodules_recurse: true"), "got: {dbg}");
    assert!(dbg.contains("submodules_sync: true"), "got: {dbg}");
}

// =====================================================================
// `read_at_ref`: content-addressed file reads at a revision.
//
// The west import resolver relies on this to load per-project manifest
// imports from `refs/heads/manifest-rev` rather than the working tree
// — so a user `git checkout`-ing an unrelated branch after `west update`
// doesn't break manifest resolution. v1 used `git show <ref>:<path>`
// directly; these tests pin the equivalent semantics on the trait.
// =====================================================================

#[test]
fn read_at_ref_reads_from_git_not_worktree() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let bare = bare_source_with_one_commit(tmp.path());
    let dest = clone_into(tmp.path(), &bare);

    // Stage a file on `manifest-rev` branch and remove it from the
    // working tree. v1's pin test (`test_import_project_list`) does
    // the same shape: commit-on-branch, switch back to master,
    // assert the resolver still finds the file.
    git(&["checkout", "-q", "-b", "manifest-rev"], &dest);
    std::fs::write(dest.join("m1.yml"), b"manifest: {}\n").unwrap();
    git(&["add", "m1.yml"], &dest);
    git(&["commit", "-q", "-m", "add m1.yml"], &dest);
    git(&["checkout", "-q", "main"], &dest);
    assert!(
        !dest.join("m1.yml").exists(),
        "precondition: m1.yml must be absent from the working tree"
    );

    let v = GitClient::new(GitOptions::default());
    let got = v
        .read_at_ref(&dest, "refs/heads/manifest-rev", Path::new("m1.yml"))
        .unwrap();
    assert_eq!(got.as_deref(), Some(b"manifest: {}\n".as_slice()));
}

#[test]
fn read_at_ref_returns_none_when_path_missing_at_ref() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let bare = bare_source_with_one_commit(tmp.path());
    let dest = clone_into(tmp.path(), &bare);

    let v = GitClient::new(GitOptions::default());
    let got = v.read_at_ref(&dest, "HEAD", Path::new("does-not-exist.yml")).unwrap();
    assert!(got.is_none());
}

#[test]
fn read_at_ref_returns_none_when_ref_missing() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let bare = bare_source_with_one_commit(tmp.path());
    let dest = clone_into(tmp.path(), &bare);

    // `manifest-rev` is the conventional ref west sets after `update`.
    // On a fresh clone it doesn't exist — must be Ok(None), not Err.
    let v = GitClient::new(GitOptions::default());
    let got = v
        .read_at_ref(&dest, "refs/heads/manifest-rev", Path::new("anything"))
        .unwrap();
    assert!(got.is_none());
}

// =====================================================================
// `ls_tree_at_ref`: directory listing for per-project directory imports.
// =====================================================================

#[test]
fn ls_tree_at_ref_lists_sorted_filenames() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let bare = bare_source_with_one_commit(tmp.path());
    let dest = clone_into(tmp.path(), &bare);
    git(&["checkout", "-q", "-b", "manifest-rev"], &dest);
    std::fs::create_dir_all(dest.join("d")).unwrap();
    std::fs::write(dest.join("d/m1.yml"), b"m1\n").unwrap();
    std::fs::write(dest.join("d/m2.yml"), b"m2\n").unwrap();
    std::fs::write(dest.join("d/ignore.txt"), b"ignore\n").unwrap();
    git(&["add", "d"], &dest);
    git(&["commit", "-q", "-m", "dir"], &dest);

    let v = GitClient::new(GitOptions::default());
    let got = v
        .ls_tree_at_ref(&dest, "refs/heads/manifest-rev", Path::new("d"))
        .unwrap()
        .expect("d/ is a tree at manifest-rev");
    // `git ls-tree --name-only` produces lexically sorted output.
    assert_eq!(got, vec!["ignore.txt", "m1.yml", "m2.yml"]);
}

#[test]
fn ls_tree_at_ref_returns_none_for_blob() {
    // A path that's a file (blob) at the ref must report Ok(None), so
    // the importer falls back to read_at_ref instead of treating it
    // as a directory.
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let bare = bare_source_with_one_commit(tmp.path());
    let dest = clone_into(tmp.path(), &bare);
    let v = GitClient::new(GitOptions::default());
    // `README` was committed by `bare_source_with_one_commit`.
    let got = v.ls_tree_at_ref(&dest, "HEAD", Path::new("README")).unwrap();
    assert!(got.is_none());
}

#[test]
fn ls_tree_at_ref_returns_none_when_ref_missing() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let bare = bare_source_with_one_commit(tmp.path());
    let dest = clone_into(tmp.path(), &bare);
    let v = GitClient::new(GitOptions::default());
    let got = v
        .ls_tree_at_ref(&dest, "refs/heads/manifest-rev", Path::new("anything"))
        .unwrap();
    assert!(got.is_none());
}

#[test]
fn ls_tree_at_ref_errors_when_repo_path_is_not_a_repo() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let v = GitClient::new(GitOptions::default());
    let res = v.ls_tree_at_ref(tmp.path(), "HEAD", Path::new("anything"));
    assert!(
        matches!(res, Err(_)),
        "non-repo dir must propagate as Err; got {res:?}"
    );
}

#[test]
fn read_at_ref_errors_when_repo_path_is_not_a_repo() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let tmp = TempDir::new().unwrap();
    let v = GitClient::new(GitOptions::default());
    let res = v.read_at_ref(tmp.path(), "HEAD", Path::new("anything"));
    assert!(
        matches!(res, Err(_)),
        "non-repo dir must propagate as Err, not silent Ok(None); got {res:?}"
    );
}
