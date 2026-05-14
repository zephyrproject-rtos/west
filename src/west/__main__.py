"""`python -m west` entry point.

The CLI is the rust binary shipped alongside this python package in
the maturin wheel. This shim locates it via `sysconfig` and execs it
with the caller's argv, so legacy `python -m west` invocations
(zephyr's tooling, CI scripts, anything that historically used this
form to locate west) keep working unchanged.

When the binary can't be found we exit with a clear message rather
than the cryptic `ModuleNotFoundError` the old `west.app.main` import
would have produced after that module was removed.
"""

from __future__ import annotations

import os
import sys
import sysconfig


def main():
    name = "west.exe" if sys.platform == "win32" else "west"
    # pip installs the wheel's `<wheel>/west-VERSION.data/scripts/west`
    # to whatever path `sysconfig.get_path("scripts")` reports for the
    # active interpreter — that's the env's `bin/` (or `Scripts\` on
    # Windows). Use the same lookup so we land on the binary that
    # belongs to THIS python.
    scripts_dir = sysconfig.get_path("scripts")
    binary = os.path.join(scripts_dir, name)
    if not os.path.isfile(binary):
        sys.stderr.write(
            f"west: CLI binary not found at {binary}\n"
            "    `pip install west` should place it there; "
            "reinstall the wheel if it's missing.\n"
        )
        sys.exit(1)

    argv = [binary, *sys.argv[1:]]
    if sys.platform == "win32":
        # `os.execv` on Windows has spawn semantics rather than
        # replace-process semantics, with subtle differences around
        # console handling. `subprocess.call` is the conventional
        # safe path on Windows.
        import subprocess

        sys.exit(subprocess.call(argv))
    else:
        os.execv(binary, argv)


if __name__ == "__main__":
    main()
