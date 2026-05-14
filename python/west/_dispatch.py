"""Rust ↔ python bridge for west extension commands.

Invoked by the rust CLI as::

    python -m west._dispatch <module-path> <class-name> -- <user-argv...>

Environment:
    WEST_TOPDIR — absolute path to the workspace root.

The bridge loads the user's extension `.py` file via importlib,
instantiates the named class (a `WestCommand` subclass), wires
up argparse, and dispatches `do_run`. `CommandError` is caught
and converted to `sys.exit(returncode)`; other exceptions
propagate (rust catches the non-zero exit and surfaces it).

Phase 1: `self.manifest` / `self.config` are not populated —
they require the full `west.manifest` / `west.configuration`
python modules that arrive when src/west/ moves into python/.
Extensions that read those will raise `AttributeError`; the
bridge surfaces that as a normal command failure.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

from west.commands import CommandError


def _parse_argv(argv: list[str]) -> tuple[str, str, list[str]]:
    """Split argv into (module_path, class_name, user_argv).

    The rust caller passes ``<module-path> <class-name> -- <user-argv...>``.
    The literal ``--`` separator is removed here.
    """
    if len(argv) < 2:
        raise SystemExit(
            "west._dispatch: expected at least <module-path> <class-name>"
        )
    module_path, class_name, *rest = argv
    if rest and rest[0] == "--":
        rest = rest[1:]
    return module_path, class_name, rest


def _load_command_class(module_path: str, class_name: str):
    """Load the extension's `.py` and return the named class."""
    p = Path(module_path).resolve()
    if not p.is_file():
        raise SystemExit(f"west._dispatch: module file not found: {module_path}")
    # Append the file's directory to sys.path so the extension can
    # do `from sibling_module import …` — matches python `west`'s
    # `_commands_module_from_file` behaviour.
    sys.path.insert(0, str(p.parent))
    spec = importlib.util.spec_from_file_location(
        f"west.commands.ext.{class_name}", str(p)
    )
    if spec is None or spec.loader is None:
        raise SystemExit(f"west._dispatch: failed to load spec for {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return getattr(module, class_name)
    except AttributeError:
        raise SystemExit(
            f"west._dispatch: class {class_name!r} not found in {module_path}"
        )


def main() -> int:
    module_path, class_name, user_argv = _parse_argv(sys.argv[1:])
    topdir = os.environ.get("WEST_TOPDIR")

    cls = _load_command_class(module_path, class_name)
    cmd = cls()

    # Build a parent ArgumentParser with a subparsers slot, then let
    # the command register its own subparser via add_parser. Mirrors
    # python west's main.py:run_extension shape closely enough for
    # the parse_known_args call below.
    parent = argparse.ArgumentParser(prog=f"west {cmd.name}", add_help=False)
    sub = parent.add_subparsers(dest="_cmd")
    cmd.add_parser(sub)
    # The user_argv is already past the `west <name>` prefix the
    # rust CLI swallowed. Parse it against the registered subparser
    # via parse_known_args.
    args, unknown = cmd.parser.parse_known_args(user_argv)

    try:
        cmd.run(args, unknown, topdir, manifest=None, config=None)
    except CommandError as e:
        if str(e):
            print(f"west: {e}", file=sys.stderr)
        return e.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
