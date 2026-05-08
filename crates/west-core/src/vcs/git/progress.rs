//! Parser for `git --progress` output, translating recognised progress
//! lines into [`ProgressEvent`]s.
//!
//! Recognised forms (sampled from `git clone`/`fetch` with `--progress`):
//!
//! ```text
//! Cloning into 'foo'...
//! remote: Enumerating objects: 1234, done.
//! remote: Counting objects:  10% (123/1234)
//! remote: Counting objects: 100% (1234/1234), done.
//! remote: Compressing objects:  50% (50/100), done.
//! Receiving objects:  47% (580/1234), 1.42 MiB | 850 KiB/s
//! Resolving deltas: 100% (456/456), done.
//! From <url>
//!  * branch            <sha> -> FETCH_HEAD
//! ```
//!
//! Anything else falls through to [`ProgressEvent::Line`] verbatim.
//!
//! The parser keeps no state — it returns one event per line, but a
//! single percentage line implies both a phase boundary (with name +
//! total) and a tick (with done/total). Callers either accept the
//! redundant Phase events (sinks like indicatif treat them as no-ops
//! when the phase name didn't change) or compare against a remembered
//! phase name. The git client uses the redundant form because it's
//! stateless and trivial.

use crate::vcs::ProgressEvent;

/// Parsed events for one input line. Most lines produce zero or one
/// events; a percentage line produces two (Phase + Tick) so the sink
/// always sees the phase name on every tick.
pub(super) fn parse_line(line: &str) -> Vec<ProgressEvent<'_>> {
    let trimmed = line.trim_end_matches(['\r', '\n']);
    if trimmed.is_empty() {
        return Vec::new();
    }

    // Strip an optional `remote: ` prefix that `git fetch`/`clone`
    // prepends to phases reported by the server.
    let body = trimmed.strip_prefix("remote: ").unwrap_or(trimmed);

    if let Some(parsed) = parse_phase_or_tick(body) {
        return parsed;
    }

    vec![ProgressEvent::Line(trimmed)]
}

/// Recognise the `<phase>:  XX% (k/n)` and `<phase>: <n>, done.` shapes.
fn parse_phase_or_tick(body: &str) -> Option<Vec<ProgressEvent<'_>>> {
    // The phase name is everything up to the first ':'. If there's no
    // colon, this isn't a progress line.
    let (name, rest) = body.split_once(':')?;
    let name = name.trim();
    if name.is_empty() {
        return None;
    }
    let rest = rest.trim_start();

    // Form: `<phase>: <n>, done.` (Enumerating objects, after first
    // pass) — no percentage, just a final count.
    if let Some(rest) = rest.strip_suffix(", done.")
        && let Ok(n) = rest.parse::<u64>()
    {
        return Some(vec![ProgressEvent::Phase {
            name,
            total: Some(n),
        }]);
    }

    // Form: `<phase>: XX% (k/n)[, …]`. The trailing portion (after the
    // closing ')') may carry size/rate info we ignore.
    if let Some((done, total)) = parse_progress(rest) {
        return Some(vec![
            ProgressEvent::Phase {
                name,
                total: Some(total),
            },
            ProgressEvent::Tick {
                done,
                total: Some(total),
            },
        ]);
    }

    None
}

/// Pull `(k, n)` out of strings like `47% (580/1234), 1.42 MiB | …` or
/// `100% (1234/1234), done.`.
fn parse_progress(s: &str) -> Option<(u64, u64)> {
    // Skip past the percentage to the parenthesised count.
    let open = s.find('(')?;
    let close = s[open + 1..].find(')')?;
    let inside = &s[open + 1..open + 1 + close];
    let (done_str, total_str) = inside.split_once('/')?;
    let done = done_str.trim().parse::<u64>().ok()?;
    let total = total_str.trim().parse::<u64>().ok()?;
    Some((done, total))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn expect_phase(event: &ProgressEvent<'_>, name: &str, total: Option<u64>) {
        match event {
            ProgressEvent::Phase { name: n, total: t } => {
                assert_eq!(*n, name);
                assert_eq!(*t, total);
            }
            other => panic!("expected Phase {{ name: {name:?} }}, got {other:?}"),
        }
    }

    fn expect_tick(event: &ProgressEvent<'_>, done: u64, total: Option<u64>) {
        match event {
            ProgressEvent::Tick { done: d, total: t } => {
                assert_eq!(*d, done);
                assert_eq!(*t, total);
            }
            other => panic!("expected Tick {{ done: {done} }}, got {other:?}"),
        }
    }

    #[test]
    fn empty_line_yields_no_events() {
        assert!(parse_line("").is_empty());
        assert!(parse_line("\n").is_empty());
        assert!(parse_line("\r\n").is_empty());
    }

    #[test]
    fn cloning_into_is_a_plain_line() {
        let events = parse_line("Cloning into 'foo'...");
        assert_eq!(events.len(), 1);
        assert!(matches!(
            events[0],
            ProgressEvent::Line("Cloning into 'foo'...")
        ));
    }

    #[test]
    fn from_url_is_a_plain_line() {
        let events = parse_line("From https://example.com/foo");
        assert_eq!(events.len(), 1);
        assert!(matches!(events[0], ProgressEvent::Line(_)));
    }

    #[test]
    fn enumerating_objects_done_is_phase_with_total() {
        let events = parse_line("remote: Enumerating objects: 1234, done.");
        assert_eq!(events.len(), 1);
        expect_phase(&events[0], "Enumerating objects", Some(1234));
    }

    #[test]
    fn counting_objects_percent_yields_phase_plus_tick() {
        let events = parse_line("remote: Counting objects:  10% (123/1234)");
        assert_eq!(events.len(), 2);
        expect_phase(&events[0], "Counting objects", Some(1234));
        expect_tick(&events[1], 123, Some(1234));
    }

    #[test]
    fn counting_objects_complete_with_done_suffix() {
        let events = parse_line("remote: Counting objects: 100% (1234/1234), done.");
        assert_eq!(events.len(), 2);
        expect_phase(&events[0], "Counting objects", Some(1234));
        expect_tick(&events[1], 1234, Some(1234));
    }

    #[test]
    fn compressing_objects_phase_switch() {
        let events = parse_line("remote: Compressing objects:  50% (50/100), done.");
        assert_eq!(events.len(), 2);
        expect_phase(&events[0], "Compressing objects", Some(100));
        expect_tick(&events[1], 50, Some(100));
    }

    #[test]
    fn receiving_objects_with_size_and_rate() {
        let events = parse_line("Receiving objects:  47% (580/1234), 1.42 MiB | 850 KiB/s");
        assert_eq!(events.len(), 2);
        expect_phase(&events[0], "Receiving objects", Some(1234));
        expect_tick(&events[1], 580, Some(1234));
    }

    #[test]
    fn receiving_objects_complete() {
        let events = parse_line("Receiving objects: 100% (1234/1234), 4.56 MiB | 850 KiB/s, done.");
        assert_eq!(events.len(), 2);
        expect_phase(&events[0], "Receiving objects", Some(1234));
        expect_tick(&events[1], 1234, Some(1234));
    }

    #[test]
    fn resolving_deltas_complete() {
        let events = parse_line("Resolving deltas: 100% (456/456), done.");
        assert_eq!(events.len(), 2);
        expect_phase(&events[0], "Resolving deltas", Some(456));
        expect_tick(&events[1], 456, Some(456));
    }

    #[test]
    fn unknown_line_falls_through_verbatim() {
        let events = parse_line(" * branch            abc -> FETCH_HEAD");
        assert_eq!(events.len(), 1);
        match &events[0] {
            ProgressEvent::Line(s) => assert_eq!(*s, " * branch            abc -> FETCH_HEAD"),
            other => panic!("expected Line, got {other:?}"),
        }
    }

    #[test]
    fn trailing_newline_stripped_for_line() {
        let events = parse_line("Cloning into 'foo'...\n");
        match &events[0] {
            ProgressEvent::Line(s) => assert_eq!(*s, "Cloning into 'foo'..."),
            other => panic!("expected trimmed Line, got {other:?}"),
        }
    }
}
