//! Integration tests for `west forall`. Sandbox + bare-repo fixture
//! pattern mirroring `tests/update.rs` and `tests/list.rs`. Same
//! boilerplate is now in three test files (the architectural review
//! flagged extraction as overdue — handled in a separate refactor).

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

/// Build a manifest YAML (optional `group-filter:` and per-project groups).
fn manifest_yaml(group_filter: Option<&str>, projects: &[(&str, &Path, &[&str])]) -> String {
    let mut s = String::from("manifest:\n");
    if let Some(gf) = group_filter {
        s.push_str(&format!("  group-filter: [{gf}]\n"));
    }
    s.push_str("  self:\n    path: my-manifest\n  projects:\n");
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

/// Run `west update -j 1` so projects are cloned. Forall only operates
/// on cloned projects, so most tests need this between init + forall.
fn update_all(sb: &Sandbox, ws: &Path) {
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update", "-j", "1"])
        .assert()
        .success();
}

fn stdout_lines(out: &[u8]) -> Vec<String> {
    String::from_utf8_lossy(out)
        .lines()
        .map(|l| l.to_owned())
        .collect()
}

// ============================================================================
// Tests
// ============================================================================

#[test]
#[serial]
fn forall_runs_command_in_each_active_project() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let p2 = make_bare_with_one_commit(sb.root(), "p2", "p2");
    let manifest = manifest_yaml(Some("-noisy"), &[("p1", &p1, &[]), ("p2", &p2, &["noisy"])]);
    let ws = init_workspace(&sb, &manifest);
    update_all(&sb, &ws);

    // Each project's command writes its name into `out`. Synthetic
    // manifest project gets one too — its `cwd` is the manifest repo.
    sb.west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "forall",
            "-j",
            "1",
            "-c",
            "echo $WEST_PROJECT_NAME > out",
        ])
        .assert()
        .success();
    assert_eq!(
        std::fs::read_to_string(ws.join("p1/out")).unwrap().trim(),
        "p1"
    );
    assert!(!ws.join("p2/out").exists(), "inactive p2 should be skipped");
    assert_eq!(
        std::fs::read_to_string(ws.join("my-manifest/out"))
            .unwrap()
            .trim(),
        "manifest"
    );
}

#[test]
#[serial]
fn forall_includes_inactive_with_all() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let p2 = make_bare_with_one_commit(sb.root(), "p2", "p2");
    let manifest = manifest_yaml(Some("-noisy"), &[("p1", &p1, &[]), ("p2", &p2, &["noisy"])]);
    let ws = init_workspace(&sb, &manifest);
    // -a propagates to update so the inactive project gets cloned too.
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update", "-j", "1", "p2"])
        .assert()
        .success();
    update_all(&sb, &ws);

    sb.west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "forall",
            "-j",
            "1",
            "-a",
            "-c",
            "touch out",
        ])
        .assert()
        .success();
    assert!(ws.join("p1/out").exists());
    assert!(ws.join("p2/out").exists(), "--all should include p2");
}

#[test]
#[serial]
fn forall_group_filter_or_logic() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let pa = make_bare_with_one_commit(sb.root(), "pa", "pa");
    let pb = make_bare_with_one_commit(sb.root(), "pb", "pb");
    let pc = make_bare_with_one_commit(sb.root(), "pc", "pc");
    let manifest = manifest_yaml(
        None,
        &[
            ("pa", &pa, &["a"]),
            ("pb", &pb, &["b"]),
            ("pc", &pc, &["c"]),
        ],
    );
    let ws = init_workspace(&sb, &manifest);
    update_all(&sb, &ws);

    sb.west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "forall",
            "-j",
            "1",
            "-g",
            "a",
            "-g",
            "b",
            "-c",
            "touch out",
        ])
        .assert()
        .success();
    assert!(ws.join("pa/out").exists());
    assert!(ws.join("pb/out").exists());
    assert!(!ws.join("pc/out").exists(), "pc not in group a or b");
    // Synthetic has empty groups → never matches `-g`.
    assert!(!ws.join("my-manifest/out").exists());
}

#[test]
#[serial]
fn forall_positional_filters_to_named_project() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let p2 = make_bare_with_one_commit(sb.root(), "p2", "p2");
    let manifest = manifest_yaml(None, &[("p1", &p1, &[]), ("p2", &p2, &[])]);
    let ws = init_workspace(&sb, &manifest);
    update_all(&sb, &ws);

    sb.west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "forall",
            "-j",
            "1",
            "-c",
            "touch out",
            "p1",
        ])
        .assert()
        .success();
    assert!(ws.join("p1/out").exists());
    assert!(!ws.join("p2/out").exists());
    assert!(!ws.join("my-manifest/out").exists());
}

#[test]
#[serial]
fn forall_positional_uncloned_errors() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let manifest = manifest_yaml(None, &[("p1", &p1, &[])]);
    let ws = init_workspace(&sb, &manifest);
    // Don't run update — p1 stays uncloned.

    let assert = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "forall",
            "-j",
            "1",
            "-c",
            "true",
            "p1",
        ])
        .assert()
        .failure();
    let stderr = String::from_utf8_lossy(&assert.get_output().stderr).into_owned();
    assert!(
        stderr.contains("uncloned") && stderr.contains("p1"),
        "expected an uncloned-project message; got: {stderr:?}"
    );
}

#[test]
#[serial]
fn forall_env_vars_set() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let manifest = manifest_yaml(None, &[("p1", &p1, &[])]);
    let ws = init_workspace(&sb, &manifest);
    update_all(&sb, &ws);

    // Dump every env var to a sentinel file with `key=value` lines.
    sb.west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "forall",
            "-j",
            "1",
            "-c",
            "{ echo NAME=$WEST_PROJECT_NAME; echo PATH=$WEST_PROJECT_PATH; \
              echo ABSPATH=$WEST_PROJECT_ABSPATH; echo REVISION=$WEST_PROJECT_REVISION; \
              echo URL=$WEST_PROJECT_URL; echo REMOTE=$WEST_PROJECT_REMOTE; } > env.out",
            "p1",
        ])
        .assert()
        .success();
    let body = std::fs::read_to_string(ws.join("p1/env.out")).unwrap();
    assert!(body.contains("NAME=p1"), "got: {body}");
    assert!(body.contains("PATH=p1"), "got: {body}");
    // ABSPATH is computed from `topdir(cwd)`, which canonicalises on
    // macOS (`/var` → `/private/var`); compare via canonicalize on
    // both sides.
    let expected_abs = ws.join("p1").canonicalize().unwrap();
    assert!(
        body.contains(&format!("ABSPATH={}", expected_abs.display())),
        "got: {body}"
    );
    assert!(body.contains("REVISION=main"), "got: {body}");
    assert!(
        body.contains(&format!("URL={}", p1.display())),
        "got: {body}"
    );
    assert!(body.contains("REMOTE=origin"), "got: {body}");
}

#[test]
#[serial]
fn forall_cwd_override() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let manifest = manifest_yaml(None, &[("p1", &p1, &[])]);
    let ws = init_workspace(&sb, &manifest);
    update_all(&sb, &ws);

    let stage = sb.root().join("stage");
    std::fs::create_dir_all(&stage).unwrap();

    // -C should override per-project cwd. Each project's command
    // writes its name into `<stage>/<NAME>` — proves cwd is `stage`,
    // not the project tree.
    sb.west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "forall",
            "-j",
            "1",
            "-C",
            stage.to_str().unwrap(),
            "-c",
            "touch $WEST_PROJECT_NAME",
            "p1",
        ])
        .assert()
        .success();
    assert!(stage.join("p1").exists(), "command should run in -C dir");
    assert!(!ws.join("p1/p1").exists(), "should not have run in p1 tree");
}

#[test]
#[serial]
fn forall_aggregates_failures() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let p2 = make_bare_with_one_commit(sb.root(), "p2", "p2");
    let manifest = manifest_yaml(None, &[("p1", &p1, &[]), ("p2", &p2, &[])]);
    let ws = init_workspace(&sb, &manifest);
    update_all(&sb, &ws);

    // Fail in p2; succeed elsewhere. Continue + non-zero exit.
    let assert = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "forall",
            "-j",
            "1",
            "-c",
            "[ \"$WEST_PROJECT_NAME\" != p2 ]",
        ])
        .assert()
        .failure();
    let stderr = String::from_utf8_lossy(&assert.get_output().stderr).into_owned();
    assert!(
        stderr.contains("forall failed for 1 project: p2"),
        "expected aggregate failure mentioning p2; got: {stderr:?}"
    );
}

#[test]
#[serial]
fn forall_synthetic_manifest_included() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let manifest = manifest_yaml(None, &[("p1", &p1, &[])]);
    let ws = init_workspace(&sb, &manifest);
    update_all(&sb, &ws);

    // Default empty-positional path should iterate over the synthetic
    // manifest project too.
    sb.west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "forall",
            "-j",
            "1",
            "-c",
            "echo $WEST_PROJECT_NAME > out",
        ])
        .assert()
        .success();
    assert_eq!(
        std::fs::read_to_string(ws.join("my-manifest/out"))
            .unwrap()
            .trim(),
        "manifest",
    );

    // Positional `manifest` must also resolve to the synthetic.
    sb.west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "forall",
            "-j",
            "1",
            "-c",
            "touch from-positional",
            "manifest",
        ])
        .assert()
        .success();
    assert!(ws.join("my-manifest/from-positional").exists());
}

#[test]
#[serial]
fn forall_banner_to_stderr_command_output_to_stdout() {
    // The `=== running …` banner is chrome and must land on stderr;
    // the command's own stdout must stay clean on stdout so a redirect
    // captures only the command output.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let manifest = manifest_yaml(None, &[("p1", &p1, &[])]);
    let ws = init_workspace(&sb, &manifest);
    update_all(&sb, &ws);

    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "forall",
            "-j",
            "1",
            "-c",
            "echo COMMAND-STDOUT",
            "p1",
        ])
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(&out.get_output().stdout).into_owned();
    let stderr = String::from_utf8_lossy(&out.get_output().stderr).into_owned();
    assert!(
        stdout.contains("COMMAND-STDOUT"),
        "command output missing from stdout: {stdout:?}"
    );
    assert!(
        !stdout.contains("=== running"),
        "banner leaked onto stdout: {stdout:?}"
    );
    assert!(
        stderr.contains("=== running") && stderr.contains("p1"),
        "banner missing from stderr: {stderr:?}"
    );
}

#[test]
#[serial]
fn forall_parallel_buffers_output_per_project() {
    // -j 2 with two projects each writing 50 numbered lines: the
    // captured stdout must show each project's lines in a contiguous
    // block (no interleaving). The `=== running` banner is chrome and
    // goes to stderr; only the commands' own stdout reaches stdout, so
    // this contiguity check sees just the `name-NN` body lines.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let p1 = make_bare_with_one_commit(sb.root(), "p1", "p1");
    let p2 = make_bare_with_one_commit(sb.root(), "p2", "p2");
    let manifest = manifest_yaml(None, &[("p1", &p1, &[]), ("p2", &p2, &[])]);
    let ws = init_workspace(&sb, &manifest);
    update_all(&sb, &ws);

    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "forall",
            "-j",
            "2",
            // Each project echoes 50 distinguishable lines with its
            // name as prefix; if outputs interleave the `name-NN`
            // sequences would tangle.
            "-c",
            "for i in $(seq 1 50); do echo \"$WEST_PROJECT_NAME-$i\"; done",
            "p1",
            "p2",
        ])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let lines = stdout_lines(&out);
    // Find the indices where p1's numbered lines appear; they must be
    // strictly contiguous (no `p2-N` lines between any two `p1-N`).
    fn contiguous(lines: &[String], prefix: &str) -> bool {
        let positions: Vec<usize> = lines
            .iter()
            .enumerate()
            .filter(|(_, l)| l.starts_with(prefix))
            .map(|(i, _)| i)
            .collect();
        positions.len() == 50 && positions.windows(2).all(|w| w[1] == w[0] + 1)
    }
    assert!(
        contiguous(&lines, "p1-"),
        "p1 output not contiguous: {lines:#?}"
    );
    assert!(
        contiguous(&lines, "p2-"),
        "p2 output not contiguous: {lines:#?}"
    );
}
