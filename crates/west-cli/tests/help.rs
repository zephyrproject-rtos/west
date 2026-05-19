//! Integration tests for `west help`. Same Sandbox pattern as
//! `tests/alias.rs`; per-test isolated `WEST_CONFIG_*` so aliases
//! written in one test don't leak to another.

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

#[test]
#[serial]
fn help_no_arg_matches_top_level_dash_dash_help() {
    // `west help` and `west --help` produce identical output —
    // anything else would surprise the user.
    let sb = Sandbox::new();
    let h = sb.west().args(["help"]).assert().success();
    let dh = sb.west().args(["--help"]).assert().success();
    assert_eq!(
        std::str::from_utf8(h.get_output().stdout.as_slice()).unwrap(),
        std::str::from_utf8(dh.get_output().stdout.as_slice()).unwrap(),
    );
}

#[test]
#[serial]
fn help_builtin_matches_builtin_dash_dash_help() {
    // `west help update` and `west update --help` must produce
    // identical output. The custom `Help` subcommand re-routes
    // through clap's full pipeline rather than calling `print_help`
    // on the bare subcommand, specifically so the Usage line
    // includes the `west ` prefix.
    let sb = Sandbox::new();
    let via_help = sb.west().args(["help", "update"]).assert().success();
    let via_flag = sb.west().args(["update", "--help"]).assert().success();
    assert_eq!(
        std::str::from_utf8(via_help.get_output().stdout.as_slice()).unwrap(),
        std::str::from_utf8(via_flag.get_output().stdout.as_slice()).unwrap(),
    );
}

#[test]
#[serial]
fn help_help_shows_help_subcommand_help() {
    // `west help help` is a meta-case; should describe the `Help`
    // command itself. Verify the description string appears.
    let sb = Sandbox::new();
    let out = sb.west().args(["help", "help"]).assert().success();
    let s = std::str::from_utf8(out.get_output().stdout.as_slice()).unwrap();
    assert!(
        s.contains("Show help for a command"),
        "expected help-subcommand description in: {s}"
    );
}

#[test]
#[serial]
fn help_alias_resolves_to_target_command() {
    // `alias.myls = "list -a"` → `west help myls` should show
    // `west list --help` since the alias's first token is `list`.
    let sb = Sandbox::new();
    sb.west()
        .args(["config", "set", "alias.myls", "list -a"])
        .assert()
        .success();

    let via_alias = sb.west().args(["help", "myls"]).assert().success();
    let via_target = sb.west().args(["list", "--help"]).assert().success();
    assert_eq!(
        std::str::from_utf8(via_alias.get_output().stdout.as_slice()).unwrap(),
        std::str::from_utf8(via_target.get_output().stdout.as_slice()).unwrap(),
    );
}

#[test]
#[serial]
fn help_alias_chained_resolves_recursively() {
    // alias.a → b, alias.b → list. `west help a` should reach
    // `list`'s help by recursing through both alias entries.
    let sb = Sandbox::new();
    sb.west()
        .args(["config", "set", "alias.a", "b"])
        .assert()
        .success();
    sb.west()
        .args(["config", "set", "alias.b", "list -a"])
        .assert()
        .success();

    let via_chain = sb.west().args(["help", "a"]).assert().success();
    let via_target = sb.west().args(["list", "--help"]).assert().success();
    assert_eq!(
        std::str::from_utf8(via_chain.get_output().stdout.as_slice()).unwrap(),
        std::str::from_utf8(via_target.get_output().stdout.as_slice()).unwrap(),
    );
}

#[test]
#[serial]
fn help_alias_cycle_terminates() {
    // alias.x → y, alias.y → x. The cycle must NOT recurse forever;
    // the visited-set in `help::resolve` short-circuits with a
    // clear error.
    let sb = Sandbox::new();
    sb.west()
        .args(["config", "set", "alias.x", "y"])
        .assert()
        .success();
    sb.west()
        .args(["config", "set", "alias.y", "x"])
        .assert()
        .success();

    let out = sb.west().args(["help", "x"]).assert().failure();
    let stderr = std::str::from_utf8(out.get_output().stderr.as_slice()).unwrap();
    assert!(
        stderr.contains("alias cycle"),
        "expected alias-cycle error in stderr: {stderr}"
    );
}

#[test]
#[serial]
fn help_unknown_name_errors_via_extension_path() {
    // A name that's neither built-in, alias, nor extension falls
    // through `extension::run`'s "unknown command" branch.
    let sb = Sandbox::new();
    let out = sb.west().args(["help", "nonexistent"]).assert().failure();
    let stderr = std::str::from_utf8(out.get_output().stderr.as_slice()).unwrap();
    assert!(
        stderr.contains("unknown command: nonexistent"),
        "expected unknown-command error in stderr: {stderr}"
    );
}
