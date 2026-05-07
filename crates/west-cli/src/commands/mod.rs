use std::ffi::OsString;
use std::process::ExitCode;

use clap::Subcommand;

use config::LoadedConfig;

pub mod config;
pub mod exec;
pub mod init;
pub mod topdir;
pub mod update;

#[derive(Subcommand, Debug)]
pub enum Command {
    /// Read or write west configuration values.
    Config(config::ConfigArgs),
    /// Run an external program. Useful with aliases to invoke `west` itself
    /// with top-level flags that aliases can't carry directly. Use `--` to
    /// be unambiguous about where exec's args end and the target program's
    /// args begin: `west exec -- python -c 'print(1+1)'`.
    Exec(exec::ExecArgs),
    /// Initialize a west workspace.
    Init(init::InitArgs),
    /// Print the top directory of the west workspace.
    Topdir,
    /// Update projects to their manifest revisions.
    Update(update::UpdateArgs),
    /// Catch-all for unknown subcommand names. Resolved via aliases when
    /// possible; future PR uses this for extension command lookup.
    #[command(external_subcommand)]
    External(Vec<OsString>),
}

pub fn dispatch(cmd: Command, mut loaded: LoadedConfig) -> ExitCode {
    match cmd {
        Command::Config(a) => config::run(a, &mut loaded),
        Command::Exec(a) => exec::run(a),
        Command::Init(a) => init::run(a, &mut loaded),
        Command::Topdir => topdir::run(),
        Command::Update(a) => update::run(a, &mut loaded),
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
