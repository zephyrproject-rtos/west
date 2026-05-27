//! Integration tests for `west diff`. Sandbox + bare-repo fixture
//! pattern mirroring `tests/forall.rs` (the architectural review
//! flagged the duplication; refactor tracked separately). Tests
//! cover: empty/dirty workspaces, `--exit-code`, `--manifest`,
//! per-project filtering, uncloned-positional error path, extra
//! args forwarded to git, color modes, `--quiet`, and parallel
//! non-interleaving.

use std::path::{Path, PathBuf};
use std::process::Command;

use assert_cmd::Command as AssertCmd;
use serial_test::serial;
use tempfile::TempDir;

const BIN: &str = "west";

// ============================================================================
// Helpers (mirroring tests/forall.rs)
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

/// Create a bare repo named `name.git` under `root` with a single
/// commit landing one file (`R`) containing `content`.
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

fn manifest_yaml(projects: &[(&str, &Path)]) -> String {
    let mut s = String::from("manifest:\n  self:\n    path: my-manifest\n  projects:\n");
    for (name, bare) in projects {
        s.push_str(&format!(
            "    - name: {name}\n      url: {url}\n      revision: main\n",
            url = bare.display()
        ));
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

fn update_all(sb: &Sandbox, ws: &Path) {
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update", "-j", "1"])
        .assert()
        .success();
}

/// Dirty up one file inside a cloned project so `git diff` has
/// something to show.
fn touch(ws: &Path, project: &str, file: &str, content: &str) {
    let p = ws.join(project).join(file);
    std::fs::write(&p, content).unwrap_or_else(|e| panic!("write {p:?}: {e}"));
}

// ============================================================================
// Tests
// ============================================================================

#[test]
#[serial]
fn diff_clean_workspace_emits_no_banner() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let yaml = manifest_yaml(&[("alpha", &alpha)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);

    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "diff"])
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(&out.get_output().stdout).into_owned();
    assert!(
        !stdout.contains("=== diff in"),
        "no banner expected for clean tree, got: {stdout:?}"
    );
}

#[test]
#[serial]
fn diff_modified_file_emits_banner_and_body() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let yaml = manifest_yaml(&[("alpha", &alpha)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);
    touch(&ws, "alpha", "R", "CHANGED");

    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "diff", "--color", "never"])
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(&out.get_output().stdout).into_owned();
    let stderr = String::from_utf8_lossy(&out.get_output().stderr).into_owned();
    // Banner is chrome → stderr; the diff body → stdout.
    assert!(stderr.contains("=== diff in alpha"), "missing banner on stderr: {stderr:?}");
    assert!(!stdout.contains("=== diff in"), "banner leaked onto stdout: {stdout:?}");
    assert!(stdout.contains("CHANGED") || stdout.contains("@@"),
        "expected diff body on stdout: {stdout:?}");
}

#[test]
#[serial]
fn diff_exit_code_returns_1_when_diff_present() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let yaml = manifest_yaml(&[("alpha", &alpha)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);
    touch(&ws, "alpha", "R", "X");

    sb.west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "diff",
            "--exit-code",
            "--color",
            "never",
        ])
        .assert()
        .code(1);
}

#[test]
#[serial]
fn diff_exit_code_returns_0_when_clean() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let yaml = manifest_yaml(&[("alpha", &alpha)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);

    sb.west()
        .args(["-C", ws.to_str().unwrap(), "diff", "--exit-code"])
        .assert()
        .success();
}

#[test]
#[serial]
fn diff_manifest_compares_against_manifest_rev() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let yaml = manifest_yaml(&[("alpha", &alpha)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);

    // Land a local commit on top of manifest-rev (HEAD = manifest-rev
    // initially; after the commit, HEAD != manifest-rev).
    let alpha_dir = ws.join("alpha");
    std::fs::write(alpha_dir.join("R"), "LOCAL-CHANGE").unwrap();
    git(&["add", "."], &alpha_dir);
    git(&["commit", "-q", "-m", "local"], &alpha_dir);

    // Without `-m`: HEAD vs working tree (clean) → no diff.
    let no_m = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "diff", "--color", "never"])
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(&no_m.get_output().stdout).into_owned();
    assert!(!stdout.contains("=== diff in alpha"),
        "expected no banner without --manifest: {stdout:?}");

    // With `-m`: manifest-rev vs working tree → diff present.
    let with_m = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "diff", "-m", "--color", "never"])
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(&with_m.get_output().stdout).into_owned();
    let stderr = String::from_utf8_lossy(&with_m.get_output().stderr).into_owned();
    assert!(stderr.contains("=== diff in alpha"),
        "missing banner with --manifest on stderr: {stderr:?}");
    assert!(stdout.contains("LOCAL-CHANGE") || stdout.contains("@@"),
        "expected diff body on stdout: {stdout:?}");
}

#[test]
#[serial]
fn diff_filters_projects_by_positional_name() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let beta = make_bare_with_one_commit(sb.root(), "beta", "B");
    let yaml = manifest_yaml(&[("alpha", &alpha), ("beta", &beta)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);
    touch(&ws, "alpha", "R", "X");
    touch(&ws, "beta", "R", "Y");

    // Ask for only `alpha`; `beta`'s diff must not appear.
    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "diff",
            "alpha",
            "--color",
            "never",
        ])
        .assert()
        .success();
    let stderr = String::from_utf8_lossy(&out.get_output().stderr).into_owned();
    assert!(stderr.contains("=== diff in alpha"),
        "missing alpha banner on stderr: {stderr:?}");
    assert!(!stderr.contains("=== diff in beta"),
        "unexpected beta banner: {stderr:?}");
}

#[test]
#[serial]
fn diff_uncloned_positional_errors() {
    if !git_available() {
        return;
    }
    // Workspace where the manifest declares `beta` but we never
    // updated it — so `<ws>/beta` is not a git repo.
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let beta = make_bare_with_one_commit(sb.root(), "beta", "B");
    let yaml = manifest_yaml(&[("alpha", &alpha), ("beta", &beta)]);
    let ws = init_workspace(&sb, &yaml);
    // Skip update_all on purpose — neither project is cloned.
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "diff", "beta"])
        .assert()
        .code(2);
}

#[test]
#[serial]
fn diff_extra_args_forwarded_to_git() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let yaml = manifest_yaml(&[("alpha", &alpha)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);
    touch(&ws, "alpha", "R", "CHANGED");

    // `--stat` makes git emit a shortstat summary instead of the
    // full hunks. Presence of the `+ 1 file changed` style line is
    // proof the flag was forwarded.
    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "diff",
            "--color",
            "never",
            "--",
            "--stat",
        ])
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(&out.get_output().stdout).into_owned();
    assert!(
        stdout.contains("file changed") || stdout.contains("insertion") || stdout.contains("deletion"),
        "expected --stat-shaped output in: {stdout:?}"
    );
    // The full-hunk markers should NOT be present under --stat.
    assert!(!stdout.contains("@@"),
        "didn't expect a full hunk under --stat: {stdout:?}");
}

#[test]
#[serial]
fn diff_color_always_emits_colored_banner() {
    // The per-project `=== diff in <name>` banner uses bright
    // green + bold, matching python v1's banner palette
    // (`colorama.Fore.LIGHTGREEN_EX`). When the user passes
    // `--color always`, the banner stays coloured even when
    // stderr is captured (assert_cmd's pipe).
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let yaml = manifest_yaml(&[("alpha", &alpha)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);
    touch(&ws, "alpha", "R", "CHANGED");

    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "diff",
            "--color",
            "always",
        ])
        .assert()
        .success();
    let stderr = String::from_utf8_lossy(out.get_output().stderr.as_slice()).into_owned();
    // Find the banner line (now on stderr) and confirm it's wrapped
    // in colour escapes. `\x1b[1;92m` (bold + bright green) or its
    // variants — accept any escape sequence preceding the banner.
    let banner_line = stderr
        .lines()
        .find(|l| l.contains("=== diff in alpha"))
        .unwrap_or_else(|| panic!("missing banner on stderr: {stderr:?}"));
    assert!(
        banner_line.starts_with("\x1b["),
        "banner not coloured under --color always: {banner_line:?}"
    );
}

#[test]
#[serial]
fn diff_color_never_strips_banner_color() {
    // The mirror: `--color never` must yield a plain banner.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let yaml = manifest_yaml(&[("alpha", &alpha)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);
    touch(&ws, "alpha", "R", "CHANGED");

    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "diff",
            "--color",
            "never",
        ])
        .assert()
        .success();
    let stderr = String::from_utf8_lossy(out.get_output().stderr.as_slice()).into_owned();
    let banner_line = stderr
        .lines()
        .find(|l| l.contains("=== diff in alpha"))
        .unwrap_or_else(|| panic!("missing banner on stderr: {stderr:?}"));
    assert!(
        !banner_line.contains("\x1b["),
        "banner has colour escapes despite --color never: {banner_line:?}"
    );
}

#[test]
#[serial]
fn diff_color_always_includes_ansi_when_piped() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let yaml = manifest_yaml(&[("alpha", &alpha)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);
    touch(&ws, "alpha", "R", "CHANGED");

    // assert_cmd captures stdout (a pipe). With `--color always`,
    // ANSI escapes should appear regardless.
    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "diff",
            "--color",
            "always",
        ])
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(&out.get_output().stdout).into_owned();
    assert!(
        stdout.contains("\x1b["),
        "expected ANSI escapes with --color always: {stdout:?}"
    );
}

#[test]
#[serial]
fn diff_color_never_strips_ansi() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let yaml = manifest_yaml(&[("alpha", &alpha)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);
    touch(&ws, "alpha", "R", "CHANGED");

    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "diff",
            "--color",
            "never",
        ])
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(&out.get_output().stdout).into_owned();
    assert!(
        !stdout.contains("\x1b["),
        "expected no ANSI escapes with --color never: {stdout:?}"
    );
}

#[test]
#[serial]
fn diff_verbose_cancels_quiet() {
    // `-v` and `-q` compose via net subtraction (same primitive
    // `log_level_filter` uses). `west -q -v diff` has net 0 and
    // should NOT suppress chrome — the banner reappears.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let yaml = manifest_yaml(&[("alpha", &alpha)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);
    touch(&ws, "alpha", "R", "CHANGED");

    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "-q",
            "-v",
            "diff",
            "--color",
            "never",
        ])
        .assert()
        .success();
    let stderr = String::from_utf8_lossy(out.get_output().stderr.as_slice()).into_owned();
    assert!(
        stderr.contains("=== diff in alpha"),
        "expected banner on stderr when -q and -v cancel: {stderr:?}"
    );
}

#[test]
#[serial]
fn diff_quiet_works_regardless_of_position() {
    // `-q` is global, so `west -q diff` and `west diff -q` must
    // produce identical output. Without the global flag, clap
    // would route `-q` after the subcommand to a subcommand-local
    // arg if one existed — making the flag's meaning depend on
    // position. This test exists to lock that fix in.
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let yaml = manifest_yaml(&[("alpha", &alpha)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);
    touch(&ws, "alpha", "R", "CHANGED");

    // After the subcommand.
    let after = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "diff",
            "-q",
            "--color",
            "never",
        ])
        .assert()
        .success();
    let after_stdout = String::from_utf8_lossy(after.get_output().stdout.as_slice()).into_owned();
    let after_stderr = String::from_utf8_lossy(after.get_output().stderr.as_slice()).into_owned();
    // The banner lives on stderr now, so `-q`'s suppression is what
    // we assert there; stdout never carries it.
    assert!(
        !after_stderr.contains("=== diff in"),
        "`west diff -q`: banner present on stderr: {after_stderr:?}"
    );

    // Before the subcommand.
    let before = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "-q",
            "diff",
            "--color",
            "never",
        ])
        .assert()
        .success();
    let before_stdout = String::from_utf8_lossy(before.get_output().stdout.as_slice()).into_owned();
    let before_stderr = String::from_utf8_lossy(before.get_output().stderr.as_slice()).into_owned();
    assert!(
        !before_stderr.contains("=== diff in"),
        "`west -q diff`: banner present on stderr: {before_stderr:?}"
    );

    // Both positions produce the SAME output on both streams — that's
    // the user expectation the global flag exists to satisfy.
    assert_eq!(after_stdout, before_stdout);
    assert_eq!(after_stderr, before_stderr);
}

#[test]
#[serial]
fn diff_quiet_suppresses_banners_and_empty_summary() {
    if !git_available() {
        return;
    }
    // Mix: one dirty project + one clean. Without `-q` we'd get
    // banner + body for alpha and "Empty diff in 1 project." at
    // the end. With `-q`, only the body should remain.
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let beta = make_bare_with_one_commit(sb.root(), "beta", "B");
    let yaml = manifest_yaml(&[("alpha", &alpha), ("beta", &beta)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);
    touch(&ws, "alpha", "R", "CHANGED");

    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "diff",
            "--quiet",
            "--color",
            "never",
        ])
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(&out.get_output().stdout).into_owned();
    let stderr = String::from_utf8_lossy(&out.get_output().stderr).into_owned();
    // Banner and the "Empty diff in N projects." summary are chrome on
    // stderr; `--quiet` must suppress both there.
    assert!(
        !stderr.contains("=== diff in"),
        "banner present despite --quiet: {stderr:?}"
    );
    assert!(
        !stderr.contains("Empty diff in"),
        "empty-summary present despite --quiet: {stderr:?}"
    );
    // Body still printed to stdout.
    assert!(
        stdout.contains("@@") || stdout.contains("CHANGED"),
        "body missing under --quiet: {stdout:?}"
    );
}

#[test]
#[serial]
fn diff_parallel_does_not_interleave_output() {
    if !git_available() {
        return;
    }
    // Two projects, both dirty. With `-j 2`, the per-project
    // banners and bodies still need to come out as contiguous
    // blocks in workspace order (alpha first, then beta).
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let beta = make_bare_with_one_commit(sb.root(), "beta", "B");
    let yaml = manifest_yaml(&[("alpha", &alpha), ("beta", &beta)]);
    let ws = init_workspace(&sb, &yaml);
    update_all(&sb, &ws);
    touch(&ws, "alpha", "R", "A-CHANGED");
    touch(&ws, "beta", "R", "B-CHANGED");

    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "diff",
            "-j",
            "2",
            "--color",
            "never",
        ])
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(&out.get_output().stdout).into_owned();
    let stderr = String::from_utf8_lossy(&out.get_output().stderr).into_owned();
    // Banners (chrome) land on stderr, in workspace order.
    let alpha_banner = stderr
        .find("=== diff in alpha")
        .unwrap_or_else(|| panic!("missing alpha banner on stderr: {stderr:?}"));
    let beta_banner = stderr
        .find("=== diff in beta")
        .unwrap_or_else(|| panic!("missing beta banner on stderr: {stderr:?}"));
    assert!(alpha_banner < beta_banner, "out-of-order banners: {stderr:?}");
    // Bodies (result) land on stdout as contiguous blocks in
    // workspace order — alpha's hunk fully precedes beta's, with no
    // beta fragment interleaved before it.
    let alpha_body = stdout
        .find("A-CHANGED")
        .unwrap_or_else(|| panic!("missing alpha body on stdout: {stdout:?}"));
    let beta_body = stdout
        .find("B-CHANGED")
        .unwrap_or_else(|| panic!("missing beta body on stdout: {stdout:?}"));
    assert!(alpha_body < beta_body, "out-of-order bodies: {stdout:?}");
    assert!(!stdout.contains("=== diff in"), "banner leaked onto stdout: {stdout:?}");
}
