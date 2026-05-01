use std::path::PathBuf;
use std::process::ExitCode;

use clap::Parser;

pub mod commands;

#[derive(Parser, Debug)]
#[command(name = "west", version, about = "The Zephyr RTOS meta-tool")]
pub struct Cli {
    /// Run as if west was started in <DIR>.
    #[arg(short = 'C', value_name = "DIR", global = true)]
    pub chdir: Option<PathBuf>,

    #[command(subcommand)]
    pub command: commands::Command,
}

pub fn run() -> ExitCode {
    let cli = Cli::parse();

    if let Some(dir) = &cli.chdir {
        if let Err(e) = std::env::set_current_dir(dir) {
            eprintln!("west: -C {}: {e}", dir.display());
            return ExitCode::FAILURE;
        }
    }

    commands::dispatch(cli.command)
}
