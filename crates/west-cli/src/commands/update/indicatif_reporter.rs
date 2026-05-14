//! `Reporter` implementation that drives a `MultiProgress` for parallel
//! `west update` runs against an interactive terminal. Each project gets
//! its own [`indicatif::ProgressBar`]; the bar's message tracks the
//! current phase, and the bar fills as `Tick` events arrive. Failed
//! projects' captured transcripts are dumped above the still-active
//! bars so the user can debug without losing context.
//!
//! Each project's completion is recorded as a permanent line via
//! [`MultiProgress::println`] and the live bar is then cleared. We do
//! not rely on a bar's *finished* state staying visible: with many bars
//! finishing in rapid succession the still-active set overdraws older
//! bars, and only the bars alive at the very end remain in scrollback.
//! The `println` route writes plain text that the terminal can't
//! reclaim.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use console::Style;
use indicatif::{MultiProgress, ProgressBar, ProgressStyle};

use west_core::vcs::{CommitSummary, ProgressSink};

use super::error::UpdateError;
use super::output::{FailureSummary, Reporter};
use crate::progress::{IndicatifSink, PREFIX_WIDTH, TICK_INTERVAL, spinner_style, truncate_prefix};

/// Max columns reserved for the commit subject on the success line.
/// Budget per row: prefix (32) + space + glyph (1) + space + sha column
/// (12) + space + subject (60) ≈ 108 cols, fits comfortably in
/// 110-column terminals. Only the subject is truncated; if the SHA
/// happens to exceed `SHA_WIDTH` we keep it intact and the row shifts.
const SUBJECT_WIDTH: usize = 60;

/// Visible width of the SHA column. Sized so that all short-SHA widths
/// observed in real Zephyr manifests (typically 7, sometimes growing
/// to ~11 on busy repos) line up under each other.
const SHA_WIDTH: usize = 12;

pub struct IndicatifReporter {
    multi: MultiProgress,
    state: Mutex<IndicatifState>,
}

#[derive(Default)]
struct IndicatifState {
    /// Per-project transcripts, kept for replay on failure.
    transcripts: HashMap<String, Arc<Mutex<Vec<u8>>>>,
    /// Per-project ProgressBar handles for project_finished cleanup.
    bars: HashMap<String, ProgressBar>,
    /// Failures collected for the summary.
    failed: Vec<(String, String)>,
    /// Counter for the bottom "Updated N/M" status line.
    completed: u64,
    summary_bar: Option<ProgressBar>,
}

impl IndicatifReporter {
    pub fn new(total_projects: usize) -> Self {
        let multi = MultiProgress::new();

        // Bottom summary line as a styled bar (text-only).
        let summary = multi.add(ProgressBar::new(total_projects as u64));
        summary.set_style(
            ProgressStyle::with_template("Updated {pos}/{len} projects").expect("static template"),
        );
        summary.tick();

        Self {
            multi,
            state: Mutex::new(IndicatifState {
                summary_bar: Some(summary),
                ..IndicatifState::default()
            }),
        }
    }
}

impl Reporter for IndicatifReporter {
    fn sink_for_project<'a>(&'a self, project_name: &str) -> Box<dyn ProgressSink + Send + 'a> {
        let bar = self.multi.add(ProgressBar::new_spinner());
        bar.set_prefix(truncate_prefix(project_name, PREFIX_WIDTH));
        bar.set_style(spinner_style());
        bar.set_message("waiting…");
        bar.enable_steady_tick(TICK_INTERVAL);

        let transcript: Arc<Mutex<Vec<u8>>> = Arc::new(Mutex::new(Vec::new()));
        {
            let mut state = self.state.lock().unwrap_or_else(|p| p.into_inner());
            state
                .transcripts
                .insert(project_name.to_owned(), Arc::clone(&transcript));
            state.bars.insert(project_name.to_owned(), bar.clone());
        }

        Box::new(IndicatifSink::new(bar, Some(transcript)))
    }

    fn project_finished(&self, project_name: &str, outcome: Result<CommitSummary, UpdateError>) {
        let mut state = self.state.lock().unwrap_or_else(|p| p.into_inner());
        let bar = state.bars.remove(project_name);
        let prefix = truncate_prefix(project_name, PREFIX_WIDTH);

        match outcome {
            Err(e) => {
                let msg = e.to_string();
                // Replay the transcript above the still-active bars so the
                // user has the failing project's context.
                if let Some(buf) = state.transcripts.get(project_name)
                    && let Ok(g) = buf.lock()
                {
                    let _ = self.multi.println(format!(
                        "--- {project_name} (failed) ---\n{}",
                        String::from_utf8_lossy(&g).trim_end()
                    ));
                }
                let _ = self.multi.println(format!(
                    "{prefix:<width$} {label} {msg}",
                    prefix = prefix,
                    width = PREFIX_WIDTH,
                    label = Style::new().red().bold().apply_to("✗"),
                ));
                if let Some(b) = bar {
                    b.finish_and_clear();
                }
                state.failed.push((project_name.to_owned(), msg));
            }
            Ok(summary) => {
                let _ = self.multi.println(render_done_line(&prefix, &summary));
                if let Some(b) = bar {
                    b.finish_and_clear();
                }
            }
        }

        state.completed += 1;
        if let Some(s) = &state.summary_bar {
            s.set_position(state.completed);
        }
    }

    fn finish(self: Box<Self>) -> FailureSummary {
        let state = self.state.into_inner().unwrap_or_else(|p| p.into_inner());
        if let Some(s) = &state.summary_bar {
            s.finish();
        }
        // Drop the MultiProgress; remaining bars (if any) are flushed.
        FailureSummary {
            failed: state.failed,
        }
    }
}

/// Build the success line for a finished project: green `✓`, yellow
/// fixed-width SHA column, dimmed subject. Splitting this out makes the
/// rendering pure (no MultiProgress, no terminal) so the line layout
/// can be unit-tested.
fn render_done_line(prefix: &str, summary: &CommitSummary) -> String {
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

/// Truncate `s` to at most `max` columns, appending `…` if it had to be
/// cut. ASCII-aware (commit subjects are typically ASCII; non-ASCII falls
/// back to a byte-safe slice via `char_indices`).
fn truncate_subject(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        return s.to_owned();
    }
    let cut = max.saturating_sub(1);
    let end = s.char_indices().nth(cut).map(|(i, _)| i).unwrap_or(s.len());
    let mut out = s[..end].to_owned();
    out.push('…');
    out
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
