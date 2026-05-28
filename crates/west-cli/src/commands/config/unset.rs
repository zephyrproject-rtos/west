use std::path::Path;
use std::process::ExitCode;

use clap::Args;

use west_core::config::{ConfigError, Configuration};

use crate::exit;
use super::{LoadedConfig, ScopeArgs, scope_to_path};

#[derive(Args, Debug)]
pub struct UnsetArgs {
    /// Configuration option name (e.g. `manifest.path`).
    pub name: String,
    #[command(flatten)]
    pub scope: ScopeArgs,
}

pub fn run(args: UnsetArgs, loaded: &mut LoadedConfig) -> ExitCode {
    if let Some(file) = &args.scope.file {
        return unset_in_single_file(&args.name, file);
    }

    let scope_path = match scope_to_path(&args.scope, &loaded.resolved) {
        Ok(p) => p,
        Err(e) => {
            log::error!("{e}");
            return exit::usage();
        }
    };

    let result = match scope_path {
        Some(p) => loaded.config.delete(&args.name, &p),
        None => loaded.config.delete_topmost(&args.name),
    };

    map_unset_result(&args.name, result)
}

fn unset_in_single_file(name: &str, file: &Path) -> ExitCode {
    let mut single = match Configuration::load([file.to_path_buf()]) {
        Ok(c) => c,
        Err(e) => {
            log::error!("{e}");
            return ExitCode::FAILURE;
        }
    };
    map_unset_result(name, single.delete(name, file))
}

fn map_unset_result(name: &str, result: Result<(), ConfigError>) -> ExitCode {
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(ConfigError::NotFound(_)) => {
            log::error!("not set: {name}");
            ExitCode::FAILURE
        }
        Err(e) => {
            log::error!("{e}");
            exit::usage()
        }
    }
}
