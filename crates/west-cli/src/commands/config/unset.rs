use std::process::ExitCode;

use clap::Args;

use west_core::config::ConfigError;

use super::{LoadedConfig, ScopeArgs, load, scope_to_path};

#[derive(Args, Debug)]
pub struct UnsetArgs {
    /// Configuration option name (e.g. `manifest.path`).
    pub name: String,
    #[command(flatten)]
    pub scope: ScopeArgs,
}

pub fn run(args: UnsetArgs) -> ExitCode {
    let extras: Vec<_> = args.scope.file.iter().cloned().collect();
    let LoadedConfig {
        resolved,
        mut config,
    } = match load(&extras) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("west: {e}");
            return ExitCode::FAILURE;
        }
    };

    let scope_path = match scope_to_path(&args.scope, &resolved) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("west: {e}");
            return ExitCode::from(2);
        }
    };

    let result = match scope_path {
        Some(p) => config.delete(&args.name, &p),
        None => config.delete_topmost(&args.name),
    };

    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(ConfigError::NotFound(_)) => {
            eprintln!("west: not set: {}", args.name);
            ExitCode::FAILURE
        }
        Err(e) => {
            eprintln!("west: {e}");
            ExitCode::from(2)
        }
    }
}
