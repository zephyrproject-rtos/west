use std::path::Path;
use std::process::ExitCode;

use clap::Args;

use west_core::config::{ConfigValue, Configuration};

use super::{LoadedConfig, ScopeArgs, scope_to_path};
use crate::exit;

#[derive(Args, Debug)]
pub struct GetArgs {
    /// Configuration option name (e.g. `manifest.path`).
    pub name: String,
    #[command(flatten)]
    pub scope: ScopeArgs,
}

pub fn run(args: GetArgs, loaded: &mut LoadedConfig) -> ExitCode {
    // --file PATH: operate strictly on PATH; ignore the layered config and
    // any --config inline overrides.
    if let Some(file) = &args.scope.file {
        return get_from_single_file(&args.name, file);
    }

    let scope_path = match scope_to_path(&args.scope, &loaded.resolved) {
        Ok(p) => p,
        Err(e) => {
            log::error!("{e}");
            return exit::usage();
        }
    };

    let value = match scope_path {
        Some(p) => loaded.config.get_in(&args.name, &p),
        None => loaded.config.get(&args.name),
    };

    emit(value)
}

fn get_from_single_file(name: &str, file: &Path) -> ExitCode {
    let single = match Configuration::load([file.to_path_buf()]) {
        Ok(c) => c,
        Err(e) => {
            log::error!("{e}");
            return exit::FAILURE;
        }
    };
    emit(single.get_in(name, file))
}

fn emit(value: Result<Option<ConfigValue>, west_core::config::ConfigError>) -> ExitCode {
    match value {
        Ok(Some(ConfigValue::List(items))) => {
            for it in items {
                if let Ok(s) = format_scalar(&it) {
                    println!("{s}");
                }
            }
            exit::SUCCESS
        }
        Ok(Some(scalar)) => {
            println!("{scalar}");
            exit::SUCCESS
        }
        Ok(None) => exit::FAILURE,
        Err(e) => {
            log::error!("{e}");
            exit::usage()
        }
    }
}

fn format_scalar(v: &ConfigValue) -> Result<String, std::fmt::Error> {
    use std::fmt::Write;
    let mut buf = String::new();
    write!(&mut buf, "{v}")?;
    Ok(buf)
}
