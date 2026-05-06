//! Integration tests for `west init`. Drives a real `git` binary against
//! tempdir-backed bare repositories and asserts on the resulting workspace.

use std::path::{Path, PathBuf};
use std::process::Command;

use assert_cmd::Command as AssertCmd;
use serial_test::serial;
use tempfile::TempDir;

const BIN: &str = "west";

// ---------- shared helpers ----------

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

/// Build a bare manifest repo with a single commit containing the given
/// `west.yml` body. Returns the path to the bare repo.
fn make_bare_manifest_repo(root: &Path, yaml: &str) -> PathBuf {
    make_bare_manifest_repo_named(root, "west.yml", yaml, "source.git")
}

fn make_bare_manifest_repo_named(
    root: &Path,
    yaml_filename: &str,
    yaml: &str,
    bare_name: &str,
) -> PathBuf {
    let work = root.join("work");
    std::fs::create_dir_all(&work).unwrap();
    git(&["init", "-q", "--initial-branch=main", "."], &work);
    std::fs::write(work.join(yaml_filename), yaml).unwrap();
    git(&["add", "."], &work);
    git(&["commit", "-q", "-m", "manifest"], &work);
    let bare = root.join(bare_name);
    git(
        &["clone", "-q", "--bare", "work", bare.to_str().unwrap()],
        root,
    );
    let _ = std::fs::remove_dir_all(&work);
    bare
}

/// Sandbox: tempdir with isolated WEST_CONFIG_* env vars.
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

fn read(path: &Path) -> String {
    std::fs::read_to_string(path).unwrap_or_default()
}

const SAMPLE_MANIFEST: &str = r#"manifest:
  self:
    path: my-manifest
  remotes:
    - name: example
      url-base: https://example.com
  projects:
    - name: dummy
      remote: example
"#;

const SAMPLE_MANIFEST_NO_SELF_PATH: &str = r#"manifest:
  remotes:
    - name: example
      url-base: https://example.com
  projects:
    - name: dummy
      remote: example
"#;

// ===========================================================================
// bootstrap mode
// ===========================================================================

#[test]
#[serial]
fn bootstrap_clones_and_writes_config() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let sb = Sandbox::new();
    let bare = make_bare_manifest_repo(sb.root(), SAMPLE_MANIFEST);
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

    let manifest_yml = workspace.join("my-manifest").join("west.yml");
    assert!(manifest_yml.exists(), "expected {}", manifest_yml.display());
    let cfg = read(&workspace.join(".west").join("config.toml"));
    assert!(cfg.contains(r#"path = "my-manifest""#), "got: {cfg}");
    assert!(cfg.contains(r#"file = "west.yml""#), "got: {cfg}");
}

#[test]
#[serial]
fn bootstrap_uses_url_basename_when_no_self_path() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let sb = Sandbox::new();
    let bare = make_bare_manifest_repo(sb.root(), SAMPLE_MANIFEST_NO_SELF_PATH);
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

    // Bare repo is named source.git -> basename "source".
    assert!(workspace.join("source").join("west.yml").exists());
    let cfg = read(&workspace.join(".west").join("config.toml"));
    assert!(cfg.contains(r#"path = "source""#), "got: {cfg}");
}

#[test]
#[serial]
fn bootstrap_honors_revision() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let sb = Sandbox::new();
    // Build a repo with two branches; main has the manifest, feature edits it.
    let work = sb.root().join("work");
    std::fs::create_dir_all(&work).unwrap();
    git(&["init", "-q", "--initial-branch=main", "."], &work);
    std::fs::write(work.join("west.yml"), SAMPLE_MANIFEST).unwrap();
    git(&["add", "."], &work);
    git(&["commit", "-q", "-m", "main"], &work);
    git(&["checkout", "-q", "-b", "feature"], &work);
    let feature_yaml = SAMPLE_MANIFEST.replace("my-manifest", "feature-manifest");
    std::fs::write(work.join("west.yml"), &feature_yaml).unwrap();
    git(&["commit", "-q", "-am", "feature"], &work);
    let bare = sb.root().join("source.git");
    git(
        &["clone", "-q", "--bare", "work", bare.to_str().unwrap()],
        sb.root(),
    );
    let _ = std::fs::remove_dir_all(&work);

    let workspace = sb.root().join("ws");
    sb.west()
        .args([
            "init",
            "--url",
            bare.to_str().unwrap(),
            "--revision",
            "feature",
            workspace.to_str().unwrap(),
        ])
        .assert()
        .success();

    // Feature branch's self.path is "feature-manifest".
    assert!(workspace.join("feature-manifest").join("west.yml").exists());
}

#[test]
#[serial]
fn bootstrap_honors_manifest_path_flag() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let sb = Sandbox::new();
    let bare = make_bare_manifest_repo(sb.root(), SAMPLE_MANIFEST_NO_SELF_PATH);
    let workspace = sb.root().join("ws");

    sb.west()
        .args([
            "init",
            "--url",
            bare.to_str().unwrap(),
            "--manifest-path",
            "custom/dir",
            workspace.to_str().unwrap(),
        ])
        .assert()
        .success();

    assert!(workspace.join("custom/dir").join("west.yml").exists());
    let cfg = read(&workspace.join(".west").join("config.toml"));
    assert!(cfg.contains(r#"path = "custom/dir""#), "got: {cfg}");
}

#[test]
#[serial]
fn bootstrap_warns_when_manifest_path_disagrees_with_self_path() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let sb = Sandbox::new();
    // YAML's self.path is "my-manifest"; user forces "custom".
    let bare = make_bare_manifest_repo(sb.root(), SAMPLE_MANIFEST);
    let workspace = sb.root().join("ws");

    let res = sb
        .west()
        .args([
            "init",
            "--url",
            bare.to_str().unwrap(),
            "--manifest-path",
            "custom",
            workspace.to_str().unwrap(),
        ])
        .assert()
        .success();

    let stderr = String::from_utf8_lossy(&res.get_output().stderr);
    assert!(stderr.contains("warning"), "stderr: {stderr}");
    assert!(stderr.contains("custom"), "stderr: {stderr}");
    assert!(stderr.contains("my-manifest"), "stderr: {stderr}");
    assert!(workspace.join("custom").join("west.yml").exists());
}

#[test]
#[serial]
fn bootstrap_honors_manifest_file_flag() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let sb = Sandbox::new();
    let bare = make_bare_manifest_repo_named(sb.root(), "alt.yml", SAMPLE_MANIFEST, "source.git");
    let workspace = sb.root().join("ws");

    sb.west()
        .args([
            "init",
            "--url",
            bare.to_str().unwrap(),
            "--manifest-file",
            "alt.yml",
            workspace.to_str().unwrap(),
        ])
        .assert()
        .success();

    assert!(workspace.join("my-manifest").join("alt.yml").exists());
    let cfg = read(&workspace.join(".west").join("config.toml"));
    assert!(cfg.contains(r#"file = "alt.yml""#), "got: {cfg}");
}

#[test]
#[serial]
fn bootstrap_via_top_level_config_flag() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let sb = Sandbox::new();
    let bare = make_bare_manifest_repo(sb.root(), SAMPLE_MANIFEST_NO_SELF_PATH);
    let workspace = sb.root().join("ws");

    sb.west()
        .args([
            "--config",
            "manifest.path=via-config",
            "init",
            "--url",
            bare.to_str().unwrap(),
            workspace.to_str().unwrap(),
        ])
        .assert()
        .success();

    assert!(workspace.join("via-config").join("west.yml").exists());
}

#[test]
#[serial]
fn bootstrap_dedicated_flag_overrides_top_level_config() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let sb = Sandbox::new();
    let bare = make_bare_manifest_repo(sb.root(), SAMPLE_MANIFEST_NO_SELF_PATH);
    let workspace = sb.root().join("ws");

    sb.west()
        .args([
            "--config",
            "manifest.path=via-config",
            "init",
            "--url",
            bare.to_str().unwrap(),
            "--manifest-path",
            "via-flag",
            workspace.to_str().unwrap(),
        ])
        .assert()
        .success();

    assert!(workspace.join("via-flag").join("west.yml").exists());
    assert!(!workspace.join("via-config").exists());
}

#[test]
#[serial]
fn bootstrap_requires_url() {
    let sb = Sandbox::new();
    let workspace = sb.root().join("ws");

    let res = sb
        .west()
        .args(["init", workspace.to_str().unwrap()])
        .assert()
        .failure();
    let stderr = String::from_utf8_lossy(&res.get_output().stderr);
    assert!(stderr.contains("--url"), "stderr: {stderr}");
}

#[test]
#[serial]
fn bootstrap_already_initialized_errors() {
    let sb = Sandbox::new();
    let workspace = sb.root().join("ws");
    std::fs::create_dir_all(workspace.join(".west")).unwrap();

    let res = sb
        .west()
        .args(["init", "--url", "ignored", workspace.to_str().unwrap()])
        .assert()
        .failure();
    let stderr = String::from_utf8_lossy(&res.get_output().stderr);
    assert!(stderr.contains("already"), "stderr: {stderr}");
}

#[test]
#[serial]
fn bootstrap_in_subdir_of_existing_workspace_errors() {
    let sb = Sandbox::new();
    let outer = sb.root().join("outer");
    std::fs::create_dir_all(outer.join(".west")).unwrap();
    let inner = outer.join("inner");

    let res = sb
        .west()
        .args(["init", "--url", "ignored", inner.to_str().unwrap()])
        .assert()
        .failure();
    let stderr = String::from_utf8_lossy(&res.get_output().stderr);
    assert!(stderr.contains("already"), "stderr: {stderr}");
}

#[test]
#[serial]
fn bootstrap_clone_failure_cleans_up_tempdir() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let sb = Sandbox::new();
    let workspace = sb.root().join("ws");
    let bogus = sb.root().join("does-not-exist");

    sb.west()
        .args([
            "init",
            "--url",
            bogus.to_str().unwrap(),
            workspace.to_str().unwrap(),
        ])
        .assert()
        .failure();

    // After failure the workspace should be free of `.west/` so the user can
    // retry.
    assert!(!workspace.join(".west").exists(), "expected .west/ removed");
}

// ===========================================================================
// local mode
// ===========================================================================

#[test]
#[serial]
fn local_registers_existing_checkout() {
    if !git_available() {
        eprintln!("skipping: git not installed");
        return;
    }
    let sb = Sandbox::new();
    let workspace = sb.root().join("ws");
    let manifest_dir = workspace.join("m");
    std::fs::create_dir_all(&manifest_dir).unwrap();
    git(&["init", "-q", "."], &manifest_dir);
    std::fs::write(manifest_dir.join("west.yml"), SAMPLE_MANIFEST).unwrap();

    sb.west()
        .args([
            "init",
            "--local",
            "--manifest-path",
            "m",
            workspace.to_str().unwrap(),
        ])
        .assert()
        .success();

    let cfg = read(&workspace.join(".west").join("config.toml"));
    assert!(cfg.contains(r#"path = "m""#), "got: {cfg}");
    assert!(cfg.contains(r#"file = "west.yml""#), "got: {cfg}");
}

#[test]
#[serial]
fn local_accepts_non_git_directory() {
    let sb = Sandbox::new();
    let workspace = sb.root().join("ws");
    let manifest_dir = workspace.join("m");
    std::fs::create_dir_all(&manifest_dir).unwrap();
    std::fs::write(manifest_dir.join("west.yml"), SAMPLE_MANIFEST).unwrap();

    sb.west()
        .args([
            "init",
            "--local",
            "--manifest-path",
            "m",
            workspace.to_str().unwrap(),
        ])
        .assert()
        .success();

    assert!(workspace.join(".west").join("config.toml").exists());
}

#[test]
#[serial]
fn local_requires_manifest_path() {
    let sb = Sandbox::new();
    let workspace = sb.root().join("ws");
    std::fs::create_dir_all(&workspace).unwrap();

    let res = sb
        .west()
        .args(["init", "--local", workspace.to_str().unwrap()])
        .assert()
        .failure();
    let stderr = String::from_utf8_lossy(&res.get_output().stderr);
    assert!(stderr.contains("manifest-path"), "stderr: {stderr}");
}

#[test]
#[serial]
fn local_missing_manifest_file_errors() {
    let sb = Sandbox::new();
    let workspace = sb.root().join("ws");
    std::fs::create_dir_all(workspace.join("m")).unwrap();
    // No west.yml file.

    let res = sb
        .west()
        .args([
            "init",
            "--local",
            "--manifest-path",
            "m",
            workspace.to_str().unwrap(),
        ])
        .assert()
        .failure();
    let stderr = String::from_utf8_lossy(&res.get_output().stderr);
    assert!(stderr.contains("not found"), "stderr: {stderr}");
}

#[test]
#[serial]
fn local_honors_top_level_config() {
    let sb = Sandbox::new();
    let workspace = sb.root().join("ws");
    let manifest_dir = workspace.join("m");
    std::fs::create_dir_all(&manifest_dir).unwrap();
    std::fs::write(manifest_dir.join("west.yml"), SAMPLE_MANIFEST).unwrap();

    sb.west()
        .args([
            "--config",
            "manifest.path=m",
            "init",
            "--local",
            workspace.to_str().unwrap(),
        ])
        .assert()
        .success();

    assert!(workspace.join(".west").join("config.toml").exists());
}

#[test]
#[serial]
fn local_workspace_already_initialized_errors() {
    let sb = Sandbox::new();
    let workspace = sb.root().join("ws");
    std::fs::create_dir_all(workspace.join(".west")).unwrap();

    let res = sb
        .west()
        .args([
            "init",
            "--local",
            "--manifest-path",
            "m",
            workspace.to_str().unwrap(),
        ])
        .assert()
        .failure();
    let stderr = String::from_utf8_lossy(&res.get_output().stderr);
    assert!(stderr.contains("already"), "stderr: {stderr}");
}

#[test]
#[serial]
fn local_in_subdir_of_existing_workspace_errors() {
    let sb = Sandbox::new();
    let outer = sb.root().join("outer");
    std::fs::create_dir_all(outer.join(".west")).unwrap();
    let inner = outer.join("inner");
    std::fs::create_dir_all(inner.join("m")).unwrap();
    std::fs::write(inner.join("m").join("west.yml"), SAMPLE_MANIFEST).unwrap();

    let res = sb
        .west()
        .args([
            "init",
            "--local",
            "--manifest-path",
            "m",
            inner.to_str().unwrap(),
        ])
        .assert()
        .failure();
    let stderr = String::from_utf8_lossy(&res.get_output().stderr);
    assert!(stderr.contains("already"), "stderr: {stderr}");
}
