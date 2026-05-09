use std::ffi::OsString;
use std::path::PathBuf;
use std::process::ExitCode;

use clap::{ArgAction, Args, Parser};
use log::LevelFilter;

pub mod alias;
pub mod commands;
pub mod progress;

#[derive(Parser, Debug)]
#[command(name = "west", version, about = "The Zephyr RTOS meta-tool")]
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
    /// Increase logging verbosity.
    #[arg(short = 'v', long = "verbose", action = ArgAction::Count, conflicts_with = "quiet")]
    pub verbose: u8,
    /// Decrease logging verbosity.
    #[arg(short = 'q', long = "quiet", action = ArgAction::Count)]
    pub quiet: u8,
}

impl VerbosityArgs {
    pub fn log_level_filter(&self) -> LevelFilter {
        let net = i32::from(self.verbose) - i32::from(self.quiet);
        match net {
            i32::MIN..=-1 => LevelFilter::Off,
            0 => LevelFilter::Error,
            1 => LevelFilter::Warn,
            2 => LevelFilter::Info,
            3 => LevelFilter::Debug,
            _ => LevelFilter::Trace,
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

    env_logger::Builder::new()
        .filter_level(initial.verbosity.log_level_filter())
        .init();

    let mut loaded = match commands::config::load(&initial.config_file, &initial.config) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("west: {e}");
            return ExitCode::FAILURE;
        }
    };

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
