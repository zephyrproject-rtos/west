//! Integration tests for `west manifest`. Sandbox + bare-repo fixture
//! pattern mirroring the other CLI integration tests. Same
//! boilerplate is now in four files — extraction to a shared helper
//! is on the architectural-review todo list.

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

/// Initialize a workspace whose manifest repo's `west.yml` is `yaml`.
/// `manifest_file_name` lets the test pick `west.yml` / `west.toml` /
/// `west.json` for the format-dispatch cases.
fn init_workspace(sb: &Sandbox, manifest_file_name: &str, body: &str) -> PathBuf {
    let manifest_work = sb.root().join("manifest-work");
    std::fs::create_dir_all(&manifest_work).unwrap();
    git(
        &["init", "-q", "--initial-branch=main", "."],
        &manifest_work,
    );
    std::fs::write(manifest_work.join(manifest_file_name), body).unwrap();
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
    let mut init = sb.west();
    init.args([
        "init",
        "--url",
        bare.to_str().unwrap(),
        "--mf",
        manifest_file_name,
        workspace.to_str().unwrap(),
    ]);
    init.assert().success();
    workspace
}

fn yaml_with_two_projects(p1_url: &Path, p2_url: &Path) -> String {
    format!(
        "manifest:\n  projects:\n    - name: alpha\n      url: {a}\n      revision: main\n    - name: beta\n      url: {b}\n      revision: main\n",
        a = p1_url.display(),
        b = p2_url.display()
    )
}

// ============================================================================
// Tests
// ============================================================================

#[test]
#[serial]
fn manifest_path_prints_active_manifest_path() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let beta = make_bare_with_one_commit(sb.root(), "beta", "B");
    let yaml = yaml_with_two_projects(&alpha, &beta);
    let ws = init_workspace(&sb, "west.yml", &yaml);

    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "manifest", "--path"])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let printed = String::from_utf8(out).unwrap();
    let expected = ws.canonicalize().unwrap().join("manifest").join("west.yml");
    assert_eq!(
        printed.trim(),
        expected.to_string_lossy(),
        "got {printed:?}"
    );
}

#[test]
#[serial]
fn manifest_validate_succeeds_on_valid_manifest() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let beta = make_bare_with_one_commit(sb.root(), "beta", "B");
    let yaml = yaml_with_two_projects(&alpha, &beta);
    let ws = init_workspace(&sb, "west.yml", &yaml);

    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "manifest", "--validate"])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let printed = String::from_utf8(out).unwrap();
    assert!(
        printed.contains("manifest is valid"),
        "expected success message, got {printed:?}"
    );
}

#[test]
#[serial]
fn manifest_validate_errors_on_malformed_manifest() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    // Manifest with a project that has no url and no remote — invalid.
    let yaml = "manifest:\n  projects:\n    - name: oops\n";
    let ws = init_workspace(&sb, "west.yml", yaml);

    sb.west()
        .args(["-C", ws.to_str().unwrap(), "manifest", "--validate"])
        .assert()
        .failure();
}

#[test]
#[serial]
fn manifest_resolve_emits_yaml_round_trippable_through_parser() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let beta = make_bare_with_one_commit(sb.root(), "beta", "B");
    let yaml = yaml_with_two_projects(&alpha, &beta);
    let ws = init_workspace(&sb, "west.yml", &yaml);

    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "manifest", "--resolve"])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let printed = String::from_utf8(out).unwrap();
    assert!(printed.contains("name: alpha"), "got: {printed}");
    assert!(printed.contains("name: beta"), "got: {printed}");
    // Round-trip the output back through the parser.
    let reparsed = west_core::manifest::Manifest::from_yaml_str(&printed)
        .expect("emitted YAML should reparse");
    let names: Vec<_> = reparsed.projects.iter().map(|p| p.name.as_str()).collect();
    assert_eq!(names, vec!["alpha", "beta"]);
}

#[test]
#[serial]
fn manifest_resolve_format_defaults_to_source_extension() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let beta = make_bare_with_one_commit(sb.root(), "beta", "B");
    // TOML manifest — equivalent shape to yaml fixture.
    let toml_body = format!(
        "[[manifest.projects]]\nname = \"alpha\"\nurl = \"{a}\"\nrevision = \"main\"\n\n[[manifest.projects]]\nname = \"beta\"\nurl = \"{b}\"\nrevision = \"main\"\n",
        a = alpha.display(),
        b = beta.display()
    );
    let ws = init_workspace(&sb, "west.toml", &toml_body);

    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "manifest", "--resolve"])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let printed = String::from_utf8(out).unwrap();
    // TOML output uses `name = "alpha"`-style key=value lines; YAML
    // would use `name: alpha`. Either way it should be parseable back.
    let reparsed = west_core::manifest::Manifest::from_toml_str(&printed)
        .expect("emitted TOML should reparse");
    assert_eq!(reparsed.projects.len(), 2);
}

#[test]
#[serial]
fn manifest_resolve_explicit_format_overrides_default() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let beta = make_bare_with_one_commit(sb.root(), "beta", "B");
    let yaml = yaml_with_two_projects(&alpha, &beta);
    let ws = init_workspace(&sb, "west.yml", &yaml);

    let out = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "manifest",
            "--resolve",
            "--format",
            "json",
        ])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let printed = String::from_utf8(out).unwrap();
    // JSON-shaped (curly braces, quoted keys), not YAML.
    assert!(printed.trim_start().starts_with('{'), "got: {printed}");
    let reparsed = west_core::manifest::Manifest::from_json_str(&printed)
        .expect("emitted JSON should reparse");
    let names: Vec<_> = reparsed.projects.iter().map(|p| p.name.as_str()).collect();
    assert_eq!(names, vec!["alpha", "beta"]);
}

#[test]
#[serial]
fn manifest_resolve_writes_to_out_path() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let beta = make_bare_with_one_commit(sb.root(), "beta", "B");
    let yaml = yaml_with_two_projects(&alpha, &beta);
    let ws = init_workspace(&sb, "west.yml", &yaml);

    let target = sb.root().join("resolved.yml");
    let stdout = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "manifest",
            "--resolve",
            "--out",
            target.to_str().unwrap(),
        ])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    assert!(stdout.is_empty(), "stdout should be empty when --out used");
    let body = std::fs::read_to_string(&target).unwrap();
    assert!(body.contains("name: alpha"), "got: {body}");
}

#[test]
#[serial]
fn manifest_freeze_replaces_revision_with_sha() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let beta = make_bare_with_one_commit(sb.root(), "beta", "B");
    let yaml = yaml_with_two_projects(&alpha, &beta);
    let ws = init_workspace(&sb, "west.yml", &yaml);

    // freeze needs the projects cloned.
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update", "-j", "1"])
        .assert()
        .success();

    let out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "manifest", "--freeze"])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let printed = String::from_utf8(out).unwrap();
    let reparsed = west_core::manifest::Manifest::from_yaml_str(&printed).unwrap();
    for p in &reparsed.projects {
        // Each revision is a full SHA (40 hex chars), not "main".
        assert_eq!(
            p.revision.len(),
            40,
            "expected SHA, got {} for {}",
            p.revision,
            p.name
        );
        assert!(
            p.revision.chars().all(|c| c.is_ascii_hexdigit()),
            "not hex: {} for {}",
            p.revision,
            p.name
        );
    }
}

#[test]
#[serial]
fn manifest_freeze_errors_when_project_not_cloned() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let beta = make_bare_with_one_commit(sb.root(), "beta", "B");
    let yaml = yaml_with_two_projects(&alpha, &beta);
    let ws = init_workspace(&sb, "west.yml", &yaml);
    // NB: no `west update` — projects aren't cloned.

    let err = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "manifest", "--freeze"])
        .assert()
        .failure()
        .get_output()
        .stderr
        .clone();
    let printed = String::from_utf8(err).unwrap();
    assert!(printed.contains("not cloned"), "got stderr: {printed:?}");
}

#[test]
#[serial]
fn manifest_actions_are_mutually_exclusive() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let beta = make_bare_with_one_commit(sb.root(), "beta", "B");
    let yaml = yaml_with_two_projects(&alpha, &beta);
    let ws = init_workspace(&sb, "west.yml", &yaml);

    // `--path` AND `--validate` together: clap should reject.
    sb.west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "manifest",
            "--path",
            "--validate",
        ])
        .assert()
        .failure();
}

#[test]
#[serial]
fn manifest_requires_an_action() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let beta = make_bare_with_one_commit(sb.root(), "beta", "B");
    let yaml = yaml_with_two_projects(&alpha, &beta);
    let ws = init_workspace(&sb, "west.yml", &yaml);

    // Bare `west manifest` (no action flag) should fail.
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "manifest"])
        .assert()
        .failure();
}

// ----- --untracked --------------------------------------------------------

/// Set up a workspace + run `west update` so projects are cloned.
/// `--untracked` only prunes directories that actually exist, so a
/// proper "clean workspace" baseline needs projects on disk.
fn updated_workspace(sb: &Sandbox) -> PathBuf {
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let beta = make_bare_with_one_commit(sb.root(), "beta", "B");
    let yaml = yaml_with_two_projects(&alpha, &beta);
    let ws = init_workspace(sb, "west.yml", &yaml);
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update", "-j", "1"])
        .assert()
        .success();
    ws
}

fn run_untracked(sb: &Sandbox, ws: &Path, extra_args: &[&str]) -> String {
    let mut args: Vec<String> = vec![
        "-C".into(),
        ws.to_str().unwrap().into(),
        "manifest".into(),
        "--untracked".into(),
    ];
    args.extend(extra_args.iter().map(|s| s.to_string()));
    let out = sb
        .west()
        .args(&args)
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    String::from_utf8(out).unwrap()
}

#[test]
#[serial]
fn manifest_untracked_empty_on_clean_workspace() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let ws = updated_workspace(&sb);
    let printed = run_untracked(&sb, &ws, &[]);
    assert_eq!(printed, "", "fresh workspace should have no orphan files; got: {printed:?}");
}

#[test]
#[serial]
fn manifest_untracked_lists_top_level_orphan_file() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let ws = updated_workspace(&sb);
    std::fs::write(ws.join("orphan.txt"), b"hi\n").unwrap();
    let printed = run_untracked(&sb, &ws, &[]);
    let lines: Vec<&str> = printed.lines().collect();
    assert_eq!(lines, vec!["orphan.txt"], "got: {printed:?}");
}

#[test]
#[serial]
fn manifest_untracked_skips_west_dir() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let ws = updated_workspace(&sb);
    // `.west/` already contains real west state (a config file at
    // minimum). It must NOT appear in --untracked output.
    let printed = run_untracked(&sb, &ws, &[]);
    assert!(
        !printed.contains(".west/"),
        ".west/ contents leaked into output: {printed:?}"
    );
}

#[test]
#[serial]
fn manifest_untracked_skips_manifest_repo_tree() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let ws = updated_workspace(&sb);
    // Resolve the manifest repo's directory via `west manifest --path`
    // (the manifest YAML's `self.path` defaults to "manifest" but it's
    // cleaner to ask the binary than to encode that assumption).
    let mp_out = sb
        .west()
        .args(["-C", ws.to_str().unwrap(), "manifest", "--path"])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let manifest_file = PathBuf::from(String::from_utf8(mp_out).unwrap().trim().to_owned());
    let manifest_dir = manifest_file.parent().unwrap();
    // Drop a file inside the manifest repo's tree — it should be
    // owned by the synthetic ManifestProject and not appear.
    std::fs::write(manifest_dir.join("scratch.txt"), b"x").unwrap();
    let printed = run_untracked(&sb, &ws, &[]);
    assert!(
        !printed.contains("scratch.txt"),
        "manifest-repo file leaked: {printed:?}"
    );
}

#[test]
#[serial]
fn manifest_untracked_skips_cloned_project_tree() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let ws = updated_workspace(&sb);
    // Both projects (alpha + beta) were cloned with a file named `R`
    // each. Confirm those don't appear.
    let printed = run_untracked(&sb, &ws, &[]);
    assert!(!printed.contains("alpha/R"), "alpha tree leaked: {printed:?}");
    assert!(!printed.contains("beta/R"), "beta tree leaked: {printed:?}");
}

#[test]
#[serial]
fn manifest_untracked_writes_to_out_path() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let ws = updated_workspace(&sb);
    std::fs::write(ws.join("orphan.txt"), b"hi\n").unwrap();

    let target = sb.root().join("out.txt");
    let stdout = sb
        .west()
        .args([
            "-C",
            ws.to_str().unwrap(),
            "manifest",
            "--untracked",
            "--out",
            target.to_str().unwrap(),
        ])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    assert!(stdout.is_empty(), "stdout should be empty with --out");
    let body = std::fs::read_to_string(&target).unwrap();
    assert!(body.lines().any(|l| l == "orphan.txt"), "got file: {body:?}");
}

#[test]
#[serial]
fn manifest_untracked_sorts_output() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let ws = updated_workspace(&sb);
    std::fs::write(ws.join("z-late.txt"), b"").unwrap();
    std::fs::write(ws.join("a-early.txt"), b"").unwrap();
    std::fs::write(ws.join("m-mid.txt"), b"").unwrap();
    let printed = run_untracked(&sb, &ws, &[]);
    let lines: Vec<&str> = printed.lines().collect();
    assert_eq!(
        lines,
        vec!["a-early.txt", "m-mid.txt", "z-late.txt"],
        "expected alphabetical; got: {printed:?}"
    );
}

#[test]
#[serial]
fn manifest_untracked_format_json_wraps_under_untracked_key() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let ws = updated_workspace(&sb);
    std::fs::write(ws.join("orphan.txt"), b"hi\n").unwrap();
    let printed = run_untracked(&sb, &ws, &["--format", "json"]);
    let parsed: serde_json::Value = serde_json::from_str(&printed).expect("valid JSON");
    let list = parsed
        .get("untracked")
        .and_then(|v| v.as_array())
        .expect("untracked is an array");
    let strings: Vec<&str> = list.iter().filter_map(|v| v.as_str()).collect();
    assert!(
        strings.contains(&"orphan.txt"),
        "expected orphan.txt in {strings:?}"
    );
}

#[test]
#[serial]
fn manifest_untracked_paths_are_cwd_relative() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let ws = updated_workspace(&sb);
    // Drop the orphan inside the workspace and also create a
    // subdirectory we'll set as the process's cwd. With cwd ==
    // <ws>/alpha-sub, the orphan at <ws>/orphan.txt should print
    // as `../orphan.txt`, matching v1's
    // `os.path.relpath(u, Path.cwd())` semantics. (`alpha-sub` is
    // itself an untracked dir — that's fine for this test.)
    std::fs::write(ws.join("orphan.txt"), b"hi\n").unwrap();
    let subdir = ws.join("alpha-sub");
    std::fs::create_dir_all(&subdir).unwrap();

    // Drive west with cwd == subdir, no `-C`.
    let out = sb
        .west()
        .current_dir(&subdir)
        .args(["manifest", "--untracked"])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let printed = String::from_utf8(out).unwrap();
    let lines: Vec<&str> = printed.lines().collect();
    assert!(
        lines.contains(&"../orphan.txt"),
        "expected ../orphan.txt in cwd-relative output: {printed:?}"
    );
    // Sanity: NOT the workspace-rooted form.
    assert!(
        !lines.contains(&"orphan.txt"),
        "unexpected workspace-rooted path: {printed:?}"
    );
}

#[test]
#[serial]
fn manifest_untracked_collapses_untracked_directory() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let ws = updated_workspace(&sb);
    // An entire orphan subtree should collapse to a single output
    // line — the directory itself, not every file inside it.
    // Matches v1's `west manifest --untracked` shape.
    let orphan_dir = ws.join("orphan-dir");
    std::fs::create_dir_all(orphan_dir.join("nested/deep")).unwrap();
    std::fs::write(orphan_dir.join("a.txt"), b"").unwrap();
    std::fs::write(orphan_dir.join("b.txt"), b"").unwrap();
    std::fs::write(orphan_dir.join("nested/c.txt"), b"").unwrap();
    std::fs::write(orphan_dir.join("nested/deep/d.txt"), b"").unwrap();

    let printed = run_untracked(&sb, &ws, &[]);
    let lines: Vec<&str> = printed.lines().collect();
    assert_eq!(
        lines,
        vec!["orphan-dir"],
        "expected the orphan dir collapsed to a single line; got: {printed:?}"
    );
}

#[test]
#[serial]
fn manifest_untracked_recurses_around_nested_project() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    // Build a workspace where one project lives under a `nested/`
    // dir so the walker has to recurse INTO `nested/` (it contains
    // an owned root) but EMIT the orphan sibling next to it.
    let alpha = make_bare_with_one_commit(sb.root(), "alpha", "A");
    let beta = make_bare_with_one_commit(sb.root(), "beta", "B");
    let yaml = format!(
        "manifest:\n  projects:\n    - name: alpha\n      url: {alpha}\n      revision: main\n      path: nested/alpha\n    - name: beta\n      url: {beta}\n      revision: main\n",
        alpha = alpha.display(),
        beta = beta.display(),
    );
    let ws = init_workspace(&sb, "west.yml", &yaml);
    sb.west()
        .args(["-C", ws.to_str().unwrap(), "update", "-j", "1"])
        .assert()
        .success();
    // Drop a sibling file/dir next to the nested project. The
    // walker must recurse into `nested/` to find them, but NOT
    // descend into `nested/alpha/` (it's a project root).
    std::fs::create_dir_all(ws.join("nested/sibling-dir/inside")).unwrap();
    std::fs::write(ws.join("nested/sibling-file.txt"), b"").unwrap();
    std::fs::write(ws.join("nested/sibling-dir/inside/x"), b"").unwrap();

    let printed = run_untracked(&sb, &ws, &[]);
    let lines: Vec<&str> = printed.lines().collect();
    assert!(
        lines.contains(&"nested/sibling-file.txt"),
        "expected sibling file in {printed:?}"
    );
    assert!(
        lines.contains(&"nested/sibling-dir"),
        "expected sibling dir collapsed in {printed:?}"
    );
    assert!(
        !lines.contains(&"nested/sibling-dir/inside/x"),
        "should not have recursed into the collapsed sibling dir: {printed:?}"
    );
    assert!(
        !lines.iter().any(|l| l.starts_with("nested/alpha/")),
        "must not list contents of the nested project: {printed:?}"
    );
}

#[test]
#[serial]
fn manifest_untracked_format_toml_top_level_table() {
    if !git_available() {
        return;
    }
    let sb = Sandbox::new();
    let ws = updated_workspace(&sb);
    std::fs::write(ws.join("orphan.txt"), b"hi\n").unwrap();
    let printed = run_untracked(&sb, &ws, &["--format", "toml"]);
    // Round-trip through the toml parser back into JSON to assert
    // structure independent of TOML's exact formatting.
    let parsed: serde_json::Value = toml_edit::de::from_str(&printed).expect("valid TOML");
    let list = parsed
        .get("untracked")
        .and_then(|v| v.as_array())
        .expect("untracked is an array");
    assert!(
        list.iter().any(|v| v.as_str() == Some("orphan.txt")),
        "expected orphan.txt in {list:?}"
    );
}
