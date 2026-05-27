use std::path::Path;
use std::process::ExitCode;

use clap::Args;

use west_core::config::{ConfigValue, Configuration};

use super::{LoadedConfig, ScopeArgs, scope_to_path};

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
            return ExitCode::from(2);
        }
    };

    // --file PATH: operate strictly on PATH; ignore the layered config and
    // any --config inline overrides.
    if let Some(file) = &args.scope.file {
        return set_in_single_file(&args.name, value, file);
    }

    let target = match scope_to_path(&args.scope, &loaded.resolved) {
        Ok(Some(p)) => p,
        Ok(None) => match loaded.resolved.local.clone() {
            Some(p) => p,
            None => {
                log::error!("--local: not in a workspace; use --file or run inside one");
                return ExitCode::from(3);
            }
        },
        Err(e) => {
            log::error!("{e}");
            let code = if e.contains("workspace") { 3 } else { 2 };
            return ExitCode::from(code);
        }
    };

    if let Err(e) = loaded.config.set(&args.name, value, &target) {
        log::error!("{e}");
        return ExitCode::FAILURE;
    }
    ExitCode::SUCCESS
}

fn set_in_single_file(name: &str, value: ConfigValue, file: &Path) -> ExitCode {
    let mut single = match Configuration::load([file.to_path_buf()]) {
        Ok(c) => c,
        Err(e) => {
            log::error!("{e}");
            return ExitCode::FAILURE;
        }
    };
    if let Err(e) = single.set(name, value, file) {
        log::error!("{e}");
        return ExitCode::FAILURE;
    }
    ExitCode::SUCCESS
}
