//! Stderr styling — one source of truth for the colour palette every
//! command paints onto stderr.
//!
//! Pairs with [`super::color`]: that module resolves *what* colour
//! decision the user expressed (flag → config → auto); this module
//! turns that decision into a [`console::Style`] for a specific role
//! (per-project banner, log-level prefix, …). Splitting input vs.
//! output keeps the two concerns inspectable in isolation.
//!
//! All styles go through `Style::for_stderr()` (or
//! `force_styling(true|false)` for the explicit choices) so
//! `console`'s TTY detection / `NO_COLOR` / `TERM=dumb` heuristics
//! apply uniformly without per-call-site duplication.

use console::Style;

use super::color::ColorArg;

/// Per-project banner palette used by `diff`, `status`, `compare`,
/// `forall`, and `update` for the `=== name (path):` chrome line
/// they prefix output blocks with. Bright green + bold matches v1's
/// `colorama.Fore.LIGHTGREEN_EX + Style.BRIGHT`.
///
/// - `Always` forces colour even when stderr isn't a TTY (useful for
///   piping into `less -R` or capturing for review).
/// - `Never` strips all styling.
/// - `Auto` follows stderr's TTY-ness via `Style::for_stderr()`.
pub fn banner(color: ColorArg) -> Style {
    match color {
        ColorArg::Always => Style::new().green().bright().bold().force_styling(true),
        ColorArg::Never => Style::new().force_styling(false),
        ColorArg::Auto => Style::new().green().bright().bold().for_stderr(),
    }
}

/// `west: error:` prefix style used by the env_logger format
/// closure. Red + bold, auto-stripped when stderr isn't a TTY.
pub fn error_prefix() -> Style {
    Style::new().red().bold().for_stderr()
}

/// `west: warning:` prefix style used by the env_logger format
/// closure. Yellow + bold, auto-stripped when stderr isn't a TTY.
pub fn warning_prefix() -> Style {
    Style::new().yellow().bold().for_stderr()
}
