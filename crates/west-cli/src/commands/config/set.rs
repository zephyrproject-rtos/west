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

    /// Append `value` to a list-valued key instead of replacing it.
    /// List inputs extend the target list with all their elements;
    /// scalar inputs append a single element. Errors if the key is
    /// currently a scalar (string/int/bool/float) — only list and
    /// absent-key are accepted. Reads the existing value from the
    /// same layer it writes back to, so `--local` won't peek at
    /// `--global` (use `west config get` then `west config set` if
    /// you want merged semantics).
    #[arg(short = 'a', long)]
    pub append: bool,

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
        return set_in_single_file(&args.name, value, file, args.append);
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

    write_value(&args.name, value, &target, &mut loaded.config, args.append)
}

fn set_in_single_file(name: &str, value: ConfigValue, file: &Path, append: bool) -> ExitCode {
    let mut single = match Configuration::load([file.to_path_buf()]) {
        Ok(c) => c,
        Err(e) => {
            log::error!("{e}");
            return exit::FAILURE;
        }
    };
    write_value(name, value, file, &mut single, append)
}

/// Shared replace-or-append writer. Reads + writes the same layer
/// when `append` is true so per-scope semantics stay clean (no silent
/// cross-layer shadowing).
fn write_value(
    name: &str,
    value: ConfigValue,
    layer: &Path,
    config: &mut Configuration,
    append: bool,
) -> ExitCode {
    let final_value = if append {
        match build_appended_value(name, value, layer, config) {
            Ok(v) => v,
            Err(code) => return code,
        }
    } else {
        value
    };
    if let Err(e) = config.set(name, final_value, layer) {
        log::error!("{e}");
        return exit::FAILURE;
    }
    exit::SUCCESS
}

/// `-a / --append`: read the current value from `layer` alone, fold
/// `value` into it, and return the list to write back. Absent → new
/// list; list → extend (or append if `value` is a scalar); scalar
/// already there → reject (`-a` only operates on lists).
fn build_appended_value(
    name: &str,
    value: ConfigValue,
    layer: &Path,
    config: &Configuration,
) -> Result<ConfigValue, ExitCode> {
    let current = match config.get_in(name, layer) {
        Ok(c) => c,
        Err(e) => {
            log::error!("{e}");
            return Err(exit::FAILURE);
        }
    };
    let to_add = into_elements(value);
    let merged = match current {
        None => to_add,
        Some(ConfigValue::List(mut existing)) => {
            existing.extend(to_add);
            existing
        }
        Some(scalar) => {
            log::error!(
                "--append requires a list-valued key; {name:?} is currently a {}. \
                 Drop --append to replace it, or `west config unset {name}` first.",
                type_label(&scalar),
            );
            return Err(exit::usage());
        }
    };
    Ok(ConfigValue::List(merged))
}

/// Convert the user-supplied value into the elements that should join
/// the target list. A `List` extends (its items become elements one
/// by one); a scalar appends as a single element. The python idiom is
/// `list.extend([...])` vs `list.append(x)` — `--append` picks the
/// right call based on what the user typed.
fn into_elements(value: ConfigValue) -> Vec<ConfigValue> {
    match value {
        ConfigValue::List(items) => items,
        scalar => vec![scalar],
    }
}

fn type_label(v: &ConfigValue) -> &'static str {
    match v {
        ConfigValue::String(_) => "string",
        ConfigValue::Bool(_) => "boolean",
        ConfigValue::Integer(_) => "integer",
        ConfigValue::Float(_) => "float",
        ConfigValue::List(_) => "list",
    }
}
