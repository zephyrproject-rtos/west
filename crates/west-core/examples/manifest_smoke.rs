//! Manifest smoke example.
//!
//! Usage: `cargo run -p west-core --example manifest_smoke -- <path-to-manifest>`
//!
//! Parses a manifest from disk (YAML, TOML, or JSON), prints a summary, and
//! exits non-zero on parse / validation failure.

use std::env;
use std::path::PathBuf;
use std::process::ExitCode;

use west_core::manifest::{Manifest, Submodules};

fn main() -> ExitCode {
    let path = match env::args_os().nth(1) {
        Some(p) => PathBuf::from(p),
        None => {
            eprintln!("usage: manifest_smoke <path-to-manifest>");
            return ExitCode::FAILURE;
        }
    };

    let manifest = match Manifest::from_path(&path) {
        Ok(m) => m,
        Err(e) => {
            eprintln!("error: {e}");
            if let Some(src) = std::error::Error::source(&e) {
                eprintln!("  caused by: {src}");
            }
            return ExitCode::FAILURE;
        }
    };

    println!("manifest:");
    if let Some(v) = &manifest.version {
        println!("  version: {v}");
    }
    println!("  self.path: {}", manifest.self_.path.display());
    if !manifest.self_.west_commands.is_empty() {
        println!("  self.west-commands: {:?}", manifest.self_.west_commands);
    }
    if !manifest.group_filter.is_empty() {
        print!("  group-filter:");
        for g in &manifest.group_filter {
            let sign = if g.disabled { '-' } else { '+' };
            print!(" {sign}{}", g.group);
        }
        println!();
    }
    println!("  projects ({}):", manifest.projects.len());
    for p in &manifest.projects {
        let subs = match &p.submodules {
            Submodules::All => "submodules:all",
            Submodules::None => "",
            Submodules::Specific(items) => {
                println!(
                    "    - {} @ {} → {} (path={}, remote={}, submodules:{} entries)",
                    p.name,
                    p.revision,
                    p.url,
                    p.path.display(),
                    p.remote_name,
                    items.len()
                );
                continue;
            }
        };
        println!(
            "    - {} @ {} → {} (path={}, remote={}{}{})",
            p.name,
            p.revision,
            p.url,
            p.path.display(),
            p.remote_name,
            if subs.is_empty() { "" } else { ", " },
            subs,
        );
    }

    ExitCode::SUCCESS
}
