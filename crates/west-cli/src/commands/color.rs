//! Color policy: one decision, one process-wide channel, every emitter
//! agrees.
//!
//! [`resolve_universal`] runs once at startup in [`crate::run`] against
//! the workspace `Configuration` + `NO_COLOR` / `CLICOLOR` env vars and
//! caches the resolved policy in the `WEST_COLOR` environment variable
//! (values: `"always"` / `"never"` / `"auto"`). Every downstream
//! emitter — the rust env_logger format closure, the per-project
//! banner palette in [`super::style`], extension subprocesses spawned
//! by the rust binary, and the python wrappers in `src/west/` — reads
//! that one variable rather than re-deriving the policy from
//! `color.ui` / `NO_COLOR` / TTY independently. The python side
//! inherits `WEST_COLOR` for free because env vars propagate to child
//! processes, so `python -m west._dispatch ...` (and any
//! `WestCommand.err()` it ultimately calls) sees the same decision
//! the rust binary just made.
//!
//! Per-command `--color {always,never,auto}` flags on `diff` /
//! `status` / `compare` / `grep` / `forall` / `update` still override
//! the universal policy for that single invocation, via [`resolve`].
//! Per-command persistent config keys (`grep.color` etc.) sit between
//! the flag and the universal policy. The chain at a per-command site
//! is therefore:
//!
//! 1. `--color` CLI flag.
//! 2. `<command>.color` config key.
//! 3. `WEST_COLOR` env var (already encodes `color.ui` + `NO_COLOR` +
//!    `CLICOLOR`).
//! 4. `auto` -> the call site checks its own stream's TTY-ness via
//!    [`want_color_stdout`] / [`want_color_stderr`] (different
//!    commands target different streams).

use std::io::IsTerminal;

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

/// The env-var name that carries the resolved color policy across the
/// rust binary, its spawned subprocesses (extensions via
/// `west._dispatch`), and any python code those subprocesses run.
/// Values are the same `"always"` / `"never"` / `"auto"` strings the
/// `--color` flag accepts.
pub const WEST_COLOR: &str = "WEST_COLOR";

/// Resolve the universal color policy from config + env. Run once in
/// [`crate::run`] right after the layered `Configuration` is built;
/// the result is cached in [`WEST_COLOR`] so every other emitter
/// (rust logger, banner palette, python wrappers) reads one already-
/// resolved decision.
pub fn resolve_universal(config: &Configuration) -> &'static str {
    resolve_universal_from(
        config,
        std::env::var_os("NO_COLOR").is_some(),
        std::env::var("CLICOLOR").as_deref() == Ok("0"),
    )
}

/// Pure variant of [`resolve_universal`] for testing -- env-var reads
/// hoisted out so tests can exercise the policy chain without
/// `unsafe` env mutation under parallel `cargo test`.
fn resolve_universal_from(
    config: &Configuration,
    no_color: bool,
    clicolor_zero: bool,
) -> &'static str {
    // `NO_COLOR` (https://no-color.org) and `CLICOLOR=0` are
    // unconditional environment-level opt-outs; honoured before any
    // workspace config so a user setting them in their shell doesn't
    // need to also un-set `color.ui`.
    if no_color || clicolor_zero {
        return "never";
    }
    // `color.ui` accepts both string and bool forms because
    // `--config color.ui=true` parses VALUE as TOML and stores a
    // bool, whereas `color.ui = "always"` in a config file stores a
    // string. Try bool first (cheaper), then string. Unknown string
    // values fall through to `auto`, matching git's permissive read.
    if let Ok(Some(b)) = config.get_bool("color.ui") {
        return if b { "always" } else { "never" };
    }
    if let Ok(Some(s)) = config.get_str("color.ui") {
        match s.as_str() {
            "always" | "true" => return "always",
            "never" | "false" => return "never",
            _ => {}
        }
    }
    "auto"
}

/// Walk the per-command override chain: `--color` flag ->
/// `<command>.color` config -> [`WEST_COLOR`] env. Returns
/// [`ColorArg::Auto`] when the chain bottoms out without a strong
/// opinion; the call site picks `Always` / `Never` via
/// [`want_color_stdout`] / [`want_color_stderr`] depending on which
/// stream it's painting.
///
/// `command_color_key` lets the per-command persistent override
/// participate. Pass `Some("grep.color")` for grep; pass `None` for
/// commands without a dedicated key.
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
    Ok(from_west_color_env())
}

/// Read [`WEST_COLOR`] and map it to a [`ColorArg`]. Used by
/// [`resolve`]'s tail. Unset / unknown -> `Auto` (the resolver hasn't
/// been run yet, or rust is invoked without going through `lib.rs::run`,
/// e.g. unit tests).
fn from_west_color_env() -> ColorArg {
    from_west_color(std::env::var(WEST_COLOR).ok().as_deref())
}

fn from_west_color(v: Option<&str>) -> ColorArg {
    match v {
        Some("always") => ColorArg::Always,
        Some("never") => ColorArg::Never,
        _ => ColorArg::Auto,
    }
}

/// Should we emit ANSI on stderr right now? Reads the universal
/// [`WEST_COLOR`] decision; falls back to a stderr TTY check on
/// `auto` / unset. Use this anywhere [`ColorArg::Auto`] needs to
/// collapse to a yes/no for a stderr write.
pub fn want_color_stderr() -> bool {
    want_color_for_stream(
        std::env::var(WEST_COLOR).ok().as_deref(),
        std::io::stderr().is_terminal(),
    )
}

/// Should we emit ANSI on stdout right now? Stdout-targeted command
/// bodies (diff hunks, status porcelain) consult this rather than
/// [`want_color_stderr`] because the two streams can have different
/// TTY status under `west cmd 2>err`-style redirects.
pub fn want_color_stdout() -> bool {
    want_color_for_stream(
        std::env::var(WEST_COLOR).ok().as_deref(),
        std::io::stdout().is_terminal(),
    )
}

fn want_color_for_stream(west_color: Option<&str>, stream_is_tty: bool) -> bool {
    match west_color {
        Some("always") => true,
        Some("never") => false,
        _ => stream_is_tty,
    }
}

/// Strict parser used for per-command config keys (e.g. `grep.color`).
/// Unknown values error rather than falling through, because these
/// keys are command-specific and a typo is more likely to be a real
/// bug than in the broader `color.ui`.
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

    // ---- resolve_universal_from: workspace + env policy ----

    #[test]
    fn no_color_env_forces_never() {
        let c = cfg_with(&[("color.ui", "always")]);
        assert_eq!(resolve_universal_from(&c, true, false), "never");
    }

    #[test]
    fn clicolor_zero_forces_never() {
        let c = cfg_with(&[("color.ui", "always")]);
        assert_eq!(resolve_universal_from(&c, false, true), "never");
    }

    #[test]
    fn color_ui_true_false_map_to_always_never() {
        let c1 = cfg_with(&[("color.ui", "true")]);
        assert_eq!(resolve_universal_from(&c1, false, false), "always");
        let c2 = cfg_with(&[("color.ui", "false")]);
        assert_eq!(resolve_universal_from(&c2, false, false), "never");
    }

    #[test]
    fn color_ui_always_never() {
        let c1 = cfg_with(&[("color.ui", "always")]);
        assert_eq!(resolve_universal_from(&c1, false, false), "always");
        let c2 = cfg_with(&[("color.ui", "never")]);
        assert_eq!(resolve_universal_from(&c2, false, false), "never");
    }

    #[test]
    fn color_ui_unknown_value_falls_through_to_auto() {
        let c = cfg_with(&[("color.ui", "ansi-256")]);
        assert_eq!(resolve_universal_from(&c, false, false), "auto");
    }

    #[test]
    fn color_ui_bool_form_resolves() {
        // `--config color.ui=true` parses VALUE as a TOML bool, not a
        // string. Make sure that path is honoured too.
        let mut c1 = Configuration::load(std::iter::empty::<std::path::PathBuf>()).unwrap();
        c1.set_inline("color.ui", ConfigValue::Bool(true)).unwrap();
        assert_eq!(resolve_universal_from(&c1, false, false), "always");
        let mut c2 = Configuration::load(std::iter::empty::<std::path::PathBuf>()).unwrap();
        c2.set_inline("color.ui", ConfigValue::Bool(false)).unwrap();
        assert_eq!(resolve_universal_from(&c2, false, false), "never");
    }

    #[test]
    fn nothing_set_defaults_to_auto() {
        let c = cfg_with(&[]);
        assert_eq!(resolve_universal_from(&c, false, false), "auto");
    }

    // ---- from_west_color: env-var -> ColorArg mapping ----

    #[test]
    fn from_west_color_maps_values() {
        assert_eq!(from_west_color(Some("always")), ColorArg::Always);
        assert_eq!(from_west_color(Some("never")), ColorArg::Never);
        assert_eq!(from_west_color(Some("auto")), ColorArg::Auto);
        assert_eq!(from_west_color(None), ColorArg::Auto);
        assert_eq!(from_west_color(Some("garbage")), ColorArg::Auto);
    }

    // ---- want_color_for_stream: Auto-tail resolution ----

    #[test]
    fn want_color_for_stream_honours_explicit_decision() {
        // WEST_COLOR=always wins regardless of stream TTY-ness.
        assert!(want_color_for_stream(Some("always"), false));
        assert!(want_color_for_stream(Some("always"), true));
        // WEST_COLOR=never wins regardless of stream TTY-ness.
        assert!(!want_color_for_stream(Some("never"), false));
        assert!(!want_color_for_stream(Some("never"), true));
    }

    #[test]
    fn want_color_for_stream_falls_back_to_tty_on_auto_or_unset() {
        // `auto` / unset / unknown -> per-stream TTY check.
        for v in [Some("auto"), None, Some("garbage")] {
            assert!(want_color_for_stream(v, true));
            assert!(!want_color_for_stream(v, false));
        }
    }

    // ---- resolve: per-command chain ----
    //
    // These tests exercise the per-command chain in isolation. The
    // `WEST_COLOR` env-var tail is exercised via `from_west_color`
    // above; here we focus on the override layers that come first
    // (CLI flag, command_color_key).

    #[test]
    fn arg_wins_over_command_key() {
        let c = cfg_with(&[("grep.color", "never")]);
        assert_eq!(
            resolve(Some(ColorArg::Always), &c, Some("grep.color")).unwrap(),
            ColorArg::Always
        );
    }

    #[test]
    fn command_key_overrides_west_color_tail() {
        // No `--color` flag, but `<command>.color` is set: that wins
        // before the tail consults `WEST_COLOR`. The tail's result
        // doesn't matter for this test because the command-key
        // branch returns first.
        let c = cfg_with(&[("grep.color", "never")]);
        assert_eq!(
            resolve(None, &c, Some("grep.color")).unwrap(),
            ColorArg::Never
        );
    }

    #[test]
    fn command_key_unknown_value_errors() {
        let c = cfg_with(&[("grep.color", "rainbow")]);
        let err = resolve(None, &c, Some("grep.color")).unwrap_err();
        assert!(err.contains("grep.color"));
        assert!(err.contains("rainbow"));
    }
}
