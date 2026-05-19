//! `west help [COMMAND]` — show help for built-ins, aliases, and
//! extension commands in a single place, plus a richer no-arg
//! listing that surfaces extension commands and aliases.
//!
//! clap auto-generates a `help` subcommand that covers built-ins,
//! but it doesn't know about aliases (resolved at the
//! `alias::resolve_loop` layer above clap) or extension commands
//! (dispatched via `Command::External`). Disabling clap's auto-help
//! and providing our own lets us bridge all three.
//!
//! Resolution order for `west help <name>`:
//!
//! 1. **Built-in** — clap subcommand by that name. Print its
//!    `--help` via `Cli::try_parse_from` so the rendered output is
//!    identical to `west <name> --help`.
//! 2. **Alias** — `alias.<name>` set in config. Take the first
//!    token of the expansion and recurse. The recursion tracks
//!    visited names to terminate cycles.
//! 3. **Extension** — anything declared in any project's
//!    `west-commands.yml`. Spawn it with `--help` via the existing
//!    `extension::run` dispatcher.
//! 4. **Unknown** — `extension::run` emits
//!    `west: unknown command: <name>` for misses.
//!
//! No-arg `west help`: prints clap's standard top-level help,
//! followed by an "extension commands from project X (path: Y):"
//! section per project that contributes commands, an "aliases:"
//! section listing every `alias.*` config entry, and a footer
//! pointer to `west help <command>`. Matches python v1's listing
//! shape (minus the built-in subcommand grouping — clap renders
//! built-ins in one flat block today).

use std::collections::HashSet;
use std::ffi::OsString;
use std::process::ExitCode;

use clap::{Args, CommandFactory, Parser};

use super::config::LoadedConfig;
use super::extension;
use crate::Cli;
use crate::alias;
use west_core::config::ConfigValue;

#[derive(Args, Debug)]
pub struct HelpArgs {
    /// Command to print help for. Without a name, shows the top-level
    /// `west` help plus any discoverable extension commands and
    /// configured aliases.
    #[arg(value_name = "COMMAND")]
    pub command: Option<String>,
}

pub fn run(args: HelpArgs, loaded: &LoadedConfig) -> ExitCode {
    match args.command {
        None => print_top_level_help(loaded),
        Some(name) => resolve(&name, loaded, &mut HashSet::new()),
    }
}

fn print_top_level_help(loaded: &LoadedConfig) -> ExitCode {
    // Render the LONG help (matches `west --help`). `render_help`
    // would give the short form (one-liners + "see more with
    // '--help'") which differs visibly from `--help` when any
    // option has multi-paragraph docs. Symmetric with the
    // per-subcommand path below, which re-parses with `--help`.
    let mut app = Cli::command();
    let help_str = app.render_long_help().to_string();
    print!("{help_str}");

    // Extension commands. Only available inside a workspace with a
    // loadable manifest; outside, silently skip — `west help` from
    // a fresh shell shouldn't error just because there's no
    // workspace nearby. Any discovery error (vcs missing, yaml
    // parse failure) takes the same skip path.
    if let Ok(groups) = extension::list_for_help(loaded) {
        for group in groups {
            println!(
                "\nextension commands from project {} (path: {}):",
                group.project,
                group.path.display()
            );
            print_indented_two_columns(&group.commands);
        }
    }

    // Aliases. `Configuration::items()` walks every layer and
    // merges; filter by `alias.` prefix and stringify values back
    // to a shell-style expansion. Sorted alphabetically for stable
    // output across runs (config layers don't guarantee order).
    let aliases = collect_aliases(loaded);
    if !aliases.is_empty() {
        println!("\naliases:");
        print_indented_two_columns(&aliases);
    }

    println!();
    println!("Run \"west help <command>\" for help on each <command>.");
    ExitCode::SUCCESS
}

/// Format `(name, description)` pairs as a two-column block in
/// the same shape clap's own `Commands:` section uses: indented
/// name, gap, description. No colon between the two — clap's
/// built-in listing doesn't use one, and matching that keeps the
/// extension/alias sections visually consistent with the rest of
/// the help output.
fn print_indented_two_columns(rows: &[(String, String)]) {
    // Fixed column width keeps long names (e.g. `zephyr-export`,
    // `menuconfig`) from cramping shorter ones across sections.
    // 22 chars is comfortable for the longest commonly-seen
    // extension names without truncating.
    const NAME_COL: usize = 22;
    for (name, desc) in rows {
        let label = format!("  {name}");
        if desc.is_empty() {
            println!("{label}");
        } else if label.len() < NAME_COL {
            println!("{label:<NAME_COL$}{desc}");
        } else {
            // Long names get the description on the same line with
            // one space separator — overflowing the column is
            // preferable to wrapping for one-line greppability.
            println!("{label} {desc}");
        }
    }
}

fn collect_aliases(loaded: &LoadedConfig) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    for (key, value) in loaded.config.items() {
        if let Some(name) = key.strip_prefix("alias.") {
            out.push((name.to_owned(), alias_value_to_str(&value)));
        }
    }
    out.sort_by(|a, b| a.0.cmp(&b.0));
    out
}

/// Render an `alias.<name>` value as a shell-style expansion
/// string. Mirrors how `alias::lookup` interprets the same value
/// at dispatch time (string → split; list → already-split tokens).
fn alias_value_to_str(value: &ConfigValue) -> String {
    match value {
        ConfigValue::String(s) => {
            if s.is_empty() {
                "<empty>".to_owned()
            } else {
                s.clone()
            }
        }
        ConfigValue::List(items) => items
            .iter()
            .filter_map(|v| match v {
                ConfigValue::String(s) => Some(s.clone()),
                _ => None,
            })
            .collect::<Vec<_>>()
            .join(" "),
        ConfigValue::Bool(b) => b.to_string(),
        ConfigValue::Integer(i) => i.to_string(),
        ConfigValue::Float(f) => f.to_string(),
    }
}

/// Resolve `name` against (built-in, alias, extension) in that order.
/// `visited` carries the alias chain so a cycle (`alias.a = b`,
/// `alias.b = a`) terminates rather than recursing forever — matches
/// the cycle-set termination `alias::resolve_loop` uses above clap.
fn resolve(name: &str, loaded: &LoadedConfig, visited: &mut HashSet<String>) -> ExitCode {
    // 1. Built-in subcommand. Re-parse via clap's full pipeline with
    //    `--help` appended so the rendered output is identical to
    //    `west <name> --help` (Usage line includes the `west`
    //    prefix, sections match exactly). clap exits the process
    //    on a successful --help via `e.exit()`.
    if Cli::command().find_subcommand(name).is_some() {
        let argv = ["west", name, "--help"];
        match Cli::try_parse_from(argv) {
            Ok(_) => return ExitCode::SUCCESS,
            Err(e) => e.exit(),
        }
    }

    // 2. Alias. First-token of the expansion drives the recursion.
    //    A naked-flag first token can't appear (alias::lookup rejects
    //    those). An empty alias also can't reach here.
    if !visited.insert(name.to_owned()) {
        eprintln!("west: alias cycle resolving help for {name:?}");
        return ExitCode::FAILURE;
    }
    match alias::lookup(&loaded.config, name) {
        Ok(Some(expanded)) => {
            let next = expanded.first().cloned().unwrap_or_default();
            return resolve(&next, loaded, visited);
        }
        Ok(None) => {}
        Err(e) => {
            eprintln!("west: {e}");
            return ExitCode::FAILURE;
        }
    }

    // 3. Extension. Forward to the existing dispatcher with `--help`
    //    appended — it knows how to discover extensions and shell
    //    out to python. A miss surfaces as
    //    `west: unknown command: <name>` from `extension::run` itself.
    let argv = vec![OsString::from(name), OsString::from("--help")];
    extension::run(&argv, loaded)
}
