//! Scoped SIGINT handling for `west update`.
//!
//! `west update -j N` spawns up to N `git` children in west's own
//! process group. A terminal Ctrl+C delivers SIGINT to every one of
//! them *and* to west. Each git cleans up its own ref locks on SIGINT
//! (git registers a lockfile cleanup handler), but west's default
//! disposition is to terminate *immediately* -- which races, and can
//! lose, git's cleanup: the parent vanishing closes the stdio pipes
//! git is mid-write on, and the process group tears down before
//! every child finished. The lost cleanup leaves a stale
//! `manifest-rev.lock` that fails the *next* `west update`.
//!
//! The fix, for the duration of [`super::run`]: catch SIGINT instead
//! of dying. The handler only flips an atomic flag, so west stays
//! alive and its blocked `wait()` on each git child returns *after*
//! that child's own cleanup completed. The worker loop polls the flag
//! and stops launching new projects (no new `update-ref` starts after
//! Ctrl+C). When the pool drains, [`reraise`] restores the default
//! disposition and re-raises SIGINT so west dies *from the signal* --
//! conventional status 130 + `WIFSIGNALED`, which lets shell loops
//! (`while ...; do west update; done`) break out. A second Ctrl+C
//! short-circuits straight to that hard exit, so a stuck west is
//! always force-killable.
//!
//! Unix only: the bug -- and git's signal-driven lock cleanup -- is a
//! POSIX story. Windows needs `SetConsoleCtrlHandler` and is tracked
//! separately; there the guard is a no-op and `cancelled()` stays
//! `false`, preserving today's instant-abort behaviour.

use std::sync::atomic::{AtomicBool, Ordering};

/// Set by the SIGINT handler; polled by the worker loop and the
/// post-run exit path. Process-global because a signal handler can't
/// carry state.
static CANCELLED: AtomicBool = AtomicBool::new(false);

/// `true` once a SIGINT has been observed while an [`InterruptGuard`]
/// is active.
pub(super) fn cancelled() -> bool {
    CANCELLED.load(Ordering::SeqCst)
}

#[cfg(unix)]
extern "C" fn handle_sigint(_sig: libc::c_int) {
    // Async-signal-safe: an atomic swap, plus -- on the second hit --
    // restore-default + re-raise. No allocation, no locks, no I/O.
    if CANCELLED.swap(true, Ordering::SeqCst) {
        // Second SIGINT: the user wants out now rather than waiting
        // for in-flight git to wind down. Restore the default
        // disposition and re-raise so the kernel terminates us
        // instead of looping back into this handler.
        unsafe {
            libc::signal(libc::SIGINT, libc::SIG_DFL);
            libc::raise(libc::SIGINT);
        }
    }
}

/// RAII guard scoping the catch-don't-die behaviour to [`super::run`].
/// Installs the SIGINT handler on construction, restores the previous
/// disposition on drop -- so every *other* command keeps the default
/// instant-abort on Ctrl+C.
pub(super) struct InterruptGuard {
    #[cfg(unix)]
    previous: libc::sighandler_t,
}

impl InterruptGuard {
    pub(super) fn install() -> Self {
        // Reset for this run: `run` is called once per process today,
        // but resetting keeps the flag honest if that ever changes.
        CANCELLED.store(false, Ordering::SeqCst);
        #[cfg(unix)]
        {
            // Cast through the fn-pointer type before the integer
            // `sighandler_t` -- a direct `fn item as usize` trips
            // clippy's `fn_to_numeric_cast`.
            let handler = handle_sigint as extern "C" fn(libc::c_int) as libc::sighandler_t;
            // SAFETY: `signal` is async-signal-safe and `handle_sigint`
            // does only signal-safe work. Installed once at the top of
            // `run`; the saved disposition is restored on drop.
            let previous = unsafe { libc::signal(libc::SIGINT, handler) };
            InterruptGuard { previous }
        }
        #[cfg(not(unix))]
        InterruptGuard {}
    }
}

impl Drop for InterruptGuard {
    fn drop(&mut self) {
        #[cfg(unix)]
        // SAFETY: restoring the disposition saved at install time;
        // signal-safe.
        unsafe {
            libc::signal(libc::SIGINT, self.previous);
        }
    }
}

/// Terminate the process as if killed by SIGINT (status 130 +
/// `WIFSIGNALED`). Called from the post-run exit path once a
/// cancellation has been observed and in-flight work has drained.
/// Mirrors the python `_dispatch` Ctrl+C handling so a built-in and an
/// extension look identical to a shell loop. Never returns.
pub(super) fn reraise() -> ! {
    #[cfg(unix)]
    unsafe {
        libc::signal(libc::SIGINT, libc::SIG_DFL);
        libc::raise(libc::SIGINT);
    }
    // `raise(SIGINT)` under `SIG_DFL` doesn't return; the explicit exit
    // is the unreachable terminator the compiler needs (and the actual
    // exit on non-unix).
    std::process::exit(130)
}
