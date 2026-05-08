//! Shared indicatif glue. Both `west init` (single op) and
//! `west update` (per-project bar inside a `MultiProgress`) use the
//! same [`IndicatifSink`] to translate `ProgressEvent`s into bar
//! updates; the reporter wrapping is the per-command concern.

use std::io::Write;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use indicatif::{ProgressBar, ProgressStyle};

use west_core::vcs::{ProgressEvent, ProgressSink};

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
