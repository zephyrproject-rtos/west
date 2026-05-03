use std::process::ExitCode;

use clap::Args;

use super::{LoadedConfig, ScopeArgs, load, scope_to_path};

#[derive(Args, Debug)]
pub struct GetArgs {
    /// Configuration option name (e.g. `manifest.path`).
    pub name: String,
    #[command(flatten)]
    pub scope: ScopeArgs,
}

pub fn run(args: GetArgs) -> ExitCode {
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

    // Lookup either across all layers or in a single layer. We try the scalar
    // accessor first; if the value turns out to be a list, fall through to
    // the list accessor.
    let scalar = match &scope_path {
        Some(p) => match config.get_str_in(&args.name, p) {
            Ok(v) => v,
            Err(e) => {
                eprintln!("west: {e}");
                return ExitCode::from(2);
            }
        },
        None => config.get_str(&args.name),
    };

    if let Some(s) = scalar {
        println!("{s}");
        return ExitCode::SUCCESS;
    }

    // Maybe it's a list?
    let list_result = match &scope_path {
        Some(p) => config.get_list_str_in(&args.name, p),
        None => config.get_list_str(&args.name),
    };
    match list_result {
        Ok(Some(items)) => {
            for item in items {
                println!("{item}");
            }
            ExitCode::SUCCESS
        }
        Ok(None) => ExitCode::FAILURE,
        Err(e) => {
            eprintln!("west: {e}");
            ExitCode::from(2)
        }
    }
}

