use std::ffi::OsString;
use std::process::{Command, ExitCode};

use clap::Args;

#[derive(Args, Debug)]
pub struct ExecArgs {
    /// Program to run, followed by its arguments. Args starting with `-`
    /// are forwarded to the program; `--` is optional but recommended for
    /// clarity, e.g. `west exec -- foo -n hello`.
    #[arg(
        trailing_var_arg = true,
        allow_hyphen_values = true,
        required = true,
        value_name = "COMMAND [ARGS]..."
    )]
    pub command: Vec<OsString>,
}

pub fn run(args: ExecArgs) -> ExitCode {
    // clap's `required = true` guarantees at least one element.
    let prog = args.command.first().expect("required = true");
    let rest = &args.command[1..];

    log::trace!("exec {} {:?}", prog.to_string_lossy(), rest);

    match Command::new(prog).args(rest).status() {
        Ok(status) => {
            if status.success() {
                return ExitCode::SUCCESS;
            }
            // Best-effort exit-code propagation. Codes outside 0..=255 and
            // signal-killed children (None) collapse to FAILURE.
            if let Some(code) = status.code() {
                if let Ok(c) = u8::try_from(code) {
                    return ExitCode::from(c);
                }
            }
            ExitCode::FAILURE
        }
        Err(e) => {
            log::error!("exec {}: {e}", prog.to_string_lossy());
            ExitCode::FAILURE
        }
    }
}
