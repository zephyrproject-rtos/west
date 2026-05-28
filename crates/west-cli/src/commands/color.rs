//! Shared `--color` argument + resolution helper.
//!
//! All "produce output" commands (`diff`, `status`, `compare`, `grep`)
//! expose a `--color {always|never|auto}` flag and need to resolve it
//! against the same fallback chain:
//!
//! 1. `--color` if the user passed one
//! 2. The per-command config key (`grep.color` for grep — `diff` /
//!    `status` / `compare` don't have one today, but the slot is left
//!    open via [`resolve`]'s `command_color_key` argument)
//! 3. `color.ui` (git-style global default)
//! 4. `auto` (TTY heuristic — handled at the call site by each command,
//!    because some check stdout and some check stderr)
//!
//! Before this module existed, `diff`/`status`/`compare` only consulted
//! step 1 and the `args.color: ColorArg` field defaulted to `Auto`, so
//! a user with `color.ui = never` set globally still got colour output
//! from those three. `grep` did the right thing in isolation but the
//! logic was inlined there. Hoisted so every command shares the chain.

use clap::ValueEnum;

use west_core::config::Configuration;

#[derive(ValueEnum, Clone, Copy, Debug, PartialEq, Eq)]
pub enum ColorArg {
    Always,
    Never,
    Auto,
}

impl ColorArg {
    pub fn as_str(self) -> &'static str {
        match self {
            ColorArg::Always => "always",
            ColorArg::Never => "never",
            ColorArg::Auto => "auto",
        }
    }
}

/// Walk the `--color` → `<command>.color` → `color.ui` → `auto` chain.
///
/// `arg` is `None` when the user did not pass `--color`; `Some(v)` means
/// an explicit choice — including `Some(ColorArg::Auto)`, which suppresses
/// config consultation and re-runs the auto-detection at the call site.
///
/// `command_color_key` lets the per-command override participate. Pass
/// `Some("grep.color")` for grep; pass `None` for commands that don't
/// have a dedicated key.
///
/// Returned `Auto` still requires per-call-site TTY resolution because
/// stdout and stderr can differ (e.g. diff results stream to stdout but
/// the per-project banner streams to stderr).
pub fn resolve(
    arg: Option<ColorArg>,
    config: &Configuration,
    command_color_key: Option<&str>,
) -> Result<ColorArg, String> {
    if let Some(c) = arg {
        return Ok(c);
    }
    if let Some(key) = command_color_key
        && let Some(s) = config.get_str(key).map_err(|e| e.to_string())?
    {
        return parse_strict(&s, key);
    }
    if let Some(s) = config.get_str("color.ui").map_err(|e| e.to_string())? {
        // `color.ui` accepts a broader vocabulary than `--color` (git
        // also takes `true` / `false`). Map the common variants;
        // anything unrecognised falls through to `auto` rather than
        // erroring, matching git's permissive read of this key.
        return Ok(match s.as_str() {
            "always" | "true" => ColorArg::Always,
            "never" | "false" => ColorArg::Never,
            _ => ColorArg::Auto,
        });
    }
    Ok(ColorArg::Auto)
}

/// Strict parser used for per-command config keys (e.g. `grep.color`).
/// Unknown values error rather than falling through, because these keys
/// are command-specific and a typo is more likely to be a real bug than
/// in the broader `color.ui`.
fn parse_strict(s: &str, key: &str) -> Result<ColorArg, String> {
    match s {
        "always" => Ok(ColorArg::Always),
        "never" => Ok(ColorArg::Never),
        "auto" => Ok(ColorArg::Auto),
        other => Err(format!(
            "{key}: unknown value {other:?} (expected always, never, or auto)"
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use west_core::config::ConfigValue;

    fn cfg_with(pairs: &[(&str, &str)]) -> Configuration {
        let mut c = Configuration::load(std::iter::empty::<std::path::PathBuf>()).unwrap();
        for (k, v) in pairs {
            c.set_inline(k, ConfigValue::String((*v).into())).unwrap();
        }
        c
    }

    #[test]
    fn arg_wins_over_config() {
        let c = cfg_with(&[("color.ui", "never"), ("grep.color", "never")]);
        assert_eq!(
            resolve(Some(ColorArg::Always), &c, Some("grep.color")).unwrap(),
            ColorArg::Always
        );
    }

    #[test]
    fn command_key_wins_over_color_ui() {
        let c = cfg_with(&[("color.ui", "always"), ("grep.color", "never")]);
        assert_eq!(
            resolve(None, &c, Some("grep.color")).unwrap(),
            ColorArg::Never
        );
    }

    #[test]
    fn color_ui_is_consulted_when_arg_and_command_key_unset() {
        let c = cfg_with(&[("color.ui", "never")]);
        assert_eq!(
            resolve(None, &c, Some("grep.color")).unwrap(),
            ColorArg::Never
        );
        // Same key, no per-command override at all — the bug fix for
        // diff/status/compare.
        assert_eq!(resolve(None, &c, None).unwrap(), ColorArg::Never);
    }

    #[test]
    fn color_ui_true_false_map_to_always_never() {
        let c1 = cfg_with(&[("color.ui", "true")]);
        assert_eq!(resolve(None, &c1, None).unwrap(), ColorArg::Always);
        let c2 = cfg_with(&[("color.ui", "false")]);
        assert_eq!(resolve(None, &c2, None).unwrap(), ColorArg::Never);
    }

    #[test]
    fn color_ui_unknown_falls_through_to_auto() {
        let c = cfg_with(&[("color.ui", "ansi-256")]);
        assert_eq!(resolve(None, &c, None).unwrap(), ColorArg::Auto);
    }

    #[test]
    fn command_key_unknown_value_errors() {
        let c = cfg_with(&[("grep.color", "rainbow")]);
        let err = resolve(None, &c, Some("grep.color")).unwrap_err();
        assert!(err.contains("grep.color"));
        assert!(err.contains("rainbow"));
    }

    #[test]
    fn nothing_set_defaults_to_auto() {
        let c = cfg_with(&[]);
        assert_eq!(resolve(None, &c, None).unwrap(), ColorArg::Auto);
        assert_eq!(
            resolve(None, &c, Some("grep.color")).unwrap(),
            ColorArg::Auto
        );
    }
}
