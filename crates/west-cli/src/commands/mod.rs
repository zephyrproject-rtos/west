use std::process::ExitCode;

use clap::Subcommand;

use config::LoadedConfig;

pub mod config;
pub mod topdir;

#[derive(Subcommand, Debug)]
pub enum Command {
    /// Read or write west configuration values.
    Config(config::ConfigArgs),
    /// Print the top directory of the west workspace.
    Topdir,
}

pub fn dispatch(cmd: Command, mut loaded: LoadedConfig) -> ExitCode {
    match cmd {
        Command::Config(a) => config::run(a, &mut loaded),
        Command::Topdir => topdir::run(),
    }
}
