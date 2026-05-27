use std::ffi::OsString;
use std::path::PathBuf;
use std::process::ExitCode;

use clap::{ArgAction, Args, Parser};
use console::Style;
use log::LevelFilter;

pub mod alias;
pub mod commands;
pub mod progress;

// PyO3 bindings backing the python `_west_native` extension module.
// Gated on the `pyo3` feature so `cargo install west-cli` from
// crates.io doesn't pull pyo3 in for the python-free niche audience.
#[cfg(feature = "pyo3")]
mod python;

#[derive(Parser, Debug)]
#[command(
    name = "west",
    version,
    about = "The Zephyr RTOS meta-tool",
    // We provide our own `Help` subcommand (commands::help) so we
    // can resolve aliases and extension commands in addition to
    // built-ins. Without disabling clap's auto-help, both compete
    // for the `help` name.
    disable_help_subcommand = true
)]
pub struct Cli {
    /// Run as if west was started in <DIR>.
    #[arg(short = 'C', value_name = "DIR")]
    pub chdir: Option<PathBuf>,

    #[command(flatten)]
    pub verbosity: VerbosityArgs,

    /// Additional configuration options. NAME is a TOML dotted key; VALUE is
    /// a TOML expression. Bare strings (without TOML constructs) may omit
    /// quotes. Repeatable.
    #[arg(long = "config", value_name = "NAME=VALUE", action = ArgAction::Append)]
    pub config: Vec<String>,

    /// Additional configuration files, appended at top file-backed precedence.
    /// Repeatable.
    #[arg(long = "config-file", value_name = "PATH", action = ArgAction::Append)]
    pub config_file: Vec<PathBuf>,

    /// Disable progress bars; the underlying tool's stdio is attached
    /// directly to the terminal. For `update`, also forces `-j 1` —
    /// interleaved native git output across N projects is unreadable.
    /// Equivalent to `--config output.raw=true`.
    #[arg(long, global = true)]
    pub raw: bool,

    #[command(subcommand)]
    pub command: commands::Command,
}

// `-v` / `-q` count flags driving the `log` crate's `LevelFilter`.
//
// Default: `Error`. `-v` → `Warn`, `-vv` → `Info`, `-vvv` → `Debug`,
// `-vvvv` → `Trace`. `-q` subtracts; multiple `-q`s silence the logger
// entirely.
#[derive(Args, Debug)]
pub struct VerbosityArgs {
    /// Increase logging verbosity. Composes with `-q`: the net is
    /// `verbose - quiet`, used for both `log_level_filter` and
    /// the `output.quiet` splice. `west -q -v` is a no-op.
    #[arg(short = 'v', long = "verbose", action = ArgAction::Count, global = true)]
    pub verbose: u8,
    /// Decrease logging verbosity. Also suppresses per-project
    /// chrome (banners, summary lines) in commands that emit it
    /// (`west diff`, …).
    ///
    /// Marked `global = true` so `-q` is accepted at any position
    /// — `west -q diff` and `west diff -q` behave identically.
    /// Without `global`, clap routes `-q` after the subcommand to
    /// a subcommand-local flag if one exists, which made `-q`'s
    /// meaning depend on its position. See the inline splice
    /// below: when set, we splice `output.quiet = true` into the
    /// config so subcommands have a single canonical place to
    /// read the choice from.
    #[arg(short = 'q', long = "quiet", action = ArgAction::Count, global = true)]
    pub quiet: u8,
}

impl VerbosityArgs {
    pub fn log_level_filter(&self) -> LevelFilter {
        let net = i32::from(self.verbose) - i32::from(self.quiet);
        // Warnings are visible by default — they're actionable, not
        // progress (the indicatif bars are the progress UI). Info and
        // below are opt-in via `-v`. `-q` drops to errors-only and
        // `-qq` silences everything.
        match net {
            i32::MIN..=-2 => LevelFilter::Off, // -qq (and beyond)
            -1 => LevelFilter::Error,          // -q
            0 => LevelFilter::Warn,            // default
            1 => LevelFilter::Info,            // -v
            2 => LevelFilter::Debug,           // -vv
            _ => LevelFilter::Trace,           // -vvv+
        }
    }
}

pub fn run() -> ExitCode {
    let argv: Vec<OsString> = std::env::args_os().collect();

    // Initial parse: read top-level flags (-C, -v/-q, --config, --config-file)
    // from the user's actual argv. These are the only sources for those flags;
    // aliases never propagate them.
    let initial = Cli::parse_from(&argv);

    if let Some(dir) = &initial.chdir {
        if let Err(e) = std::env::set_current_dir(dir) {
            eprintln!("west: -C {}: {e}", dir.display());
            return ExitCode::FAILURE;
        }
    }

    // Clean, tool-like log lines — no timestamp or Rust module path.
    // `error:`/`warning:` get a coloured prefix (NO_COLOR / non-tty
    // aware via `console`, which inspects stderr); info/debug are the
    // bare message (milestone/diagnostic content already carries its
    // own `project:` framing); trace keeps the target for deep
    // debugging. All on stderr, so stdout stays machine-readable.
    env_logger::Builder::new()
        .filter_level(initial.verbosity.log_level_filter())
        .format(|buf, record| {
            use std::io::Write;
            let msg = record.args();
            match record.level() {
                log::Level::Error => writeln!(
                    buf,
                    "{} {msg}",
                    Style::new().red().bold().for_stderr().apply_to("west: error:")
                ),
                log::Level::Warn => writeln!(
                    buf,
                    "{} {msg}",
                    Style::new().yellow().bold().for_stderr().apply_to("west: warning:")
                ),
                log::Level::Info | log::Level::Debug => writeln!(buf, "{msg}"),
                log::Level::Trace => writeln!(buf, "[{}] {msg}", record.target()),
            }
        })
        .init();

    let mut loaded = match commands::config::load(&initial.config_file, &initial.config) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("west: {e}");
            return ExitCode::FAILURE;
        }
    };

    // "Net-quiet" — `-v` cancels `-q`. `west -q -v diff` produces
    // net 0 and shouldn't suppress chrome. Mirrors the same
    // verbose - quiet arithmetic `log_level_filter` uses for the
    // env_logger threshold; both consume the same primitive so
    // `-q` and `-v` compose consistently across log level AND
    // banner suppression.
    let net_verbosity =
        i32::from(initial.verbosity.verbose) - i32::from(initial.verbosity.quiet);
    if net_verbosity < 0
        && let Err(e) = commands::config::splice_inline(
            &mut loaded.config,
            "output.quiet",
            west_core::config::ConfigValue::Bool(true),
        )
    {
        eprintln!("west: -q: {e}");
        return ExitCode::FAILURE;
    }

    if initial.raw
        && let Err(e) = commands::config::splice_inline(
            &mut loaded.config,
            "output.raw",
            west_core::config::ConfigValue::Bool(true),
        )
    {
        eprintln!("west: --raw: {e}");
        return ExitCode::FAILURE;
    }

    // Resolve aliases. Re-parses argv on each iteration; aliases never inject
    // top-level flags (validated in `alias::lookup`) so the initial chdir,
    // logger, and config state remain valid throughout.
    let cli = match alias::resolve_loop(argv, &loaded.config) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("west: {e}");
            return ExitCode::FAILURE;
        }
    };

    commands::dispatch(cli.command, loaded)
}
