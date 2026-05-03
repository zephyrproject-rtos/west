use std::path::PathBuf;
use std::process::ExitCode;

use clap::{ArgAction, Args, Parser};
use log::LevelFilter;

pub mod commands;

#[derive(Parser, Debug)]
#[command(name = "west", version, about = "The Zephyr RTOS meta-tool")]
pub struct Cli {
    /// Run as if west was started in <DIR>.
    #[arg(short = 'C', value_name = "DIR")]
    pub chdir: Option<PathBuf>,

    #[command(flatten)]
    pub verbosity: VerbosityArgs,

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
    let cli = Cli::parse();

    env_logger::Builder::new()
        .filter_level(cli.verbosity.log_level_filter())
        .init();

    if let Some(dir) = &cli.chdir {
        if let Err(e) = std::env::set_current_dir(dir) {
            eprintln!("west: -C {}: {e}", dir.display());
            return ExitCode::FAILURE;
        }
    }

    commands::dispatch(cli.command)
}
