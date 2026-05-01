use std::process::ExitCode;

use clap::Subcommand;

pub mod topdir;

#[derive(Subcommand, Debug)]
pub enum Command {
    /// Print the top directory of the west workspace.
    Topdir,
}

pub fn dispatch(cmd: Command) -> ExitCode {
    match cmd {
        Command::Topdir => topdir::run(),
    }
}
