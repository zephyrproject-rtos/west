//! Exit-code conventions for the `west` binary.
//!
//! Centralised so the contract is one source of truth instead of
//! magic numbers scattered across `Command::run` arms. Callers reach
//! for the constant/function that names the *semantic*, not the
//! numeric value.
//!
//! | Constant | Value | Meaning |
//! |--|--|--|
//! | [`SUCCESS`]   | `0` | command worked |
//! | [`FAILURE`]   | `1` | non-usage failure: command well-formed but the work failed |
//! | [`DIVERGENCE`]| `1` | `--exit-code` signal from diff/status/compare |
//! | [`usage`]     | `2` | invocation itself wrong (bad flag, malformed config, etc.) |
//!
//! `FAILURE` and `DIVERGENCE` share their numeric value (`1`) but
//! the distinct constants make intent greppable: one signals "real
//! failure", the other signals "user asked to be told about
//! divergence and there is some". Scripts that need to differentiate
//! can look at the command + stderr; the OS exit code is the same.
//!
//! For child-process propagation (`exec`, `extension`, `config set
//! --file`), use `ExitCode::from(c)` directly — those forward
//! whatever the child said and aren't part of west's own convention.

use std::process::ExitCode;

/// `0`. Clean success.
pub const SUCCESS: ExitCode = ExitCode::SUCCESS;

/// `1`. Non-usage failure: the invocation parsed fine, but the work
/// failed. Use for runtime/internal issues — config unloadable,
/// per-project step errored, lookup miss, workspace absent.
pub const FAILURE: ExitCode = ExitCode::FAILURE;

/// `1` (numerically equal to [`FAILURE`]). `--exit-code` signal
/// from `diff` / `status` / `compare`: the workspace deviates from
/// the manifest and the caller asked to be told.
pub const DIVERGENCE: ExitCode = ExitCode::FAILURE;

/// `2`. User-error / pre-condition not met. Reserved for "the
/// invocation itself was wrong": bad flag value, malformed config
/// syntax, invalid scope, positional naming an uncloned project,
/// missing required selector. Scripts can distinguish this from
/// [`FAILURE`] (`1`).
///
/// Function rather than `const` because `ExitCode::from(u8)` isn't
/// const-callable on stable. Call as `exit::usage()`.
pub fn usage() -> ExitCode {
    ExitCode::from(2)
}
