use std::path::PathBuf;
use std::process::ExitCode;

use clap::Parser;
use clap_verbosity_flag::Verbosity;

pub mod commands;

#[derive(Parser, Debug)]
#[command(name = "west", version, about = "The Zephyr RTOS meta-tool")]
pub struct Cli {
    /// Run as if west was started in <DIR>.
    #[arg(short = 'C', value_name = "DIR", global = true)]
    pub chdir: Option<PathBuf>,

    #[command(flatten)]
    pub verbosity: Verbosity,

    #[command(subcommand)]
    pub command: commands::Command,
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
