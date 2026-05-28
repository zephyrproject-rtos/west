//! Integration tests for `west config get/set/unset/list` and the top-level
//! `--config` / `--config-file` overrides.
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
fn set_writes_native_integer_via_toml_syntax() {
    let sb = Sandbox::new();
    sb.west()
        .args(["config", "set", "n.v", "42"])
        .assert()
        .success();
    let body = read(&sb.local);
    assert!(body.contains("v = 42"), "got: {body}");
    assert!(
        !body.contains(r#"v = "42""#),
        "should not be quoted: {body}"
    );

    let out = sb.west().args(["config", "get", "n.v"]).assert().success();
    assert_eq!(
        std::str::from_utf8(&out.get_output().stdout).unwrap(),
        "42\n"
    );
}

#[test]
#[serial]
fn set_writes_string_when_value_is_quoted() {
    let sb = Sandbox::new();
    sb.west()
        .args(["config", "set", "n.v", r#""42""#])
        .assert()
        .success();
    let body = read(&sb.local);
    assert!(body.contains(r#"v = "42""#), "got: {body}");
}

#[test]
#[serial]
fn set_writes_native_bool_via_toml_syntax() {
    let sb = Sandbox::new();
    sb.west()
        .args(["config", "set", "b.v", "true"])
        .assert()
        .success();
    let body = read(&sb.local);
    assert!(body.contains("v = true"), "got: {body}");

    let out = sb.west().args(["config", "get", "b.v"]).assert().success();
    assert_eq!(
        std::str::from_utf8(&out.get_output().stdout).unwrap(),
        "true\n"
    );
}

#[test]
#[serial]
fn set_rejects_ambiguous_unparseable_toml() {
    let sb = Sandbox::new();
    let res = sb
        .west()
        .args(["config", "set", "x.v", "[bad"])
        .assert()
        .failure();
    assert_eq!(res.get_output().status.code(), Some(2));
}

#[test]
#[serial]
fn set_list_via_toml_array_syntax() {
    let sb = Sandbox::new();
    sb.west()
        .args([
            "config",
            "set",
            "manifest.project-filter",
            r#"["+foo","-bar","baz"]"#,
        ])
        .assert()
        .success();
    assert!(
        read(&sb.local).contains(r#"project-filter = ["+foo", "-bar", "baz"]"#),
        "got: {}",
        read(&sb.local)
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
fn append_to_existing_list_grows_by_one() {
    // Set a list, then -a a scalar — should grow by exactly one element.
    let sb = Sandbox::new();
    sb.west()
        .args([
            "config",
            "set",
            "manifest.project-filter",
            r#"["+foo","-bar"]"#,
        ])
        .assert()
        .success();

    sb.west()
        .args(["config", "set", "-a", "manifest.project-filter", "+baz"])
        .assert()
        .success();

    let out = sb
        .west()
        .args(["config", "get", "manifest.project-filter"])
        .assert()
        .success();
    assert_eq!(
        std::str::from_utf8(&out.get_output().stdout).unwrap(),
        "+foo\n-bar\n+baz\n"
    );
}

#[test]
#[serial]
fn append_list_value_extends_not_nests() {
    // -a with a TOML list input EXTENDS — adds each element one by
    // one — rather than nesting a sub-list. The python analogue is
    // `list.extend(...)`, not `list.append(...)`.
    let sb = Sandbox::new();
    sb.west()
        .args(["config", "set", "manifest.project-filter", r#"["+foo"]"#])
        .assert()
        .success();

    sb.west()
        .args([
            "config",
            "set",
            "--append",
            "manifest.project-filter",
            r#"["+a","+b"]"#,
        ])
        .assert()
        .success();

    let out = sb
        .west()
        .args(["config", "get", "manifest.project-filter"])
        .assert()
        .success();
    assert_eq!(
        std::str::from_utf8(&out.get_output().stdout).unwrap(),
        "+foo\n+a\n+b\n"
    );
}

#[test]
#[serial]
fn append_to_absent_creates_new_list() {
    // Key absent at the target layer → -a creates a fresh list with
    // just the appended elements.
    let sb = Sandbox::new();
    sb.west()
        .args(["config", "set", "-a", "manifest.project-filter", "+only"])
        .assert()
        .success();

    let out = sb
        .west()
        .args(["config", "get", "manifest.project-filter"])
        .assert()
        .success();
    assert_eq!(
        std::str::from_utf8(&out.get_output().stdout).unwrap(),
        "+only\n"
    );
}

#[test]
#[serial]
fn append_to_scalar_errors_cleanly() {
    // The key holds a scalar — `-a` refuses rather than silently
    // converting to a list.
    let sb = Sandbox::new();
    sb.west()
        .args(["config", "set", "k.v", "plain"])
        .assert()
        .success();

    let res = sb
        .west()
        .args(["config", "set", "-a", "k.v", "more"])
        .assert()
        .failure();
    assert_eq!(res.get_output().status.code(), Some(2));
    let stderr = String::from_utf8_lossy(&res.get_output().stderr);
    assert!(stderr.contains("list-valued"), "stderr: {stderr}");
    assert!(stderr.contains("string"), "stderr: {stderr}");

    // The original value is preserved — append's pre-check didn't touch
    // anything on disk.
    let out = sb.west().args(["config", "get", "k.v"]).assert().success();
    assert_eq!(
        std::str::from_utf8(&out.get_output().stdout).unwrap(),
        "plain\n"
    );
}

#[test]
#[serial]
fn append_honours_scope_does_not_peek_at_other_layers() {
    // Global has the key as a list; --local --append starts from
    // scratch at the local layer (not from the global value). The
    // local layer ends up holding just the new element. This is the
    // "scope-target read, scope-target write" semantic we chose over
    // v1's merged-read behaviour, to avoid silent layer shadowing.
    let sb = Sandbox::new();
    sb.west()
        .args([
            "config",
            "set",
            "--global",
            "manifest.project-filter",
            r#"["+global"]"#,
        ])
        .assert()
        .success();

    sb.west()
        .args([
            "config",
            "set",
            "--local",
            "-a",
            "manifest.project-filter",
            "+local",
        ])
        .assert()
        .success();

    // Local layer holds just the new entry.
    let local_only = sb
        .west()
        .args(["config", "get", "--local", "manifest.project-filter"])
        .assert()
        .success();
    assert_eq!(
        std::str::from_utf8(&local_only.get_output().stdout).unwrap(),
        "+local\n"
    );

    // Global layer is untouched.
    let global_only = sb
        .west()
        .args(["config", "get", "--global", "manifest.project-filter"])
        .assert()
        .success();
    assert_eq!(
        std::str::from_utf8(&global_only.get_output().stdout).unwrap(),
        "+global\n"
    );
}

#[test]
#[serial]
fn set_default_local_outside_workspace_fails_with_failure_exit() {
    // Pre-`exit` module: this returned 3 (the lone `3` in the tree).
    // Folded to FAILURE (1) for consistency with topdir / list / diff
    // / status / compare / forall / grep / update — every other command
    // that reports "no workspace" returns FAILURE.
    let sb = Sandbox::new();
    let res = sb
        .west_outside_workspace()
        .args(["config", "set", "k.v", "hello"])
        .assert()
        .failure();
    assert_eq!(res.get_output().status.code(), Some(1));
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

    sb.west()
        .args(["config", "unset", "k.v"])
        .assert()
        .success();
    let out = sb.west().args(["config", "get", "k.v"]).assert().success();
    assert_eq!(
        std::str::from_utf8(&out.get_output().stdout).unwrap(),
        "global-val\n"
    );

    sb.west()
        .args(["config", "unset", "k.v"])
        .assert()
        .success();
    let res = sb.west().args(["config", "get", "k.v"]).assert().failure();
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

    let out = sb.west().args(["config", "get", "k.v"]).assert().success();
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
fn unset_delete_all_removes_from_every_scope() {
    // v1's `west config -D` semantic: delete `name` from every layer
    // that holds it. Set in all three scopes, then verify -D wipes
    // them all and a subsequent `get` fails with the "not set" exit.
    let sb = Sandbox::new();
    sb.west()
        .args(["config", "set", "--system", "k.v", "S"])
        .assert()
        .success();
    sb.west()
        .args(["config", "set", "--global", "k.v", "G"])
        .assert()
        .success();
    sb.west()
        .args(["config", "set", "--local", "k.v", "L"])
        .assert()
        .success();

    sb.west()
        .args(["config", "unset", "-D", "k.v"])
        .assert()
        .success();

    for scope in ["--system", "--global", "--local"] {
        let res = sb
            .west()
            .args(["config", "get", scope, "k.v"])
            .assert()
            .failure();
        assert_eq!(
            res.get_output().status.code(),
            Some(1),
            "scope {scope} still held the key"
        );
    }
}

#[test]
#[serial]
fn unset_delete_all_succeeds_when_only_some_scopes_have_it() {
    // The key is only in --global; -D should clear it and succeed
    // even though --system / --local never held it.
    let sb = Sandbox::new();
    sb.west()
        .args(["config", "set", "--global", "k.v", "G"])
        .assert()
        .success();

    sb.west()
        .args(["config", "unset", "--delete-all", "k.v"])
        .assert()
        .success();

    let res = sb
        .west()
        .args(["config", "get", "--global", "k.v"])
        .assert()
        .failure();
    assert_eq!(res.get_output().status.code(), Some(1));
}

#[test]
#[serial]
fn unset_delete_all_fails_when_no_scope_has_it() {
    let sb = Sandbox::new();
    let res = sb
        .west()
        .args(["config", "unset", "-D", "absent.key"])
        .assert()
        .failure();
    assert_eq!(res.get_output().status.code(), Some(1));
    let stderr = String::from_utf8_lossy(&res.get_output().stderr);
    assert!(stderr.contains("not set"), "stderr: {stderr}");
}

#[test]
#[serial]
fn unset_delete_all_conflicts_with_scope_flags() {
    // clap should reject `-D --global` (and the other scope flags)
    // because the operation modes are mutually exclusive.
    let sb = Sandbox::new();
    let res = sb
        .west()
        .args(["config", "unset", "-D", "--global", "k.v"])
        .assert()
        .failure();
    // clap parse errors exit with 2.
    assert_eq!(res.get_output().status.code(), Some(2));
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
            "manifest.project-filter",
            r#"["+a","-b"]"#,
        ])
        .assert()
        .success();

    let out = sb.west().args(["config", "list"]).assert().success();
    let stdout = std::str::from_utf8(&out.get_output().stdout).unwrap();
    assert!(stdout.contains("manifest.path=upper"), "got: {stdout}");
    assert!(
        stdout.contains("manifest.project-filter=+a"),
        "got: {stdout}"
    );
    assert!(
        stdout.contains("manifest.project-filter=-b"),
        "got: {stdout}"
    );
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

#[test]
#[serial]
fn get_default_used_when_key_absent() {
    // `--default VALUE` upgrades "key not set" from exit 1 to exit 0
    // with VALUE on stdout. Mirrors `git config --get --default`.
    let sb = Sandbox::new();
    let res = sb
        .west()
        .args(["config", "get", "--default", "fallback", "nope.key"])
        .assert()
        .success();
    assert_eq!(
        std::str::from_utf8(&res.get_output().stdout).unwrap(),
        "fallback\n"
    );
}

#[test]
#[serial]
fn get_default_ignored_when_key_present() {
    // When the key IS set, --default is ignored and the real value
    // is printed.
    let sb = Sandbox::new();
    sb.west()
        .args(["config", "set", "k.v", "real"])
        .assert()
        .success();

    let res = sb
        .west()
        .args(["config", "get", "--default", "fallback", "k.v"])
        .assert()
        .success();
    assert_eq!(
        std::str::from_utf8(&res.get_output().stdout).unwrap(),
        "real\n"
    );
}

#[test]
#[serial]
fn get_default_works_with_file_flag() {
    // `--file PATH --default VALUE` for an absent key in a specific
    // file: same fallback semantic as the layered path.
    let sb = Sandbox::new();
    let empty = sb._tmp.path().join("empty.toml");
    std::fs::write(&empty, "").unwrap();

    let res = sb
        .west()
        .args([
            "config",
            "get",
            "--file",
            empty.to_str().unwrap(),
            "--default",
            "from-file-fallback",
            "absent.key",
        ])
        .assert()
        .success();
    assert_eq!(
        std::str::from_utf8(&res.get_output().stdout).unwrap(),
        "from-file-fallback\n"
    );
}

#[test]
#[serial]
fn get_default_does_not_mask_malformed_key_error() {
    // `--default` only kicks in for "key not set". A malformed key
    // (no dot) is a usage error and should still exit 2 — the
    // fallback doesn't get printed.
    let sb = Sandbox::new();
    let res = sb
        .west()
        .args(["config", "get", "--default", "fallback", "no-dot"])
        .assert()
        .failure();
    assert_eq!(res.get_output().status.code(), Some(2));
    assert!(res.get_output().stdout.is_empty());
}

// --- top-level --config / --config-file -------------------------------------

#[test]
#[serial]
fn top_level_config_overrides_file() {
    let sb = Sandbox::new();
    sb.west()
        .args(["config", "set", "k.v", "from-file"])
        .assert()
        .success();
    let out = sb
        .west()
        .args(["--config", "k.v=from-cli", "config", "get", "k.v"])
        .assert()
        .success();
    assert_eq!(
        std::str::from_utf8(&out.get_output().stdout).unwrap(),
        "from-cli\n"
    );
}

#[test]
#[serial]
fn top_level_config_typed_int() {
    let sb = Sandbox::new();
    let out = sb
        .west()
        .args(["--config", "n.v=42", "config", "get", "n.v"])
        .assert()
        .success();
    assert_eq!(
        std::str::from_utf8(&out.get_output().stdout).unwrap(),
        "42\n"
    );
}

#[test]
#[serial]
fn top_level_config_array() {
    let sb = Sandbox::new();
    let out = sb
        .west()
        .args([
            "--config",
            r#"foo.list=["a","b"]"#,
            "config",
            "get",
            "foo.list",
        ])
        .assert()
        .success();
    assert_eq!(
        std::str::from_utf8(&out.get_output().stdout).unwrap(),
        "a\nb\n"
    );
}

#[test]
#[serial]
fn top_level_config_does_not_affect_writes() {
    let sb = Sandbox::new();
    // Override says X, but `set` writes Y to disk.
    sb.west()
        .args(["--config", "k.v=overridden", "config", "set", "k.v", "Y"])
        .assert()
        .success();
    assert!(
        read(&sb.local).contains(r#"v = "Y""#),
        "got: {}",
        read(&sb.local)
    );

    // Without the override, get reads Y from disk.
    let out = sb.west().args(["config", "get", "k.v"]).assert().success();
    assert_eq!(
        std::str::from_utf8(&out.get_output().stdout).unwrap(),
        "Y\n"
    );
}

#[test]
#[serial]
fn top_level_config_file_appends_layer() {
    let sb = Sandbox::new();
    let extra = sb._tmp.path().join("extra.toml");
    std::fs::write(&extra, "[k]\nv = \"from-extra\"\n").unwrap();

    let out = sb
        .west()
        .args([
            "--config-file",
            extra.to_str().unwrap(),
            "config",
            "get",
            "k.v",
        ])
        .assert()
        .success();
    assert_eq!(
        std::str::from_utf8(&out.get_output().stdout).unwrap(),
        "from-extra\n"
    );
}

#[test]
#[serial]
fn top_level_config_load_failure_blocks_other_commands() {
    let sb = Sandbox::new();
    // Malformed TOML in the local layer.
    std::fs::write(&sb.local, "[unclosed\nno = good\n").unwrap();

    let res = sb.west().args(["topdir"]).assert().failure();
    let stderr = String::from_utf8_lossy(&res.get_output().stderr);
    assert!(
        stderr.contains("malformed TOML"),
        "expected malformed-TOML error from top-level load, got: {stderr}"
    );
}

#[test]
#[serial]
fn top_level_invalid_config_pair_errors() {
    let sb = Sandbox::new();
    let res = sb
        .west()
        .args(["--config", "no-equals", "topdir"])
        .assert()
        .failure();
    let stderr = String::from_utf8_lossy(&res.get_output().stderr);
    assert!(
        stderr.contains("--config"),
        "expected --config error, got: {stderr}"
    );
}

#[test]
#[serial]
fn top_level_flags_rejected_after_subcommand() {
    let sb = Sandbox::new();
    let res = sb
        .west()
        .args(["config", "get", "--config", "k.v=oops", "k.v"])
        .assert()
        .failure();
    let code = res.get_output().status.code();
    // clap emits exit code 2 for arg-parse errors.
    assert_eq!(code, Some(2));
}
