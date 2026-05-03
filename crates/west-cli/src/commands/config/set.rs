use std::process::ExitCode;

use clap::{Args, ValueEnum};

use west_core::config::ConfigValue;

use super::{LoadedConfig, ScopeArgs, load, scope_to_path};

#[derive(Args, Debug)]
pub struct SetArgs {
    /// Configuration option name (e.g. `manifest.path`).
    pub name: String,

    /// Value(s) to set. Multiple values require `--list`.
    #[arg(required = true, allow_hyphen_values = true)]
    pub values: Vec<String>,

    /// Coerce the (single) value to a non-string TOML type.
    #[arg(
        long = "type",
        value_name = "TYPE",
        default_value = "string",
        conflicts_with = "list"
    )]
    pub r#type: ValueType,

    /// Treat the values as a list of strings (TOML array).
    #[arg(long, conflicts_with = "type")]
    pub list: bool,

    #[command(flatten)]
    pub scope: ScopeArgs,
}

#[derive(Clone, Debug, ValueEnum, PartialEq, Eq)]
pub enum ValueType {
    String,
    Bool,
    Int,
    Float,
}

pub fn run(args: SetArgs) -> ExitCode {
    // Build the value first so that argument errors don't touch disk.
    let value = match build_value(&args) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("west: {e}");
            return ExitCode::from(2);
        }
    };

    let extras: Vec<_> = args.scope.file.iter().cloned().collect();
    let LoadedConfig {
        resolved,
        mut config,
    } = match load(&extras) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("west: {e}");
            return ExitCode::FAILURE;
        }
    };

    // Resolve target. Default scope is --local.
    let target = match scope_to_path(&args.scope, &resolved) {
        Ok(Some(p)) => p,
        Ok(None) => match resolved.local.clone() {
            Some(p) => p,
            None => {
                eprintln!("west: --local: not in a workspace; use --file or run inside one");
                return ExitCode::from(3);
            }
        },
        Err(e) => {
            eprintln!("west: {e}");
            // --local without workspace gets the workspace-error code.
            let code = if e.contains("workspace") { 3 } else { 2 };
            return ExitCode::from(code);
        }
    };

    if let Err(e) = config.set(&args.name, value, &target) {
        eprintln!("west: {e}");
        return ExitCode::FAILURE;
    }
    ExitCode::SUCCESS
}

fn build_value(args: &SetArgs) -> Result<ConfigValue, String> {
    if args.list {
        return Ok(ConfigValue::list_of_strings(args.values.iter().cloned()));
    }
    if args.values.len() != 1 {
        return Err(format!(
            "expected exactly one value (got {}); use --list for multiple values",
            args.values.len()
        ));
    }
    let raw = &args.values[0];
    match args.r#type {
        ValueType::String => Ok(ConfigValue::String(raw.clone())),
        ValueType::Bool => parse_bool(raw)
            .map(ConfigValue::Bool)
            .ok_or_else(|| format!("invalid boolean: {raw:?}")),
        ValueType::Int => raw
            .parse::<i64>()
            .map(ConfigValue::Integer)
            .map_err(|e| format!("invalid integer {raw:?}: {e}")),
        ValueType::Float => raw
            .parse::<f64>()
            .map(ConfigValue::Float)
            .map_err(|e| format!("invalid float {raw:?}: {e}")),
    }
}

/// Same coercion set as `Configuration::get_bool` (Python configparser).
fn parse_bool(s: &str) -> Option<bool> {
    match s.to_ascii_lowercase().as_str() {
        "1" | "yes" | "true" | "on" => Some(true),
        "0" | "no" | "false" | "off" => Some(false),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(values: &[&str]) -> SetArgs {
        SetArgs {
            name: "k.v".into(),
            values: values.iter().map(|s| (*s).to_owned()).collect(),
            r#type: ValueType::String,
            list: false,
            scope: ScopeArgs::default(),
        }
    }

    #[test]
    fn build_value_string_default() {
        let a = args(&["hello"]);
        assert_eq!(build_value(&a).unwrap(), ConfigValue::String("hello".into()));
    }

    #[test]
    fn build_value_bool_accepts_python_set() {
        for s in ["1", "yes", "true", "on", "TRUE", "On"] {
            let mut a = args(&[s]);
            a.r#type = ValueType::Bool;
            assert_eq!(build_value(&a).unwrap(), ConfigValue::Bool(true), "{s}");
        }
        for s in ["0", "no", "false", "off"] {
            let mut a = args(&[s]);
            a.r#type = ValueType::Bool;
            assert_eq!(build_value(&a).unwrap(), ConfigValue::Bool(false), "{s}");
        }
    }

    #[test]
    fn build_value_bool_rejects_garbage() {
        let mut a = args(&["maybe"]);
        a.r#type = ValueType::Bool;
        let err = build_value(&a).unwrap_err();
        assert!(err.contains("invalid boolean"));
    }

    #[test]
    fn build_value_int_and_float() {
        let mut a = args(&["42"]);
        a.r#type = ValueType::Int;
        assert_eq!(build_value(&a).unwrap(), ConfigValue::Integer(42));

        let mut a = args(&["1.5"]);
        a.r#type = ValueType::Float;
        assert_eq!(build_value(&a).unwrap(), ConfigValue::Float(1.5));
    }

    #[test]
    fn build_value_list_accepts_many_values() {
        let mut a = args(&["a", "b", "c"]);
        a.list = true;
        assert_eq!(
            build_value(&a).unwrap(),
            ConfigValue::list_of_strings(["a", "b", "c"])
        );
    }

    #[test]
    fn build_value_scalar_rejects_multiple_values() {
        let a = args(&["a", "b"]);
        let err = build_value(&a).unwrap_err();
        assert!(err.contains("--list"));
    }
}
