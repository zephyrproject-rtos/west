use std::ffi::OsString;
use std::process::ExitCode;

use clap::Subcommand;

use config::LoadedConfig;

pub mod config;
pub mod exec;
pub mod extension;
pub mod forall;
pub mod help;
pub mod init;
pub mod list;
pub mod manifest;
pub mod select;
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
    /// Run a shell command in each project.
    Forall(forall::ForallArgs),
    /// Show help for a command — built-in, alias, or extension.
    Help(help::HelpArgs),
    /// Initialize a west workspace.
    Init(init::InitArgs),
    /// List projects defined in the manifest.
    List(list::ListArgs),
    /// Inspect, validate, resolve, or freeze the workspace manifest.
    Manifest(manifest::ManifestArgs),
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
        Command::Forall(a) => forall::run(a, &mut loaded),
        Command::Help(a) => help::run(a, &loaded),
        Command::Init(a) => init::run(a, &mut loaded),
        Command::List(a) => list::run(a, &mut loaded),
        Command::Manifest(a) => manifest::run(a, &mut loaded),
        Command::Topdir => topdir::run(),
        Command::Update(a) => update::run(a, &mut loaded),
        Command::External(args) => extension::run(&args, &loaded),
    }
}
