//! Integration tests for `west config get/set/unset/list`.
//!
//! Each test uses a tempdir for the workspace plus tempfile paths for the
//! global/system layers, wired in via `WEST_CONFIG_*` env vars. Tests are
//! serialized because env vars are process-wide.

use std::path::Path;

use assert_cmd::Command;
use serial_test::serial;
use tempfile::TempDir;

const BIN: &str = "west";

struct Sandbox {
    _tmp: TempDir,
    workspace: std::path::PathBuf,
    local: std::path::PathBuf,
    global: std::path::PathBuf,
    system: std::path::PathBuf,
}

impl Sandbox {
    fn new() -> Self {
        let tmp = TempDir::new().unwrap();
        let workspace = tmp.path().join("ws");
        std::fs::create_dir_all(workspace.join(".west")).unwrap();
        let local = workspace.join(".west").join("config.toml");
        let global = tmp.path().join("glob.toml");
        let system = tmp.path().join("sys.toml");
        Sandbox {
            _tmp: tmp,
            workspace,
            local,
            global,
            system,
        }
    }

    fn west(&self) -> Command {
        let mut c = Command::cargo_bin(BIN).unwrap();
        c.current_dir(&self.workspace)
            .env("WEST_CONFIG_LOCAL", &self.local)
            .env("WEST_CONFIG_GLOBAL", &self.global)
            .env("WEST_CONFIG_SYSTEM", &self.system)
            .env_remove("XDG_CONFIG_HOME");
        c
    }

    fn west_outside_workspace(&self) -> Command {
        // Run from the tempdir root, not the workspace dir; west's topdir
        // resolver won't find a `.west` here. We also unset WEST_CONFIG_LOCAL
        // so there is genuinely no local layer.
        let mut c = Command::cargo_bin(BIN).unwrap();
        c.current_dir(self._tmp.path())
            .env_remove("WEST_CONFIG_LOCAL")
            .env("WEST_CONFIG_GLOBAL", &self.global)
            .env("WEST_CONFIG_SYSTEM", &self.system)
            .env_remove("XDG_CONFIG_HOME");
        c
    }
}

fn read(path: &Path) -> String {
    std::fs::read_to_string(path).unwrap_or_default()
}

#[test]
#[serial]
fn set_get_round_trip_local_default() {
    let sb = Sandbox::new();
    sb.west()
        .args(["config", "set", "manifest.path", "zephyr"])
        .assert()
        .success();

    assert!(read(&sb.local).contains(r#"path = "zephyr""#));

    let out = sb
        .west()
        .args(["config", "get", "manifest.path"])
        .assert()
        .success();
    assert_eq!(
        std::str::from_utf8(&out.get_output().stdout).unwrap(),
        "zephyr\n"
    );
}

#[test]
#[serial]
fn set_get_round_trip_each_explicit_scope() {
    let sb = Sandbox::new();
    for scope in ["--system", "--global", "--local"] {
        let value = format!("at-{}", scope.trim_start_matches("--"));
        sb.west()
            .args(["config", "set", scope, "k.v", &value])
            .assert()
            .success();
        let out = sb
            .west()
            .args(["config", "get", scope, "k.v"])
            .assert()
            .success();
        assert_eq!(
            std::str::from_utf8(&out.get_output().stdout).unwrap(),
            format!("{value}\n")
        );
    }
}

#[test]
#[serial]
fn set_via_file_flag_writes_to_arbitrary_path() {
    let sb = Sandbox::new();
    let extra = sb._tmp.path().join("extra.toml");
    sb.west()
        .args([
            "config",
            "set",
            "--file",
            extra.to_str().unwrap(),
            "k.v",
            "hello",
        ])
        .assert()
        .success();
    assert!(read(&extra).contains(r#"v = "hello""#));
}

#[test]
#[serial]
fn set_typed_int_writes_native_toml_integer() {
    let sb = Sandbox::new();
    sb.west()
        .args(["config", "set", "--type", "int", "n.v", "42"])
        .assert()
        .success();
    let body = read(&sb.local);
    assert!(body.contains("v = 42"), "got: {body}");
    assert!(!body.contains(r#"v = "42""#), "should not be quoted: {body}");

    let out = sb
        .west()
        .args(["config", "get", "n.v"])
        .assert()
        .success();
    assert_eq!(
        std::str::from_utf8(&out.get_output().stdout).unwrap(),
        "42\n"
    );
}

#[test]
#[serial]
fn set_typed_bool_accepts_python_set_and_rejects_garbage() {
    let sb = Sandbox::new();
    sb.west()
        .args(["config", "set", "--type", "bool", "b.v", "yes"])
        .assert()
        .success();
    let out = sb
        .west()
        .args(["config", "get", "b.v"])
        .assert()
        .success();
    assert_eq!(
        std::str::from_utf8(&out.get_output().stdout).unwrap(),
        "true\n"
    );

    // Garbage rejected with exit 2.
    let res = sb
        .west()
        .args(["config", "set", "--type", "bool", "b.v", "maybe"])
        .assert()
        .failure();
    assert_eq!(res.get_output().status.code(), Some(2));
}

#[test]
#[serial]
fn set_list_writes_toml_array_and_get_prints_one_per_line() {
    let sb = Sandbox::new();
    sb.west()
        .args([
            "config",
            "set",
            "--list",
            "manifest.project-filter",
            "+foo",
            "-bar",
            "baz",
        ])
        .assert()
        .success();
    assert!(
        read(&sb.local).contains(r#"project-filter = ["+foo", "-bar", "baz"]"#)
    );

    let out = sb
        .west()
        .args(["config", "get", "manifest.project-filter"])
        .assert()
        .success();
    assert_eq!(
        std::str::from_utf8(&out.get_output().stdout).unwrap(),
        "+foo\n-bar\nbaz\n"
    );
}

#[test]
#[serial]
fn set_default_local_outside_workspace_fails_with_exit_3() {
    let sb = Sandbox::new();
    let res = sb
        .west_outside_workspace()
        .args(["config", "set", "k.v", "hello"])
        .assert()
        .failure();
    assert_eq!(res.get_output().status.code(), Some(3));
    let stderr = String::from_utf8_lossy(&res.get_output().stderr);
    assert!(stderr.contains("workspace"), "stderr: {stderr}");
}

#[test]
#[serial]
fn unset_default_removes_topmost_then_next() {
    let sb = Sandbox::new();
    sb.west()
        .args(["config", "set", "--global", "k.v", "global-val"])
        .assert()
        .success();
    sb.west()
        .args(["config", "set", "--local", "k.v", "local-val"])
        .assert()
        .success();

    sb.west().args(["config", "unset", "k.v"]).assert().success();
    let out = sb
        .west()
        .args(["config", "get", "k.v"])
        .assert()
        .success();
    assert_eq!(
        std::str::from_utf8(&out.get_output().stdout).unwrap(),
        "global-val\n"
    );

    sb.west().args(["config", "unset", "k.v"]).assert().success();
    let res = sb
        .west()
        .args(["config", "get", "k.v"])
        .assert()
        .failure();
    assert_eq!(res.get_output().status.code(), Some(1));
}

#[test]
#[serial]
fn unset_scoped_only_touches_that_scope() {
    let sb = Sandbox::new();
    sb.west()
        .args(["config", "set", "--global", "k.v", "G"])
        .assert()
        .success();
    sb.west()
        .args(["config", "set", "--local", "k.v", "L"])
        .assert()
        .success();

    sb.west()
        .args(["config", "unset", "--global", "k.v"])
        .assert()
        .success();

    let out = sb
        .west()
        .args(["config", "get", "k.v"])
        .assert()
        .success();
    assert_eq!(
        std::str::from_utf8(&out.get_output().stdout).unwrap(),
        "L\n"
    );
}

#[test]
#[serial]
fn unset_missing_key_exits_1() {
    let sb = Sandbox::new();
    let res = sb
        .west()
        .args(["config", "unset", "nope.key"])
        .assert()
        .failure();
    assert_eq!(res.get_output().status.code(), Some(1));
}

#[test]
#[serial]
fn list_shows_merged_view_with_lists_expanded() {
    let sb = Sandbox::new();
    sb.west()
        .args(["config", "set", "--global", "manifest.path", "lower"])
        .assert()
        .success();
    sb.west()
        .args(["config", "set", "--local", "manifest.path", "upper"])
        .assert()
        .success();
    sb.west()
        .args([
            "config",
            "set",
            "--local",
            "--list",
            "manifest.project-filter",
            "+a",
            "-b",
        ])
        .assert()
        .success();

    let out = sb
        .west()
        .args(["config", "list"])
        .assert()
        .success();
    let stdout = std::str::from_utf8(&out.get_output().stdout).unwrap();
    assert!(stdout.contains("manifest.path=upper"), "got: {stdout}");
    assert!(stdout.contains("manifest.project-filter=+a"), "got: {stdout}");
    assert!(stdout.contains("manifest.project-filter=-b"), "got: {stdout}");
    assert!(!stdout.contains("manifest.path=lower"), "got: {stdout}");
}

#[test]
#[serial]
fn list_scoped_shows_only_that_scope() {
    let sb = Sandbox::new();
    sb.west()
        .args(["config", "set", "--global", "k.g", "G"])
        .assert()
        .success();
    sb.west()
        .args(["config", "set", "--local", "k.l", "L"])
        .assert()
        .success();

    let out = sb
        .west()
        .args(["config", "list", "--local"])
        .assert()
        .success();
    let stdout = std::str::from_utf8(&out.get_output().stdout).unwrap();
    assert!(stdout.contains("k.l=L"));
    assert!(!stdout.contains("k.g="));
}

#[test]
#[serial]
fn get_missing_exits_1_no_output() {
    let sb = Sandbox::new();
    let res = sb
        .west()
        .args(["config", "get", "nope.key"])
        .assert()
        .failure();
    assert_eq!(res.get_output().status.code(), Some(1));
    assert!(res.get_output().stdout.is_empty());
}
