//! Integration tests for `west help`. Same Sandbox pattern as
//! `tests/alias.rs`; per-test isolated `WEST_CONFIG_*` so aliases
//! written in one test don't leak to another.

use std::path::Path;

use assert_cmd::Command;
use serial_test::serial;
use tempfile::TempDir;

const BIN: &str = "west";

fn git_available() -> bool {
    std::process::Command::new("git")
        .arg("--version")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

fn git(args: &[&str], cwd: &Path) {
    let status = std::process::Command::new("git")
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
fn help_no_arg_starts_with_clap_top_level_help() {
    // `west help` builds on top of `west --help` — every line of
    // clap's standard help should appear at the start, with our
    // extension/alias sections + footer appended below. Asserting
    // prefix-equality (rather than byte-identity) keeps the test
    // resilient to the appended sections.
    let sb = Sandbox::new();
    let help_out = sb.west().args(["help"]).assert().success();
    let flag_out = sb.west().args(["--help"]).assert().success();
    let help_str = std::str::from_utf8(help_out.get_output().stdout.as_slice()).unwrap();
    let flag_str = std::str::from_utf8(flag_out.get_output().stdout.as_slice()).unwrap();
    assert!(
        help_str.starts_with(flag_str),
        "`west help` output should start with `west --help` output. \
         help: {help_str:?}\n\nflag: {flag_str:?}",
    );
}

#[test]
#[serial]
fn help_no_arg_appends_footer_pointer() {
    // The footer is the navigation hint v1 ships with — verify
    // it's in place so users discover `west help <command>`.
    let sb = Sandbox::new();
    let out = sb.west().args(["help"]).assert().success();
    let s = std::str::from_utf8(out.get_output().stdout.as_slice()).unwrap();
    assert!(
        s.contains("Run \"west help <command>\" for help on each <command>."),
        "expected footer line in: {s}",
    );
}

#[test]
#[serial]
fn help_no_arg_lists_configured_aliases() {
    // Set a couple of aliases at the global layer; both should
    // appear in the "aliases:" section. Empty aliases render as
    // `<empty>` per python v1.
    let sb = Sandbox::new();
    sb.west()
        .args(["config", "set", "--global", "alias.up", "update"])
        .assert()
        .success();
    sb.west()
        .args(["config", "set", "--global", "alias.menuconfig",
               "build --pristine never -t menuconfig"])
        .assert()
        .success();

    let out = sb.west().args(["help"]).assert().success();
    let s = std::str::from_utf8(out.get_output().stdout.as_slice()).unwrap();
    assert!(s.contains("aliases:"), "missing aliases header in: {s}");
    // No colon after the name — the two-column listing matches
    // clap's `Commands:` shape (`  <name>    <description>`).
    assert!(
        s.contains("  up ") && s.contains("update"),
        "missing `up` alias in: {s}",
    );
    assert!(
        s.contains("  menuconfig ")
            && s.contains("build --pristine never -t menuconfig"),
        "missing `menuconfig` alias in: {s}",
    );
}

#[test]
#[serial]
fn help_no_arg_no_aliases_omits_aliases_section() {
    // Aliases section is conditional — clean workspace shouldn't
    // grow an empty `aliases:` header.
    let sb = Sandbox::new();
    let out = sb.west().args(["help"]).assert().success();
    let s = std::str::from_utf8(out.get_output().stdout.as_slice()).unwrap();
    assert!(
        !s.contains("aliases:"),
        "unexpected aliases section in: {s}",
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
fn help_no_arg_lists_extension_commands_grouped_by_project() {
    // Bootstrap a workspace whose self-project declares
    // `west-commands.yml`. After `west init`, the self-project is
    // automatically cloned (it's the manifest repo). The help
    // listing should surface its extensions under
    // `extension commands from project manifest (path: my-manifest):`.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();

    let manifest_yaml = "\
manifest:
  self:
    path: my-manifest
    west-commands: scripts/west-commands.yml
  projects: []
";
    let west_commands_yaml = "\
west-commands:
- file: scripts/hello.py
  commands:
  - name: hello
    class: Hello
    help: say hi to the world
  - name: silent
    class: Silent
";
    // Stand up a manifest repo + bare clone, then `west init`.
    let manifest_work = sb.workspace.parent().unwrap().join("manifest-work");
    std::fs::create_dir_all(manifest_work.join("scripts")).unwrap();
    std::fs::write(manifest_work.join("west.yml"), manifest_yaml).unwrap();
    std::fs::write(
        manifest_work.join("scripts/west-commands.yml"),
        west_commands_yaml,
    )
    .unwrap();
    std::fs::write(manifest_work.join("scripts/hello.py"), "# stub\n").unwrap();
    git(&["init", "-q", "--initial-branch=main", "."], &manifest_work);
    git(&["add", "."], &manifest_work);
    git(&["commit", "-q", "-m", "initial"], &manifest_work);
    let bare = sb.workspace.parent().unwrap().join("manifest.git");
    git(
        &[
            "clone",
            "-q",
            "--bare",
            manifest_work.to_str().unwrap(),
            bare.to_str().unwrap(),
        ],
        sb.workspace.parent().unwrap(),
    );

    // Re-init the workspace in the sandbox's `ws/` dir.
    let _ = std::fs::remove_dir_all(&sb.workspace);
    sb.west()
        .current_dir(sb.workspace.parent().unwrap())
        .args([
            "init",
            "--url",
            bare.to_str().unwrap(),
            sb.workspace.to_str().unwrap(),
        ])
        .assert()
        .success();

    let out = sb.west().args(["help"]).assert().success();
    let s = std::str::from_utf8(out.get_output().stdout.as_slice()).unwrap();
    assert!(
        s.contains("extension commands from project manifest (path: my-manifest):"),
        "missing extension-section header in: {s}",
    );
    // No colon after the name — matches clap's `Commands:` shape.
    assert!(
        s.contains("  hello ") && s.contains("say hi to the world"),
        "missing `hello` entry with help text in: {s}",
    );
    // Even names without a help string get a leading-indented row;
    // assert the bare-name form (with a trailing newline character)
    // so we're not matching a substring of some longer line.
    assert!(
        s.contains("\n  silent\n"),
        "missing bare `silent` entry (no help text — should still render): {s}",
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
