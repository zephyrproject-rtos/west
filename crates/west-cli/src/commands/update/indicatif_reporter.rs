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

use indicatif::{MultiProgress, ProgressBar, ProgressStyle};

use west_core::vcs::{CommitSummary, ProgressSink};

use super::error::UpdateError;
use super::output::{FailureSummary, Reporter};
use crate::progress::{
    IndicatifSink, PREFIX_WIDTH, TICK_INTERVAL, render_done_line, render_failed_line,
    spinner_style, truncate_prefix,
};

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
        // Share the process-wide MultiProgress so the logger (routed
        // through the same instance) can suspend these bars to print
        // log lines above them.
        let multi = crate::progress::multi().clone();

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
                let _ = self.multi.println(render_failed_line(&prefix, &msg));
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
        // The MultiProgress is the process-global singleton, so we
        // don't drop it; per-project bars were already cleared and the
        // summary bar is left finished (rendered) above.
        FailureSummary {
            failed: state.failed,
        }
    }
}

// `render_done_line` / `render_failed_line` / `truncate_subject` plus
// their unit tests moved to `crate::progress` (shared with
// `west init`). Same shape, same test coverage; nothing reporter-
// specific left here.
