//! Shared indicatif glue. Both `west init` (single op) and
//! `west update` (per-project bar inside a `MultiProgress`) use the
//! same [`IndicatifSink`] to translate `ProgressEvent`s into bar
//! updates; the reporter wrapping is the per-command concern.

use std::io::Write;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use console::Style;
use indicatif::{ProgressBar, ProgressStyle};

use west_core::vcs::{CommitSummary, ProgressEvent, ProgressSink};

/// How often a bar pulses while waiting for the first Tick (so the
/// spinner moves visibly even on slow networks).
pub const TICK_INTERVAL: Duration = Duration::from_millis(120);

/// Width of the project-name prefix slot — chosen so common
/// zephyrproject project names fit and bars line up vertically.
pub const PREFIX_WIDTH: usize = 32;

/// Build the "phase + percentage bar" style. Used once a Phase event
/// with a known total has arrived.
pub fn bar_style() -> ProgressStyle {
    ProgressStyle::with_template(
        "{prefix:32!.cyan.bold} {spinner} {msg:24} [{bar:30.green/blue}] {pos}/{len}",
    )
    .expect("static template")
    .progress_chars("=> ")
}

/// Build the "phase + spinner" style. Used while we don't yet have a
/// known total (e.g. during the initial banner phase).
pub fn spinner_style() -> ProgressStyle {
    ProgressStyle::with_template("{prefix:32!.cyan.bold} {spinner} {msg}").expect("static template")
}

/// Truncate (with `…`) and right-pad `s` to `max` columns.
pub fn truncate_prefix(s: &str, max: usize) -> String {
    if s.len() <= max {
        format!("{s:<max$}", max = max)
    } else {
        let cut = max.saturating_sub(1);
        let mut t = s[..cut].to_owned();
        t.push('…');
        t
    }
}

/// Max columns reserved for the commit subject on success lines.
/// Budget per row: prefix (32) + space + glyph (1) + space + sha column
/// (12) + space + subject (60) ≈ 108 cols, fits comfortably in
/// 110-column terminals. Only the subject is truncated; if the SHA
/// happens to exceed `SHA_WIDTH` we keep it intact and the row shifts.
pub const SUBJECT_WIDTH: usize = 60;

/// Visible width of the SHA column on success lines. Sized so that all
/// short-SHA widths observed in real Zephyr manifests (typically 7,
/// sometimes growing to ~11 on busy repos) line up under each other.
pub const SHA_WIDTH: usize = 12;

/// Build the success line for a finished project / clone: green `✓`,
/// yellow fixed-width SHA column, dimmed subject. Pure function so
/// the line layout can be unit-tested. Both `west update`'s
/// per-project reporter and `west init`'s single-bar flow use this.
pub fn render_done_line(prefix: &str, summary: &CommitSummary) -> String {
    let subject = truncate_subject(&summary.subject, SUBJECT_WIDTH);
    let padded_sha = format!("{sha:<width$}", sha = &summary.short_sha, width = SHA_WIDTH);
    format!(
        "{prefix:<prefix_width$} {label} {sha} {subject}",
        prefix = prefix,
        prefix_width = PREFIX_WIDTH,
        label = Style::new().green().bold().apply_to("✓"),
        sha = Style::new().yellow().apply_to(padded_sha),
        subject = Style::new().dim().apply_to(subject),
    )
}

/// Build the failure line: red `✗` + a free-form message. Pairs with
/// [`render_done_line`] so success and failure rows have the same
/// prefix column and glyph slot.
pub fn render_failed_line(prefix: &str, msg: &str) -> String {
    format!(
        "{prefix:<prefix_width$} {label} {msg}",
        prefix = prefix,
        prefix_width = PREFIX_WIDTH,
        label = Style::new().red().bold().apply_to("✗"),
    )
}

/// Truncate `s` to at most `max` columns, appending `…` if it had to be
/// cut. ASCII-aware (commit subjects are typically ASCII; non-ASCII falls
/// back to a byte-safe slice via `char_indices`).
pub fn truncate_subject(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        return s.to_owned();
    }
    let cut = max.saturating_sub(1);
    let end = s.char_indices().nth(cut).map(|(i, _)| i).unwrap_or(s.len());
    let mut out = s[..end].to_owned();
    out.push('…');
    out
}

/// `ProgressSink` that drives a single `indicatif::ProgressBar`.
///
/// On `Phase` it switches the bar to the percentage style and updates
/// the message; on `Tick` it advances the position. `Line` events go
/// into an optional captured transcript (used by callers that want to
/// replay output on failure — `west update`'s IndicatifReporter does
/// this; `west init` doesn't bother).
///
/// The sink itself does not finalise the bar; the caller decides
/// whether to `finish_and_clear`, `finish_with_message`, or restyle on
/// failure.
pub struct IndicatifSink {
    bar: ProgressBar,
    bar_style: ProgressStyle,
    spinner_style: ProgressStyle,
    transcript: Option<Arc<Mutex<Vec<u8>>>>,
    current_phase: Option<String>,
    /// Whether we've switched the bar to the percentage-bar style.
    uses_bar_style: bool,
}

impl IndicatifSink {
    pub fn new(bar: ProgressBar, transcript: Option<Arc<Mutex<Vec<u8>>>>) -> Self {
        Self {
            bar,
            bar_style: bar_style(),
            spinner_style: spinner_style(),
            transcript,
            current_phase: None,
            uses_bar_style: false,
        }
    }
}

impl ProgressSink for IndicatifSink {
    fn event(&mut self, event: ProgressEvent<'_>) {
        match event {
            ProgressEvent::Line(s) => {
                if let Some(buf) = &self.transcript
                    && let Ok(mut g) = buf.lock()
                {
                    let _ = writeln!(g, "{s}");
                }
            }
            ProgressEvent::Phase { name, total } => {
                // The parser emits a fresh Phase before every Tick (it's
                // stateless), so most Phase events are redundant repeats
                // of the same phase. Only reset position when the name
                // actually changes; otherwise the bar visibly flashes
                // back to 0% between ticks.
                let phase_changed = self.current_phase.as_deref() != Some(name);
                if phase_changed {
                    self.current_phase = Some(name.to_owned());
                    self.bar.set_message(name.to_owned());
                }
                if let Some(t) = total {
                    if !self.uses_bar_style {
                        self.bar.set_style(self.bar_style.clone());
                        self.uses_bar_style = true;
                    }
                    self.bar.set_length(t);
                    if phase_changed {
                        self.bar.set_position(0);
                    }
                } else if self.uses_bar_style {
                    self.bar.set_style(self.spinner_style.clone());
                    self.uses_bar_style = false;
                }
            }
            ProgressEvent::Tick { done, total } => {
                if let Some(t) = total {
                    if !self.uses_bar_style {
                        self.bar.set_style(self.bar_style.clone());
                        self.uses_bar_style = true;
                    }
                    self.bar.set_length(t);
                }
                self.bar.set_position(done);
            }
            ProgressEvent::Finished => {
                // The caller decides finalisation (clear vs success-msg
                // vs error-restyle).
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use console::strip_ansi_codes;

    #[test]
    fn done_line_contains_glyph_sha_and_subject() {
        let prefix = truncate_prefix("zephyr", PREFIX_WIDTH);
        let line = render_done_line(
            &prefix,
            &CommitSummary {
                short_sha: "9164bd1".into(),
                subject: "tests: Format platform in multiline".into(),
            },
        );
        let plain = strip_ansi_codes(&line);
        assert!(plain.starts_with("zephyr"), "got: {plain:?}");
        assert!(plain.contains('✓'), "got: {plain:?}");
        assert!(plain.contains("9164bd1"), "got: {plain:?}");
        assert!(
            plain.contains("tests: Format platform in multiline"),
            "got: {plain:?}"
        );
    }

    #[test]
    fn done_line_pads_sha_to_fixed_column() {
        let prefix = truncate_prefix("zephyr", PREFIX_WIDTH);
        let line = render_done_line(
            &prefix,
            &CommitSummary {
                short_sha: "abc1234".into(), // 7 chars, shorter than SHA_WIDTH
                subject: "subject line".into(),
            },
        );
        let plain = strip_ansi_codes(&line);
        let sha_at = plain.find("abc1234").expect("sha in line");
        let sub_at = plain.find("subject line").expect("subject in line");
        // Layout: ... SHA<pad-to-SHA_WIDTH> SPACE subject
        let expected_gap = SHA_WIDTH - "abc1234".len() + 1;
        assert_eq!(
            sub_at - sha_at - "abc1234".len(),
            expected_gap,
            "subject not aligned to SHA column; line: {plain:?}"
        );
    }

    #[test]
    fn failed_line_contains_glyph_and_message() {
        let prefix = truncate_prefix("zephyr", PREFIX_WIDTH);
        let line = render_failed_line(&prefix, "fetch: remote unreachable");
        let plain = strip_ansi_codes(&line);
        assert!(plain.starts_with("zephyr"), "got: {plain:?}");
        assert!(plain.contains('✗'), "got: {plain:?}");
        assert!(
            plain.contains("fetch: remote unreachable"),
            "got: {plain:?}"
        );
    }

    #[test]
    fn truncate_subject_short_passthrough() {
        assert_eq!(truncate_subject("short", 60), "short");
    }

    #[test]
    fn truncate_subject_long_gets_ellipsis() {
        let long = "a".repeat(80);
        let out = truncate_subject(&long, 60);
        assert_eq!(out.chars().count(), 60);
        assert!(out.ends_with('…'));
    }
}
