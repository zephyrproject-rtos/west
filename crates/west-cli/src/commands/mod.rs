use std::ffi::OsString;
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
    /// Catch-all for unknown subcommand names. Resolved via aliases when
    /// possible; future PR uses this for extension command lookup.
    #[command(external_subcommand)]
    External(Vec<OsString>),
}

pub fn dispatch(cmd: Command, mut loaded: LoadedConfig) -> ExitCode {
    match cmd {
        Command::Config(a) => config::run(a, &mut loaded),
        Command::Topdir => topdir::run(),
        Command::External(args) => {
            let name = args
                .first()
                .map(|a| a.to_string_lossy().into_owned())
                .unwrap_or_default();
            eprintln!("west: unknown command: {name}");
            ExitCode::FAILURE
        }
    }
}
