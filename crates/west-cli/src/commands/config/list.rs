use std::path::Path;
use std::process::ExitCode;

use clap::Args;

use west_core::config::{ConfigValue, Configuration};

use crate::exit;
use super::{LoadedConfig, ScopeArgs, scope_to_path};

#[derive(Args, Debug)]
pub struct ListArgs {
    #[command(flatten)]
    pub scope: ScopeArgs,
}

pub fn run(args: ListArgs, loaded: &mut LoadedConfig) -> ExitCode {
    if let Some(file) = &args.scope.file {
        return list_single_file(file);
    }

    let scope_path = match scope_to_path(&args.scope, &loaded.resolved) {
        Ok(p) => p,
        Err(e) => {
            log::error!("{e}");
            return exit::usage();
        }
    };

    let items = match scope_path {
        Some(p) => match loaded.config.items_in(&p) {
            Ok(items) => items,
            Err(e) => {
                log::error!("{e}");
                return exit::usage();
            }
        },
        None => loaded.config.items(),
    };

    print_items(&items);
    ExitCode::SUCCESS
}

fn list_single_file(file: &Path) -> ExitCode {
    let single = match Configuration::load([file.to_path_buf()]) {
        Ok(c) => c,
        Err(e) => {
            log::error!("{e}");
            return ExitCode::FAILURE;
        }
    };
    let items = match single.items_in(file) {
        Ok(items) => items,
        Err(e) => {
            log::error!("{e}");
            return exit::usage();
        }
    };
    print_items(&items);
    ExitCode::SUCCESS
}

fn print_items(items: &[(String, ConfigValue)]) {
    for (key, value) in items {
        emit(key, value);
    }
}

fn emit(key: &str, value: &ConfigValue) {
    match value {
        ConfigValue::List(elements) => {
            for el in elements {
                match el {
                    ConfigValue::List(_) => {
                        // Nested lists aren't representable in git-style flat
                        // output. Render as a placeholder to surface them
                        // without crashing.
                        println!("{key}=<nested list>");
                    }
                    other => println!("{key}={other}"),
                }
            }
        }
        scalar => println!("{key}={scalar}"),
    }
}
