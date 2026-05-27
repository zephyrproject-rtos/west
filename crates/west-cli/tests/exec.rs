//! Integration tests for `west exec`.

use assert_cmd::Command;
use serial_test::serial;
use tempfile::TempDir;

const BIN: &str = "west";

fn west() -> Command {
    Command::cargo_bin(BIN).unwrap()
}

#[test]
fn exec_runs_program_and_propagates_stdout() {
    let out = west().args(["exec", "printf", "hello"]).assert().success();
    assert_eq!(
        std::str::from_utf8(&out.get_output().stdout).unwrap(),
        "hello"
    );
}

#[test]
fn exec_propagates_exit_code() {
    let res = west()
        .args(["exec", "sh", "-c", "exit 7"])
        .assert()
        .failure();
    assert_eq!(res.get_output().status.code(), Some(7));
}

#[test]
fn exec_propagates_zero_exit_code() {
    west()
        .args(["exec", "sh", "-c", "exit 0"])
        .assert()
        .success();
}

#[test]
fn exec_passes_hyphen_args_without_dashdash() {
    let out = west()
        .args(["exec", "printf", "%s\n", "-n"])
        .assert()
        .success();
    assert_eq!(
        std::str::from_utf8(&out.get_output().stdout).unwrap(),
        "-n\n"
    );
}

#[test]
fn exec_passes_hyphen_args_with_dashdash() {
    let out = west()
        .args(["exec", "--", "printf", "%s\n", "-n"])
        .assert()
        .success();
    assert_eq!(
        std::str::from_utf8(&out.get_output().stdout).unwrap(),
        "-n\n"
    );
}

#[test]
fn exec_missing_program_errors() {
    let res = west()
        .args(["exec", "/no/such/binary-for-west-exec-test"])
        .assert()
        .failure();
    let stderr = String::from_utf8_lossy(&res.get_output().stderr);
    assert!(stderr.contains("exec"), "stderr: {stderr}");
    assert!(
        stderr.contains("/no/such/binary-for-west-exec-test"),
        "stderr: {stderr}"
    );
}

#[test]
#[serial]
fn exec_via_alias_overrides_top_level_flags() {
    // `alias.dbg = "exec <west-bin> -vvv config get foo.x"` — the alias
    // resolves once, then `exec` spawns a fresh `west` invocation that re-
    // parses argv. The subprocess sees `-vvv` and emits trace output.
    let tmp = TempDir::new().unwrap();
    let workspace = tmp.path().join("ws");
    std::fs::create_dir_all(workspace.join(".west")).unwrap();
    let local = workspace.join(".west").join("config.toml");

    // Use the same binary for both invocations.
    let west_bin = assert_cmd::cargo::cargo_bin(BIN);

    // Build the local config: alias + the value the subprocess will read.
    let alias_cmd = format!("exec {} -vvv config get foo.x", west_bin.to_str().unwrap());
    let body = format!("[alias]\ndbg = {alias_cmd:?}\n\n[foo]\nx = \"value\"\n");
    std::fs::write(&local, body).unwrap();

    let res = Command::cargo_bin(BIN)
        .unwrap()
        .current_dir(&workspace)
        .env("WEST_CONFIG_LOCAL", &local)
        .env("WEST_CONFIG_GLOBAL", tmp.path().join("glob.toml"))
        .env("WEST_CONFIG_SYSTEM", tmp.path().join("sys.toml"))
        .env_remove("XDG_CONFIG_HOME")
        .args(["dbg"])
        .assert()
        .success();

    let stdout = std::str::from_utf8(&res.get_output().stdout).unwrap();
    let stderr = std::str::from_utf8(&res.get_output().stderr).unwrap();
    assert_eq!(stdout, "value\n");
    // -vvv ⇒ Trace-level filter. Trace lines carry their module
    // target; west_core::config logs each config layer it loads, so a
    // `[west_core::config]` line is the visible, -vvv-specific evidence
    // the subprocess saw the elevated verbosity.
    assert!(
        stderr.contains("west_core::config"),
        "expected trace output from -vvv subprocess, got stderr: {stderr}"
    );
}
