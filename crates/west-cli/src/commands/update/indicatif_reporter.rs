//! `Reporter` implementation that drives a `MultiProgress` for parallel
//! `west update` runs against an interactive terminal. Each project gets
//! its own [`indicatif::ProgressBar`]; the bar's message tracks the
//! current phase, and the bar fills as `Tick` events arrive. Failed
//! projects' captured transcripts are dumped above the still-active
//! bars so the user can debug without losing context.

use std::collections::HashMap;
use std::io::Write;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use indicatif::{MultiProgress, ProgressBar, ProgressStyle};

use west_core::vcs::{ProgressEvent, ProgressSink};

use super::output::{FailureSummary, Reporter};

/// Width reserved for the project-name prefix so all bars line up.
const NAME_WIDTH: usize = 32;

/// How often a bar pulses while waiting for the first Tick (so the
/// spinner moves visibly even on slow networks).
const TICK_INTERVAL: Duration = Duration::from_millis(120);

pub struct IndicatifReporter {
    multi: MultiProgress,
    bar_style: ProgressStyle,
    spinner_style: ProgressStyle,
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
        let bar_style = ProgressStyle::with_template(
            "{prefix:32!.cyan.bold} {spinner} {msg:24} [{bar:30.green/blue}] {pos}/{len}",
        )
        .expect("static template")
        .progress_chars("=> ");
        let spinner_style = ProgressStyle::with_template("{prefix:32!.cyan.bold} {spinner} {msg}")
            .expect("static template");

        // Bottom summary line as a styled bar (text-only).
        let summary = multi.add(ProgressBar::new(total_projects as u64));
        summary.set_style(
            ProgressStyle::with_template("Updated {pos}/{len} projects").expect("static template"),
        );
        summary.tick();

        Self {
            multi,
            bar_style,
            spinner_style,
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
        bar.set_prefix(truncate_prefix(project_name, NAME_WIDTH));
        bar.set_style(self.spinner_style.clone());
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

        Box::new(IndicatifSink {
            bar,
            bar_style: self.bar_style.clone(),
            spinner_style: self.spinner_style.clone(),
            transcript,
            current_phase: None,
            uses_bar_style: false,
        })
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

fn truncate_prefix(s: &str, max: usize) -> String {
    if s.len() <= max {
        format!("{s:<max$}", max = max)
    } else {
        let cut = max.saturating_sub(1);
        let mut t = s[..cut].to_owned();
        t.push('…');
        t
    }
}

struct IndicatifSink {
    bar: ProgressBar,
    bar_style: ProgressStyle,
    spinner_style: ProgressStyle,
    transcript: Arc<Mutex<Vec<u8>>>,
    current_phase: Option<String>,
    /// Whether we've switched the bar to the "with bar" style (i.e. a
    /// phase with a known total has arrived). Until then we use the
    /// spinner style.
    uses_bar_style: bool,
}

impl ProgressSink for IndicatifSink {
    fn event(&mut self, event: ProgressEvent<'_>) {
        match event {
            ProgressEvent::Line(s) => {
                if let Ok(mut g) = self.transcript.lock() {
                    let _ = writeln!(g, "{s}");
                }
            }
            ProgressEvent::Phase { name, total } => {
                // The parser emits a fresh Phase before every Tick (it's
                // stateless), so most Phase events are redundant repeats
                // of the *same* phase. Only reset the position when the
                // name actually changes — otherwise the bar visibly
                // flashes to 0% between every tick.
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
                    // Phase without an up-front total: revert to spinner.
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
                // The reporter handles bar cleanup from project_finished
                // so we know the outcome (success/failure styling).
            }
        }
    }
}
