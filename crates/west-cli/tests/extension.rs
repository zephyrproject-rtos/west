//! Integration tests for `west` extension command discovery and
//! dispatch. Sandbox + bare-repo fixture pattern mirroring
//! `tests/update.rs`. Tests spawn a real python3 interpreter; the
//! `west._dispatch` bridge is found via `PYTHONPATH` pointing at
//! the repo's `python/` directory (the maturin wheel build isn't
//! required for these tests).

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

fn python_available() -> bool {
    Command::new("python3")
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

/// Build a bare repo containing arbitrary file contents on `main`.
/// Returns the bare path.
fn make_bare_with_files(root: &Path, name: &str, files: &[(&str, &str)]) -> PathBuf {
    let work = root.join(format!("work-{name}"));
    std::fs::create_dir_all(&work).unwrap();
    git(&["init", "-q", "--initial-branch=main", "."], &work);
    for (path, body) in files {
        let full = work.join(path);
        if let Some(parent) = full.parent() {
            std::fs::create_dir_all(parent).unwrap();
        }
        std::fs::write(full, body.as_bytes()).unwrap();
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

/// Repo-root path to the python sources (the wheel's
/// `python-source` directory). Used as PYTHONPATH so the
/// spawned python3 finds `west._dispatch` without needing the
/// maturin wheel to be installed.
fn repo_python_dir() -> PathBuf {
    PathBuf::from(repo_root())
        .join("src")
        .canonicalize()
        .expect("src/")
}

fn repo_root() -> PathBuf {
    let manifest_dir = env!("CARGO_MANIFEST_DIR"); // crates/west-cli
    PathBuf::from(manifest_dir).join("..").join("..")
}

/// Path to the venv python (where pykwalify/pyyaml/colorama are
/// installed). The python `west` package's full surface imports
/// them at module load time, so a bare `python3` from PATH
/// wouldn't be enough. Tests force the venv via `WEST_PYTHON`.
///
/// Important: don't canonicalize. `.venv/bin/python` is a symlink
/// to a uv-managed canonical python; following the symlink gives
/// you that canonical python (no venv site-packages). Python's
/// venv detection keys off the EXECUTABLE PATH it was launched
/// with — the symlink-in-venv path makes it pick up the venv's
/// site-packages.
fn venv_python() -> Option<PathBuf> {
    let p = repo_root()
        .join(".venv")
        .join(if cfg!(windows) { "Scripts" } else { "bin" })
        .join(if cfg!(windows) {
            "python.exe"
        } else {
            "python"
        });
    p.exists().then_some(p)
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
            .env_remove("XDG_CONFIG_HOME")
            // PYTHONPATH points at the in-repo python sources so
            // `python3 -m west._dispatch` works without an
            // installed wheel.
            .env("PYTHONPATH", repo_python_dir());
        // If the project's venv has been set up, use its python —
        // the python `west` package imports colorama / pykwalify /
        // pyyaml at module load, and the system python3 may not
        // have those.
        if let Some(python) = venv_python() {
            c.env("WEST_PYTHON", python);
        }
        c
    }
}

/// Build the workspace's manifest repo (manifest.git) declaring
/// `self.path = my-manifest`, the projects from `projects`, and
/// optionally a `self.west-commands:` field. `manifest_workdir`
/// can carry extra files (typically the self project's
/// `west-commands.yml` + extension `.py`).
struct WorkspaceFixture<'a> {
    /// Manifest YAML body (caller-controlled).
    manifest_yaml: &'a str,
    /// Files to commit into the manifest repo alongside `west.yml`.
    extra_files: &'a [(&'a str, &'a str)],
}

fn init_workspace(sb: &Sandbox, fx: WorkspaceFixture<'_>) -> PathBuf {
    let manifest_work = sb.root().join("manifest-work");
    std::fs::create_dir_all(&manifest_work).unwrap();
    git(
        &["init", "-q", "--initial-branch=main", "."],
        &manifest_work,
    );
    std::fs::write(manifest_work.join("west.yml"), fx.manifest_yaml).unwrap();
    for (path, body) in fx.extra_files {
        let full = manifest_work.join(path);
        if let Some(parent) = full.parent() {
            std::fs::create_dir_all(parent).unwrap();
        }
        std::fs::write(full, body.as_bytes()).unwrap();
    }
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

/// Run `west update -j 1` so projects are cloned. Extensions are
/// only discoverable from cloned projects.
fn update_all(sb: &Sandbox, ws: &Path) {
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update", "-j", "1"])
        .assert()
        .success();
}

// ============================================================================
// Tests
// ============================================================================

/// Minimal extension python file used as a fixture. Subclasses
/// the bridge's WestCommand, writes a sentinel file the test
/// asserts on, and exits 0.
const EXT_PY: &str = r#"
import os
from west.commands import WestCommand

class Hello(WestCommand):
    def __init__(self):
        super().__init__(
            name='hello', description='say hi',
            requires_workspace=False,
        )
    def do_add_parser(self, parser_adder):
        return parser_adder.add_parser(self.name)
    def do_run(self, args, unknown):
        sentinel = os.environ['SENTINEL']
        with open(sentinel, 'w') as f:
            f.write(f'hello! topdir={self.topdir}\n')
"#;

#[test]
#[serial]
fn extension_unknown_command_errors() {
    if !git_available() || !python_available() {
        return;
    }
    let sb = Sandbox::new();
    // Workspace with no west-commands.yml anywhere.
    let manifest_yaml = "manifest:\n  self:\n    path: my-manifest\n  projects: []\n";
    let ws = init_workspace(
        &sb,
        WorkspaceFixture {
            manifest_yaml,
            extra_files: &[],
        },
    );

    let assert = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "nope"])
        .assert()
        .failure();
    let stderr = String::from_utf8_lossy(&assert.get_output().stderr);
    assert!(stderr.contains("unknown command: nope"), "stderr: {stderr}");
}

#[test]
#[serial]
fn extension_dispatches_to_python_in_self_project() {
    // `self.west-commands:` declares the extension. The yaml + python
    // file are committed in the manifest repo itself; the synthetic
    // manifest project's discovery picks them up.
    if !git_available() || !python_available() {
        return;
    }
    let sb = Sandbox::new();
    let yaml = "west-commands:\n  - file: scripts/hello.py\n    commands:\n      - name: hello\n        class: Hello\n";
    let manifest_yaml = "manifest:\n  self:\n    path: my-manifest\n    west-commands: scripts/wc.yml\n  projects: []\n";
    let ws = init_workspace(
        &sb,
        WorkspaceFixture {
            manifest_yaml,
            extra_files: &[("scripts/wc.yml", yaml), ("scripts/hello.py", EXT_PY)],
        },
    );

    let sentinel = sb.root().join("sentinel.txt");
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "hello"])
        .env("SENTINEL", &sentinel)
        .assert()
        .success();
    let body = std::fs::read_to_string(&sentinel).unwrap();
    assert!(body.contains("hello!"), "body: {body}");
    let canonical_ws = ws.canonicalize().unwrap();
    assert!(
        body.contains(&format!("topdir={}", canonical_ws.display())),
        "body: {body}"
    );
}

#[test]
#[serial]
fn extension_propagates_exit_code() {
    if !git_available() || !python_available() {
        return;
    }
    let ext_py = r#"
import sys
from west.commands import WestCommand, CommandError

class Boom(WestCommand):
    def __init__(self):
        super().__init__(name='boom', description='fail with a chosen code',
                         requires_workspace=False)
    def do_add_parser(self, parser_adder):
        return parser_adder.add_parser(self.name)
    def do_run(self, args, unknown):
        raise CommandError(returncode=42)
"#;
    let sb = Sandbox::new();
    let yaml = "west-commands:\n  - file: scripts/boom.py\n    commands:\n      - name: boom\n        class: Boom\n";
    let manifest_yaml = "manifest:\n  self:\n    path: my-manifest\n    west-commands: scripts/wc.yml\n  projects: []\n";
    let ws = init_workspace(
        &sb,
        WorkspaceFixture {
            manifest_yaml,
            extra_files: &[("scripts/wc.yml", yaml), ("scripts/boom.py", ext_py)],
        },
    );

    let assert = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "boom"])
        .assert()
        .failure();
    assert_eq!(assert.get_output().status.code(), Some(42));
}

#[test]
#[serial]
fn extension_receives_west_topdir_env() {
    if !git_available() || !python_available() {
        return;
    }
    let ext_py = r#"
import os, sys
from west.commands import WestCommand

class CheckEnv(WestCommand):
    def __init__(self):
        super().__init__(name='check-env', description='dump env',
                         requires_workspace=False)
    def do_add_parser(self, parser_adder):
        return parser_adder.add_parser(self.name)
    def do_run(self, args, unknown):
        with open(os.environ['SENTINEL'], 'w') as f:
            f.write(os.environ.get('WEST_TOPDIR', '<missing>'))
"#;
    let sb = Sandbox::new();
    let yaml = "west-commands:\n  - file: scripts/check.py\n    commands:\n      - name: check-env\n        class: CheckEnv\n";
    let manifest_yaml = "manifest:\n  self:\n    path: my-manifest\n    west-commands: scripts/wc.yml\n  projects: []\n";
    let ws = init_workspace(
        &sb,
        WorkspaceFixture {
            manifest_yaml,
            extra_files: &[("scripts/wc.yml", yaml), ("scripts/check.py", ext_py)],
        },
    );

    let sentinel = sb.root().join("env.txt");
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "check-env"])
        .env("SENTINEL", &sentinel)
        .assert()
        .success();
    let canonical_ws = ws.canonicalize().unwrap();
    assert_eq!(
        std::fs::read_to_string(&sentinel).unwrap(),
        canonical_ws.to_string_lossy()
    );
}

#[test]
#[serial]
fn extension_dispatches_via_project_yaml() {
    // Same shape as the self-project test, but declared on a real
    // project (rather than `self`). Exercises the
    // Manifest.projects iteration path.
    if !git_available() || !python_available() {
        return;
    }
    let sb = Sandbox::new();
    // p1 is a bare repo that contains scripts/wc.yml + scripts/hi.py.
    let yaml = "west-commands:\n  - file: scripts/hi.py\n    commands:\n      - name: hi\n        class: Hi\n";
    let ext_py = r#"
import os
from west.commands import WestCommand

class Hi(WestCommand):
    def __init__(self):
        super().__init__(name='hi', description='wave',
                         requires_workspace=False)
    def do_add_parser(self, parser_adder):
        return parser_adder.add_parser(self.name)
    def do_run(self, args, unknown):
        with open(os.environ['SENTINEL'], 'w') as f:
            f.write('hi from project')
"#;
    let p1 = make_bare_with_files(
        sb.root(),
        "p1",
        &[("scripts/wc.yml", yaml), ("scripts/hi.py", ext_py)],
    );

    let manifest_yaml = format!(
        "manifest:\n  self:\n    path: my-manifest\n  projects:\n    - name: p1\n      url: {}\n      revision: main\n      west-commands: scripts/wc.yml\n",
        p1.display()
    );
    let ws = init_workspace(
        &sb,
        WorkspaceFixture {
            manifest_yaml: &manifest_yaml,
            extra_files: &[],
        },
    );
    update_all(&sb, &ws);

    let sentinel = sb.root().join("hi.txt");
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "hi"])
        .env("SENTINEL", &sentinel)
        .assert()
        .success();
    assert_eq!(
        std::fs::read_to_string(&sentinel).unwrap(),
        "hi from project"
    );
}
