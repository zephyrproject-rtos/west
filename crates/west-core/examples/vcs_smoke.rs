//! VCS smoke example.
//!
//! Usage: `cargo run -p west-core --example vcs_smoke -- <url> <dest>`
//!
//! Clones `<url>` into `<dest>` via `GitClient` (the default `vcs.client`),
//! then prints the cloned repo's HEAD SHA. Useful for hand-verifying the
//! VCS layer against a real remote.

use std::env;
use std::path::PathBuf;
use std::process::ExitCode;

use west_core::vcs::{CloneSpec, GitClient, GitOptions, RevSpec, Vcs};

fn main() -> ExitCode {
    let mut args = env::args_os().skip(1);
    let url = match args.next().and_then(|s| s.into_string().ok()) {
        Some(s) => s,
        None => {
            eprintln!("usage: vcs_smoke <url> <dest>");
            return ExitCode::FAILURE;
        }
    };
    let dest = match args.next() {
        Some(s) => PathBuf::from(s),
        None => {
            eprintln!("usage: vcs_smoke <url> <dest>");
            return ExitCode::FAILURE;
        }
    };

    let client = GitClient::new(GitOptions::default());

    let mut out = west_core::vcs::Output::Native;
    let spec = CloneSpec {
        url: &url,
        dest: &dest,
        revision: None,
        origin: None,
        mirror: false,
    };
    if let Err(e) = client.clone(&spec, &mut out) {
        eprintln!("clone failed: {e}");
        return ExitCode::FAILURE;
    }

    match client.sha(&dest, RevSpec::Head) {
        Ok(sha) => {
            println!("{sha}");
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("rev-parse failed: {e}");
            ExitCode::FAILURE
        }
    }
}
