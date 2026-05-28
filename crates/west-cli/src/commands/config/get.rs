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

    /// Fallback value to print (on stdout, exit 0) when NAME is not
    /// set, instead of the default behaviour (print nothing, exit
    /// 1). Mirrors `git config --get --default`. Doesn't suppress
    /// real errors — a malformed key still exits 2.
    #[arg(long, value_name = "VALUE")]
    pub default: Option<String>,

    #[command(flatten)]
    pub scope: ScopeArgs,
}

pub fn run(args: GetArgs, loaded: &mut LoadedConfig) -> ExitCode {
    // --file PATH: operate strictly on PATH; ignore the layered config and
    // any --config inline overrides.
    if let Some(file) = &args.scope.file {
        return get_from_single_file(&args.name, file, args.default.as_deref());
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

    emit(value, args.default.as_deref())
}

fn get_from_single_file(name: &str, file: &Path, default: Option<&str>) -> ExitCode {
    let single = match Configuration::load([file.to_path_buf()]) {
        Ok(c) => c,
        Err(e) => {
            log::error!("{e}");
            return exit::FAILURE;
        }
    };
    emit(single.get_in(name, file), default)
}

fn emit(
    value: Result<Option<ConfigValue>, west_core::config::ConfigError>,
    default: Option<&str>,
) -> ExitCode {
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
        // Key absent. `--default VALUE` upgrades this from FAILURE to
        // SUCCESS-with-VALUE-on-stdout, mirroring `git config --get
        // --default`. The default isn't parsed as a TOML expression
        // (unlike `set`'s value) — it's printed verbatim, matching the
        // way present-key scalars are printed.
        Ok(None) => match default {
            Some(d) => {
                println!("{d}");
                exit::SUCCESS
            }
            None => exit::FAILURE,
        },
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
