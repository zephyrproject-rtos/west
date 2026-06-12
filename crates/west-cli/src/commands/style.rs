//! Stderr styling -- one source of truth for the colour palette every
//! command paints onto stderr.
//!
//! Pairs with [`super::color`]: that module resolves *what* colour
//! decision the user expressed (flag -> per-command config ->
//! `WEST_COLOR` env -> stream-local TTY); this module turns that
//! decision into a [`console::Style`] for a specific role (per-project
//! banner, log-level prefix, ...). Splitting input vs. output keeps
//! the two concerns inspectable in isolation.
//!
//! The `Auto` branch of every style consults
//! [`super::color::want_color_stderr`] rather than calling
//! `console::Style::for_stderr()` directly: that routes through the
//! cached `WEST_COLOR` decision so `color.ui = false` / `NO_COLOR=1`
//! are honoured here without re-deriving the policy. Explicit
//! `Always` / `Never` still force the choice via `force_styling`.

use console::Style;

use super::color::{ColorArg, want_color_stderr};

/// Per-project banner palette used by `diff`, `status`, `compare`,
/// `forall`, and `update` for the `=== name (path):` chrome line
/// they prefix output blocks with. Bright green + bold matches v1's
/// `colorama.Fore.LIGHTGREEN_EX + Style.BRIGHT`.
pub fn banner(color: ColorArg) -> Style {
    match color {
        ColorArg::Always => Style::new().green().bright().bold().force_styling(true),
        ColorArg::Never => Style::new().force_styling(false),
        ColorArg::Auto => Style::new()
            .green()
            .bright()
            .bold()
            .force_styling(want_color_stderr()),
    }
}

/// `west: error:` prefix style used by the env_logger format closure.
/// Red + bold; respects the cached `WEST_COLOR` policy via
/// [`want_color_stderr`].
pub fn error_prefix() -> Style {
    Style::new().red().bold().force_styling(want_color_stderr())
}

/// `west: warning:` prefix style used by the env_logger format closure.
/// Yellow + bold; same policy as [`error_prefix`].
pub fn warning_prefix() -> Style {
    Style::new()
        .yellow()
        .bold()
        .force_styling(want_color_stderr())
}

