//! Integration tests for `alias.<name>` resolution.

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
}

fn write_local(sb: &Sandbox, body: &str) {
    std::fs::write(&sb.local, body).unwrap();
}

#[test]
#[serial]
fn alias_string_form_dispatches_to_target() {
    let sb = Sandbox::new();
    sb.west()
        .args(["config", "set", "manifest.path", "zephyr"])
        .assert()
        .success();
    sb.west()
        .args(["config", "set", "alias.lg", "config get manifest.path"])
        .assert()
        .success();

    let out = sb.west().args(["lg"]).assert().success();
    assert_eq!(
        std::str::from_utf8(&out.get_output().stdout).unwrap(),
        "zephyr\n"
    );
}

#[test]
#[serial]
fn alias_array_form_dispatches_to_target() {
    let sb = Sandbox::new();
    write_local(
        &sb,
        r#"[alias]
lg = ["config", "get", "manifest.path"]

[manifest]
path = "zephyr"
"#,
    );

    let out = sb.west().args(["lg"]).assert().success();
    assert_eq!(
        std::str::from_utf8(&out.get_output().stdout).unwrap(),
        "zephyr\n"
    );
}

#[test]
#[serial]
fn alias_recursive_chain() {
    let sb = Sandbox::new();
    write_local(
        &sb,
        r#"[alias]
a = "b"
b = "config get manifest.path"

[manifest]
path = "zephyr"
"#,
    );

    let out = sb.west().args(["a"]).assert().success();
    assert_eq!(
        std::str::from_utf8(&out.get_output().stdout).unwrap(),
        "zephyr\n"
    );
}

#[test]
#[serial]
fn alias_overrides_same_name_without_loop() {
    // alias.flash = "flash --no-rebuild" — first expansion adds --no-rebuild,
    // second iteration sees `flash` already-visited and stops. Since `flash`
    // isn't a built-in or extension yet, dispatch ends with "unknown command".
    // The KEY thing being tested: this doesn't infinite-loop.
    let sb = Sandbox::new();
    write_local(
        &sb,
        r#"[alias]
flash = "flash --no-rebuild"
"#,
    );

    let res = sb.west().args(["flash"]).assert().failure();
    let stderr = String::from_utf8_lossy(&res.get_output().stderr);
    assert!(
        stderr.contains("unknown command: flash"),
        "stderr: {stderr}"
    );
}

#[test]
#[serial]
fn alias_first_token_flag_rejected() {
    let sb = Sandbox::new();
    write_local(
        &sb,
        r#"[alias]
x = "-vvv config"
"#,
    );

    let res = sb.west().args(["x"]).assert().failure();
    let stderr = String::from_utf8_lossy(&res.get_output().stderr);
    assert!(
        stderr.contains("must begin with a command name"),
        "stderr: {stderr}"
    );
}

#[test]
#[serial]
fn alias_inline_via_config_flag() {
    let sb = Sandbox::new();
    sb.west()
        .args(["config", "set", "manifest.path", "zephyr"])
        .assert()
        .success();

    let out = sb
        .west()
        .args([
            "--config",
            "alias.cfg=config",
            "cfg",
            "get",
            "manifest.path",
        ])
        .assert()
        .success();
    assert_eq!(
        std::str::from_utf8(&out.get_output().stdout).unwrap(),
        "zephyr\n"
    );
}

#[test]
#[serial]
fn alias_unknown_command_exits_nonzero() {
    let sb = Sandbox::new();
    let res = sb.west().args(["nope"]).assert().failure();
    let stderr = String::from_utf8_lossy(&res.get_output().stderr);
    assert!(stderr.contains("unknown command: nope"), "stderr: {stderr}");
}

#[test]
#[serial]
fn alias_empty_value_errors() {
    let sb = Sandbox::new();
    write_local(
        &sb,
        r#"[alias]
empty = ""
"#,
    );

    let res = sb.west().args(["empty"]).assert().failure();
    let stderr = String::from_utf8_lossy(&res.get_output().stderr);
    assert!(stderr.contains("empty"), "stderr: {stderr}");
}
