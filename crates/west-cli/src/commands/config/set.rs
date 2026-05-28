use std::path::Path;
use std::process::ExitCode;

use clap::Args;

use west_core::config::{ConfigValue, Configuration};

use super::{LoadedConfig, ScopeArgs, scope_to_path};
use crate::exit;

#[derive(Args, Debug)]
pub struct SetArgs {
    /// Configuration option name (e.g. `manifest.path`).
    pub name: String,

    /// TOML expression. Bare strings (without TOML constructs) may omit
    /// quotes; values that look like a TOML construct must parse cleanly.
    /// Examples: `42`, `true`, `"42"`, `'["a","b"]'`, `zephyr`.
    #[arg(allow_hyphen_values = true)]
    pub value: String,

    #[command(flatten)]
    pub scope: ScopeArgs,
}

pub fn run(args: SetArgs, loaded: &mut LoadedConfig) -> ExitCode {
    let value = match ConfigValue::parse(&args.value) {
        Ok(v) => v,
        Err(e) => {
            log::error!("{e}");
            return exit::usage();
        }
    };

    // --file PATH: operate strictly on PATH; ignore the layered config and
    // any --config inline overrides.
    if let Some(file) = &args.scope.file {
        return set_in_single_file(&args.name, value, file);
    }

    // "Not in a workspace" routes through FAILURE (matches topdir / list /
    // diff / status / compare / forall / grep / update — every other
    // command that surfaces the same condition). "Scope arg invalid"
    // routes through USAGE (matches every other command's prelude on bad
    // flag values). The string-contains heuristic is a smell that wants
    // `scope_to_path` to return a typed error; leaving as a TODO until we
    // touch that helper.
    let target = match scope_to_path(&args.scope, &loaded.resolved) {
        Ok(Some(p)) => p,
        Ok(None) => match loaded.resolved.local.clone() {
            Some(p) => p,
            None => {
                log::error!("--local: not in a workspace; use --file or run inside one");
                return exit::FAILURE;
            }
        },
        Err(e) => {
            log::error!("{e}");
            return if e.contains("workspace") {
                exit::FAILURE
            } else {
                exit::usage()
            };
        }
    };

    if let Err(e) = loaded.config.set(&args.name, value, &target) {
        log::error!("{e}");
        return exit::FAILURE;
    }
    exit::SUCCESS
}

fn set_in_single_file(name: &str, value: ConfigValue, file: &Path) -> ExitCode {
    let mut single = match Configuration::load([file.to_path_buf()]) {
        Ok(c) => c,
        Err(e) => {
            log::error!("{e}");
            return exit::FAILURE;
        }
    };
    if let Err(e) = single.set(name, value, file) {
        log::error!("{e}");
        return exit::FAILURE;
    }
    exit::SUCCESS
}
