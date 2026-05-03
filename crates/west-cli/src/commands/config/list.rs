use std::process::ExitCode;

use clap::Args;

use west_core::config::ConfigValue;

use super::{LoadedConfig, ScopeArgs, load, scope_to_path};

#[derive(Args, Debug)]
pub struct ListArgs {
    #[command(flatten)]
    pub scope: ScopeArgs,
}

pub fn run(args: ListArgs) -> ExitCode {
    let extras: Vec<_> = args.scope.file.iter().cloned().collect();
    let LoadedConfig { resolved, config } = match load(&extras) {
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

    let items = match scope_path {
        Some(p) => match config.items_in(&p) {
            Ok(items) => items,
            Err(e) => {
                eprintln!("west: {e}");
                return ExitCode::from(2);
            }
        },
        None => config.items(),
    };

    for (key, value) in items {
        emit(&key, &value);
    }
    ExitCode::SUCCESS
}

fn emit(key: &str, value: &ConfigValue) {
    match value {
        ConfigValue::List(elements) => {
            for el in elements {
                match el {
                    ConfigValue::List(_) => {
                        // Nested lists aren't representable in git-style flat
                        // output. Render as a TOML-ish fragment to surface
                        // them without crashing.
                        println!("{key}=<nested list>");
                    }
                    other => println!("{key}={other}"),
                }
            }
        }
        scalar => println!("{key}={scalar}"),
    }
}
