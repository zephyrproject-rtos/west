use std::process::ExitCode;

pub fn run() -> ExitCode {
    let cwd = match std::env::current_dir() {
        Ok(d) => d,
        Err(e) => {
            eprintln!("west: cannot get current directory: {e}");
            return ExitCode::FAILURE;
        }
    };
    match west_core::topdir::topdir(&cwd) {
        Ok(p) => {
            println!("{}", p.display());
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("west: {e}");
            ExitCode::FAILURE
        }
    }
}
