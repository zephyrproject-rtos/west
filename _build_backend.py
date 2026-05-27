"""Custom PEP 517 build backend for west.

Wraps maturin's backend to ALSO build and stage the CLI binary
(`west`/`west.exe`) before maturin packages the wheel. Without this
wrapper, `pip install .` (or any PEP 517 frontend) would produce a
wheel containing only the `_west_native` cdylib — no PATH-installable
`west` binary — because maturin's `bindings = "pyo3"` mode doesn't
auto-build `[[bin]]` targets (see PyO3/maturin#368).

The workaround: before delegating to maturin, run
`cargo build --release --bin west -p west-cli` and copy the resulting
binary to `.wheel-data/scripts/`. Maturin's
`[tool.maturin] data = ".wheel-data"` directive then packs everything
under that directory into the wheel's `<name>-<version>.data/` tree,
which pip unpacks to `scripts/` on the destination interpreter
(PEP 427).

The bin build deliberately omits `--features pyo3` — that feature is
for the cdylib (maturin enables it itself), and turning it on for the
bin pulls pyo3 symbols into the binary's link step without
`-undefined dynamic_lookup` or a libpython link, which breaks on macOS.

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

from maturin import build_editable as _maturin_build_editable

# Re-export the sdist hooks unchanged: they don't touch the wheel data dir.
from maturin import (  # noqa: F401
    build_sdist,
    get_requires_for_build_sdist,
    get_requires_for_build_wheel,
)
from maturin import build_wheel as _maturin_build_wheel
from maturin import prepare_metadata_for_build_wheel as _maturin_prepare_metadata_for_build_wheel

# PEP 660 hook for editable installs. Maturin only registers this on
# newer versions; mirror it conditionally so we don't break older
# maturins that don't expose it.
try:
    from maturin import get_requires_for_build_editable  # noqa: F401
except ImportError:
    pass

try:
    from maturin import (
        prepare_metadata_for_build_editable as _maturin_prepare_metadata_for_build_editable,
    )
except ImportError:
    _maturin_prepare_metadata_for_build_editable = None


_ROOT = Path(__file__).resolve().parent
_DATA = _ROOT / ".wheel-data"
_BIN_NAME = "west.exe" if sys.platform == "win32" else "west"


def _ensure_data_dir() -> None:
    """Create the wheel data dir so maturin's `data` directive validates.

    Maturin checks that `[tool.maturin] data` exists even for metadata-only
    invocations (`pep517 write-dist-info`). The actual CLI binary is staged
    later by `_stage_cli_binary` during the real wheel/editable build.
    """
    (_DATA / "scripts").mkdir(parents=True, exist_ok=True)


def _stage_cli_binary() -> None:
    """Build the CLI binary and stage it for maturin's `data` directive."""
    try:
        subprocess.run(
            [
                "cargo",
                "build",
                "--release",
                "--bin",
                "west",
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


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    _ensure_data_dir()
    return _maturin_prepare_metadata_for_build_wheel(metadata_directory, config_settings)


if _maturin_prepare_metadata_for_build_editable is not None:

    def prepare_metadata_for_build_editable(metadata_directory, config_settings=None):
        _ensure_data_dir()
        return _maturin_prepare_metadata_for_build_editable(metadata_directory, config_settings)


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    _stage_cli_binary()
    return _maturin_build_wheel(wheel_directory, config_settings, metadata_directory)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    _stage_cli_binary()
    return _maturin_build_editable(wheel_directory, config_settings, metadata_directory)
