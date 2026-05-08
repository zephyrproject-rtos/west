//! `Reporter` implementation that drives a `MultiProgress` for parallel
//! `west update` runs against an interactive terminal. Each project gets
//! its own [`indicatif::ProgressBar`]; the bar's message tracks the
//! current phase, and the bar fills as `Tick` events arrive. Failed
//! projects' captured transcripts are dumped above the still-active
//! bars so the user can debug without losing context.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use indicatif::{MultiProgress, ProgressBar, ProgressStyle};

use west_core::vcs::ProgressSink;

use super::output::{FailureSummary, Reporter};
use crate::progress::{IndicatifSink, PREFIX_WIDTH, TICK_INTERVAL, spinner_style, truncate_prefix};

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
            let mut state = self.state.lock().expect("indicatif state mutex poisoned");
            state
                .transcripts
                .insert(project_name.to_owned(), Arc::clone(&transcript));
            state.bars.insert(project_name.to_owned(), bar.clone());
        }

        Box::new(IndicatifSink::new(bar, Some(transcript)))
    }

    fn project_finished(&self, project_name: &str, outcome: Result<(), String>) {
        let mut state = self.state.lock().expect("indicatif state mutex poisoned");
        let bar = state.bars.remove(project_name);

        if let Err(e) = &outcome {
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
            let _ = self.multi.println(format!("{project_name}: ERROR: {e}"));
            if let Some(b) = &bar {
                b.set_style(
                    ProgressStyle::with_template("{prefix:32!.red.bold} {msg}")
                        .expect("static template"),
                );
                b.finish_with_message("failed");
            }
            state.failed.push((project_name.to_owned(), e.clone()));
        } else if let Some(b) = &bar {
            b.set_style(
                ProgressStyle::with_template("{prefix:32!.green.bold} {msg}")
                    .expect("static template"),
            );
            b.finish_with_message("done");
        }

        state.completed += 1;
        if let Some(s) = &state.summary_bar {
            s.set_position(state.completed);
        }
    }

    fn finish(self: Box<Self>) -> FailureSummary {
        let state = self
            .state
            .into_inner()
            .expect("indicatif state mutex poisoned");
        if let Some(s) = &state.summary_bar {
            s.finish();
        }
        // Drop the MultiProgress; remaining bars (if any) are flushed.
        FailureSummary {
            failed: state.failed,
        }
    }
}
