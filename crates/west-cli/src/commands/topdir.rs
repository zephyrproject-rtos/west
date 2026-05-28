use std::process::ExitCode;

use crate::exit;

pub fn run() -> ExitCode {
    let cwd = match std::env::current_dir() {
        Ok(d) => d,
        Err(e) => {
            log::error!("cannot get current directory: {e}");
            return exit::FAILURE;
        }
    };
    match west_core::topdir::topdir(&cwd) {
        Ok(p) => {
            println!("{}", p.display());
            exit::SUCCESS
        }
        Err(e) => {
            log::error!("{e}");
            exit::FAILURE
        }
    }
}
