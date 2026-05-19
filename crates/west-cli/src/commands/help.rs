//! `west help [COMMAND]` — show help for built-ins, aliases, and
//! extension commands in a single place.
//!
//! clap auto-generates a `help` subcommand that covers built-ins, but
//! it doesn't know about aliases (resolved at the `alias::resolve_loop`
//! layer above clap) or extension commands (dispatched via
//! `Command::External`). Disabling clap's auto-help and providing our
//! own lets us bridge all three.
//!
//! Resolution order for `west help <name>`:
//!
//! 1. **Built-in** — clap subcommand by that name. Print its
//!    `--help` via `clap::Command::print_help`. Output matches what
//!    `west <name> --help` produces.
//! 2. **Alias** — `alias.<name>` set in config. Take the first token
//!    of the expansion and recurse. A chain like `alias.a = b` /
//!    `alias.b = list` resolves through to `list`'s help. The
//!    recursion tracks visited names to avoid cycles.
//! 3. **Extension** — anything declared in any project's
//!    `west-commands.yml`. Spawn it with `--help` via the existing
//!    `extension::run` dispatcher; the python extension's argparse
//!    handles its own help output.
//! 4. **Unknown** — `extension::run` already emits
//!    `west: unknown command: <name>` for misses; let it.

use std::collections::HashSet;
use std::ffi::OsString;
use std::process::ExitCode;

use clap::{Args, CommandFactory, Parser};

use super::config::LoadedConfig;
use super::extension;
use crate::Cli;
use crate::alias;

#[derive(Args, Debug)]
pub struct HelpArgs {
    /// Command to print help for. Without a name, shows the top-level
    /// `west` help. Resolves built-ins, aliases (recursively), and
    /// extension commands.
    #[arg(value_name = "COMMAND")]
    pub command: Option<String>,
}

pub fn run(args: HelpArgs, loaded: &LoadedConfig) -> ExitCode {
    match args.command {
        None => print_top_level_help(),
        Some(name) => resolve(&name, loaded, &mut HashSet::new()),
    }
}

fn print_top_level_help() -> ExitCode {
    // Same shape as the subcommand branch: re-parse `west --help`
    // so the rendered output is byte-for-byte identical to what
    // `west --help` produces.
    match Cli::try_parse_from(["west", "--help"]) {
        Ok(_) => ExitCode::SUCCESS,
        Err(e) => e.exit(),
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
