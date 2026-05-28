use std::path::{Path, PathBuf};
use std::process::ExitCode;

use clap::Args;

use west_core::config::{ConfigError, Configuration};

use super::{LoadedConfig, ScopeArgs, scope_to_path};
use crate::exit;

#[derive(Args, Debug)]
pub struct UnsetArgs {
    /// Configuration option name (e.g. `manifest.path`).
    pub name: String,

    /// Delete `name` from every file-backed layer that holds it
    /// (system + global + `conf.d/*` + local + any `--config-file`
    /// paths). Mutually exclusive with the scope flags. Succeeds if
    /// at least one layer had the key; errors if none did. Mirrors
    /// v1's `west config -D`.
    #[arg(
        short = 'D',
        long = "delete-all",
        conflicts_with_all = ["system", "global", "local", "file"],
    )]
    pub delete_all: bool,

    #[command(flatten)]
    pub scope: ScopeArgs,
}

pub fn run(args: UnsetArgs, loaded: &mut LoadedConfig) -> ExitCode {
    if args.delete_all {
        return run_delete_all(&args.name, &mut loaded.config);
    }
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

/// `-D / --delete-all`: walk every file-backed layer the loaded
/// configuration knows about and delete `name` from each that holds
/// it. The inline-overrides layer (from `--config NAME=VALUE`) is
/// read-only and isn't visited; layers whose file doesn't exist on
/// disk yet won't contain the key either way, so `NotFound` from
/// them is silently swallowed.
fn run_delete_all(name: &str, config: &mut Configuration) -> ExitCode {
    let layer_paths: Vec<PathBuf> = config
        .layer_paths()
        .iter()
        .map(|p| p.to_path_buf())
        .collect();
    let mut deleted_from: Vec<PathBuf> = Vec::new();
    for path in &layer_paths {
        match config.delete(name, path) {
            Ok(()) => deleted_from.push(path.clone()),
            // Layer didn't hold the key — that's the common case for
            // the layers we walk past; keep going.
            Err(ConfigError::NotFound(_)) => continue,
            Err(ConfigError::InvalidKey(_)) => {
                // Malformed `name` (e.g. no dot). Same diagnostic the
                // single-scope path would surface; bail without
                // touching further layers.
                log::error!("invalid key: {name}");
                return exit::usage();
            }
            Err(e) => {
                log::error!("{e}");
                return exit::FAILURE;
            }
        }
    }
    if deleted_from.is_empty() {
        log::error!("not set: {name}");
        return exit::FAILURE;
    }
    exit::SUCCESS
}

fn unset_in_single_file(name: &str, file: &Path) -> ExitCode {
    let mut single = match Configuration::load([file.to_path_buf()]) {
        Ok(c) => c,
        Err(e) => {
            log::error!("{e}");
            return exit::FAILURE;
        }
    };
    map_unset_result(name, single.delete(name, file))
}

fn map_unset_result(name: &str, result: Result<(), ConfigError>) -> ExitCode {
    match result {
        Ok(()) => exit::SUCCESS,
        Err(ConfigError::NotFound(_)) => {
            log::error!("not set: {name}");
            exit::FAILURE
        }
        Err(e) => {
            log::error!("{e}");
            exit::usage()
        }
    }
}
