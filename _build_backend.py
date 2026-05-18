"""Custom PEP 517 build backend for west.

Wraps maturin's backend to ALSO build and stage the CLI binary
(`west`/`west.exe`) before maturin packages the wheel. Without this
wrapper, `pip install .` (or any PEP 517 frontend) would produce a
wheel containing only the `_west_native` cdylib — no PATH-installable
`west` binary — because maturin's `bindings = "pyo3"` mode doesn't
auto-build `[[bin]]` targets (see PyO3/maturin#368).

The workaround: before delegating to maturin, run
`cargo build --release --features pyo3 -p west-cli` and copy the
resulting binary to `.wheel-data/scripts/`. Maturin's
`[tool.maturin] data = ".wheel-data"` directive then packs everything
under that directory into the wheel's `<name>-<version>.data/` tree,
which pip unpacks to `scripts/` on the destination interpreter
(PEP 427).

Once #368 lands native support, this file can be deleted and
`build-backend` switched back to plain `"maturin"`.

Frontends this wrapper handles:

- `pip install .`                  → `build_wheel`
- `pip install -e .`               → `build_editable`
- `pip install <vcs-url>`          → `build_wheel` (from a temp clone)
- `python -m build`                → `build_sdist` + `build_wheel`

`maturin develop` invokes maturin directly and bypasses PEP 517;
developers who use it instead of `pip install -e .` get only the
cdylib. The right entry point for editable installs is
`pip install -e .`.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

# Re-export the metadata + sdist hooks unchanged: they don't produce
# wheel contents, so no binary staging is required.
from maturin import (  # noqa: F401
    build_sdist,
    get_requires_for_build_sdist,
    get_requires_for_build_wheel,
    prepare_metadata_for_build_wheel,
)
from maturin import build_editable as _maturin_build_editable
from maturin import build_wheel as _maturin_build_wheel

# PEP 660 hook for editable installs. Maturin only registers this on
# newer versions; mirror it conditionally so we don't break older
# maturins that don't expose it.
try:
    from maturin import get_requires_for_build_editable  # noqa: F401
except ImportError:
    pass


_ROOT = Path(__file__).resolve().parent
_DATA = _ROOT / ".wheel-data"
_BIN_NAME = "west.exe" if sys.platform == "win32" else "west"


def _stage_cli_binary() -> None:
    """Build the CLI binary and stage it for maturin's `data` directive."""
    try:
        subprocess.run(
            [
                "cargo",
                "build",
                "--release",
                "--features",
                "pyo3",
                "-p",
                "west-cli",
            ],
            cwd=_ROOT,
            check=True,
        )
    except FileNotFoundError as e:
        # cargo isn't on PATH. Pip's build-isolation venv inherits the
        # outer PATH, so this means the developer literally doesn't have
        # rust installed — surface a clear message rather than the bare
        # "[Errno 2] No such file or directory".
        raise RuntimeError(
            "cargo not found on PATH. Install Rust (https://rustup.rs) "
            "and re-run; the west wheel build requires a rust toolchain."
        ) from e

    src = _ROOT / "target" / "release" / _BIN_NAME
    if not src.is_file():
        raise RuntimeError(
            f"cargo build succeeded but {src} is missing; "
            "the west-cli `[[bin]]` target may have been renamed."
        )
    dst_dir = _DATA / "scripts"
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst_dir / _BIN_NAME)


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    _stage_cli_binary()
    return _maturin_build_wheel(wheel_directory, config_settings, metadata_directory)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    _stage_cli_binary()
    return _maturin_build_editable(wheel_directory, config_settings, metadata_directory)
