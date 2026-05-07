//! Per-project output gathering for `west update`.
//!
//! The trait/Vcs layer is unaware of formatting and ordering: it just
//! writes captured progress bytes into whatever `&mut dyn io::Write` we
//! hand it. This module owns the per-project buffer, the banner format,
//! and the policy for *when* the buffer makes it to the user's terminal.
//!
//! Two implementations of [`Reporter`] in this PR:
//! - [`SerialReporter`] writes each project's output as soon as the
//!   project finishes — appropriate for `-j 1` and the default error
//!   path. The serial loop produces a clean, deterministic transcript.
//! - [`BufferingReporter`] collects finished reports across rayon workers
//!   under a `Mutex` and flushes them in arrival order on
//!   [`Reporter::finish`]. Used for `-j > 1`.
//!
//! Both flush to `io::stderr()` today. A future indicatif-based reporter
//! drops in here without touching workers.

use std::io::{self, Write};
use std::path::PathBuf;
use std::sync::Mutex;

/// One project's complete output transcript. Workers fill `captured`
/// (banner + step output + error notes), set `outcome`, and ship the
/// whole thing to the [`Reporter`].
pub struct ProjectReport {
    pub name: String,
    pub path: PathBuf,
    pub captured: Vec<u8>,
    pub outcome: Result<(), String>,
}

impl ProjectReport {
    pub fn new(name: String, path: PathBuf) -> Self {
        Self {
            name,
            path,
            captured: Vec::new(),
            outcome: Ok(()),
        }
    }

    /// Banner emitted at the top of every project's transcript. Match
    /// Python's `=== updating <name> (<path>):` tone, with a leading marker
    /// that's easy to grep.
    pub fn write_banner(&mut self) -> io::Result<()> {
        writeln!(
            self.captured,
            "=== updating {} ({})",
            self.name,
            self.path.display()
        )
    }
}

/// Aggregated failure tally returned at the end of an update run.
pub struct FailureSummary {
    pub failed: Vec<(String, String)>,
}

impl FailureSummary {
    pub fn is_empty(&self) -> bool {
        self.failed.is_empty()
    }

    /// Render Python's "<name> failed for project{s} <list>" style.
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

pub trait Reporter: Send + Sync {
    /// Called by a worker once a project's transcript is complete.
    fn project_finished(&self, report: ProjectReport);
    /// Called once at the end of the run. Implementations may flush
    /// pending output. Returns the aggregated failure summary.
    fn finish(self: Box<Self>) -> FailureSummary;
}

/// Flush each report immediately on arrival. Best for `-j 1` runs.
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
    fn project_finished(&self, report: ProjectReport) {
        let stderr = io::stderr();
        let mut lock = stderr.lock();
        // Best-effort: if writing to stderr fails the process is in real
        // trouble; nothing useful we can do here.
        let _ = lock.write_all(&report.captured);
        if let Err(e) = &report.outcome {
            self.failed
                .lock()
                .expect("SerialReporter mutex poisoned")
                .push((report.name, e.clone()));
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

/// Collect reports across rayon workers and flush them at `finish` time
/// in arrival order. Used for `-j > 1`.
pub struct BufferingReporter {
    pending: Mutex<Vec<ProjectReport>>,
}

impl BufferingReporter {
    pub fn new() -> Self {
        Self {
            pending: Mutex::new(Vec::new()),
        }
    }
}

impl Default for BufferingReporter {
    fn default() -> Self {
        Self::new()
    }
}

impl Reporter for BufferingReporter {
    fn project_finished(&self, report: ProjectReport) {
        self.pending
            .lock()
            .expect("BufferingReporter mutex poisoned")
            .push(report);
    }

    fn finish(self: Box<Self>) -> FailureSummary {
        let pending = self
            .pending
            .into_inner()
            .expect("BufferingReporter mutex poisoned");
        let stderr = io::stderr();
        let mut lock = stderr.lock();
        let mut failed = Vec::new();
        for report in pending {
            let _ = lock.write_all(&report.captured);
            if let Err(e) = report.outcome {
                failed.push((report.name, e));
            }
        }
        FailureSummary { failed }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn project_report_banner() {
        let mut r = ProjectReport::new("zephyr".into(), PathBuf::from("zephyr"));
        r.write_banner().unwrap();
        let text = String::from_utf8(r.captured).unwrap();
        assert!(text.contains("=== updating zephyr (zephyr)"));
    }

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
}
