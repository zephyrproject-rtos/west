//! Per-project output gathering for `west update`.
//!
//! [`Reporter`] is the seam between workers and the user-facing UI:
//!
//! - The worker asks the reporter for a [`ProgressSink`] (one per
//!   project) and hands it to the vcs ops.
//! - When the project's run finishes, the worker reports the outcome.
//! - At the end of the whole run, the reporter is dropped and produces
//!   a [`FailureSummary`].
//!
//! Three implementations:
//! - [`SerialReporter`] — for `-j 1`. The worker uses
//!   [`west_core::vcs::Output::Native`], so the sink is a [`NullSink`]
//!   (never invoked); the reporter only tracks failures.
//! - [`BufferingReporter`] — for parallel runs without a TTY. Each
//!   project gets a [`LineSink`] writing into a per-project buffer the
//!   reporter owns; `finish` drains them in completion order to stderr.
//! - [`IndicatifReporter`] — for parallel runs with a TTY; lives in
//!   its own module.

use std::collections::HashMap;
use std::io::{self, Write};
use std::sync::{Arc, Mutex};

use west_core::vcs::{LineSink, NullSink, ProgressSink};

use super::error::UpdateError;

/// Aggregated failure tally returned at the end of an update run.
pub struct FailureSummary {
    pub failed: Vec<(String, String)>,
}

impl FailureSummary {
    pub fn is_empty(&self) -> bool {
        self.failed.is_empty()
    }

    /// Render a one-line summary like `update failed for 2 projects: a, b`.
    pub fn render(&self) -> String {
        if self.failed.is_empty() {
            return String::new();
        }
        let mut s = format!("update failed for {} project", self.failed.len());
        if self.failed.len() != 1 {
            s.push('s');
        }
        s.push_str(": ");
        let names: Vec<&str> = self.failed.iter().map(|(n, _)| n.as_str()).collect();
        s.push_str(&names.join(", "));
        s
    }
}

/// Bridge between workers and the user-facing UI. See module docs.
pub trait Reporter: Send + Sync {
    /// Build a per-project sink. Lifetime is bounded by `&self` so the
    /// sink can hold references into the reporter's state.
    fn sink_for_project<'a>(&'a self, project_name: &str) -> Box<dyn ProgressSink + Send + 'a>;

    /// Worker reports completion. The sink has already accumulated
    /// whatever the reporter needs; this just records the outcome.
    /// Implementations render the error to a string at this point — the
    /// typed structure has done its job and pattern-matching downstream
    /// would only make this trait harder to satisfy.
    fn project_finished(&self, project_name: &str, outcome: Result<(), UpdateError>);

    /// Called once at the end of the run. Implementations flush any
    /// pending state and return the aggregated summary.
    fn finish(self: Box<Self>) -> FailureSummary;
}

// =====================================================================
// SerialReporter — `-j 1`
// =====================================================================

pub struct SerialReporter {
    failed: Mutex<Vec<(String, String)>>,
}

impl SerialReporter {
    pub fn new() -> Self {
        Self {
            failed: Mutex::new(Vec::new()),
        }
    }
}

impl Default for SerialReporter {
    fn default() -> Self {
        Self::new()
    }
}

impl Reporter for SerialReporter {
    fn sink_for_project<'a>(&'a self, _project_name: &str) -> Box<dyn ProgressSink + Send + 'a> {
        // The worker uses Output::Native in serial mode, so the sink is
        // never invoked. Hand back a NullSink so the type-check is
        // satisfied if the worker did decide to call into it.
        Box::new(NullSink)
    }

    fn project_finished(&self, project_name: &str, outcome: Result<(), UpdateError>) {
        if let Err(e) = outcome {
            self.failed
                .lock()
                .expect("SerialReporter mutex poisoned")
                .push((project_name.to_owned(), e.to_string()));
        }
    }

    fn finish(self: Box<Self>) -> FailureSummary {
        FailureSummary {
            failed: self
                .failed
                .into_inner()
                .expect("SerialReporter mutex poisoned"),
        }
    }
}

// =====================================================================
// BufferingReporter — parallel + non-TTY
// =====================================================================

pub struct BufferingReporter {
    state: Mutex<BufferingState>,
}

#[derive(Default)]
struct BufferingState {
    /// Per-project capture buffer, populated by the project's sink in
    /// the reader thread.
    buffers: HashMap<String, Arc<Mutex<Vec<u8>>>>,
    /// Completion order; the order in which `project_finished` was
    /// called from worker threads.
    completion_order: Vec<String>,
    /// Per-project outcomes.
    outcomes: HashMap<String, Result<(), String>>,
}

impl BufferingReporter {
    pub fn new() -> Self {
        Self {
            state: Mutex::new(BufferingState::default()),
        }
    }
}

impl Default for BufferingReporter {
    fn default() -> Self {
        Self::new()
    }
}

impl Reporter for BufferingReporter {
    fn sink_for_project<'a>(&'a self, project_name: &str) -> Box<dyn ProgressSink + Send + 'a> {
        let buf = Arc::new(Mutex::new(Vec::<u8>::new()));
        // First write the banner directly into the buffer so each
        // project's transcript stands alone when we flush at the end.
        {
            let mut guard = buf.lock().expect("buffer mutex poisoned");
            let _ = writeln!(guard, "=== updating {project_name}");
        }
        self.state
            .lock()
            .expect("BufferingReporter mutex poisoned")
            .buffers
            .insert(project_name.to_owned(), Arc::clone(&buf));
        Box::new(BufferedSink { buffer: buf })
    }

    fn project_finished(&self, project_name: &str, outcome: Result<(), UpdateError>) {
        let mut state = self.state.lock().expect("BufferingReporter mutex poisoned");
        let stringified = outcome.map_err(|e| {
            let msg = e.to_string();
            if let Some(buf) = state.buffers.get(project_name)
                && let Ok(mut g) = buf.lock()
            {
                let _ = writeln!(g, "ERROR: {msg}");
            }
            msg
        });
        state.completion_order.push(project_name.to_owned());
        state.outcomes.insert(project_name.to_owned(), stringified);
    }

    fn finish(self: Box<Self>) -> FailureSummary {
        let state = self
            .state
            .into_inner()
            .expect("BufferingReporter mutex poisoned");
        let stderr = io::stderr();
        let mut lock = stderr.lock();
        let mut failed = Vec::new();
        for name in &state.completion_order {
            if let Some(buf) = state.buffers.get(name)
                && let Ok(g) = buf.lock()
            {
                let _ = lock.write_all(&g);
            }
            if let Some(Err(e)) = state.outcomes.get(name) {
                failed.push((name.clone(), e.clone()));
            }
        }
        FailureSummary { failed }
    }
}

/// Sink that pushes captured events through a [`LineSink`] writing into
/// the reporter's per-project buffer.
struct BufferedSink {
    buffer: Arc<Mutex<Vec<u8>>>,
}

impl ProgressSink for BufferedSink {
    fn event(&mut self, event: west_core::vcs::ProgressEvent<'_>) {
        let Ok(mut buf) = self.buffer.lock() else {
            return;
        };
        // Reuse LineSink's formatting on a borrowed writer.
        let mut sink = LineSink::new(&mut *buf);
        sink.event(event);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn failure_summary_renders_singular_and_plural() {
        let one = FailureSummary {
            failed: vec![("a".into(), "boom".into())],
        };
        assert_eq!(one.render(), "update failed for 1 project: a");

        let two = FailureSummary {
            failed: vec![("a".into(), "boom".into()), ("b".into(), "bang".into())],
        };
        assert_eq!(two.render(), "update failed for 2 projects: a, b");
    }

    #[test]
    fn serial_reporter_records_failures_only() {
        let r = Box::new(SerialReporter::new());
        r.project_finished("a", Ok(()));
        r.project_finished(
            "b",
            Err(UpdateError::SetManifestRev(
                west_core::vcs::VcsError::UnknownClient("boom".into()),
            )),
        );
        let summary = r.finish();
        assert_eq!(summary.failed.len(), 1);
        assert_eq!(summary.failed[0].0, "b");
        assert!(summary.failed[0].1.contains("manifest-rev"));
        assert!(summary.failed[0].1.contains("boom"));
    }
}
