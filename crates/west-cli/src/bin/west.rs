use std::process::ExitCode;

fn main() -> ExitCode {
    // Rust's stdlib installs `SIG_IGN` for `SIGPIPE` at startup, which
    // turns pipe-closed writes into `EPIPE`; `println!` documents
    // panic-on-write-error, so `west cmd | head` would panic instead
    // of exiting cleanly. Reset to the OS default so the kernel kills
    // the process on `EPIPE` with exit status 141 (= 128 + SIGPIPE) —
    // what shells expect from `cmd | head`.
    //
    // Restoration is in the binary, not in `lib.rs`, so the cdylib
    // (loaded by the python wheel) doesn't perturb its host process's
    // signal handling.
    //
    // The natural replacement is rust's `-Zon-broken-pipe=kill`
    // compiler flag (still nightly-only; tracking: rust-lang/rust#97889,
    // mechanism settled via rust-lang/rust#124480). Once stable, swap
    // this block for that flag and drop the libc dep. `libc` is
    // already in the workspace's transitive tree via dirs / rayon /
    // indicatif, so the explicit dep adds nothing.
    #[cfg(unix)]
    // SAFETY: `signal` is async-signal-safe and called exactly once
    // at process start, before any thread or write has run.
    unsafe {
        libc::signal(libc::SIGPIPE, libc::SIG_DFL);
    }

    west_cli::run()
}
