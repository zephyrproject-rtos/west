"""Rust ↔ python bridge for west extension commands.

Invoked by the rust CLI as::

    python -m west._dispatch <module-path> <class-name>
        [--inline-config NAME=VALUE]...
        [--extra-config-file PATH]...
        -- <user-argv...>

Environment:
    WEST_TOPDIR — absolute path to the workspace root.

The bridge loads the user's extension `.py` file via importlib,
instantiates the named class (a `WestCommand` subclass), constructs
a `Configuration` + `Manifest` for the workspace (matching the
rust binary's view, including any `--config` / `--config-file`
overrides the user supplied), wires up argparse, and dispatches
`do_run`. `CommandError` is caught and converted to
`sys.exit(returncode)`; other exceptions propagate (rust catches
the non-zero exit and surfaces it).
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import signal
import sys
from pathlib import Path
from typing import NamedTuple

from west.commands import CommandError, WestCommand
from west.configuration import Configuration
from west.manifest import Manifest

# A single `--inline-config NAME=VALUE` pair, in CLI order.
_InlineOverride = tuple[str, str]


class _ParsedArgv(NamedTuple):
    '''Decoded argv shape `_parse_argv` returns. Fields:

    - `module_path`: path to the extension's `.py` file.
    - `class_name`: the `WestCommand` subclass to instantiate.
    - `inline_overrides`: ordered `(name, value)` pairs collected
      from `--inline-config` flags; later entries override earlier.
    - `extra_config_files`: ordered paths from `--extra-config-file`
      flags, appended at top file-backed precedence.
    - `user_argv`: everything after the `--` separator — the
      extension's own arguments.

    NamedTuple so callers can keep the original positional unpack
    (`a, b, c, d, e = _parse_argv(...)`) while the field names
    document the shape.
    '''

    module_path: str
    class_name: str
    inline_overrides: list[_InlineOverride]
    extra_config_files: list[str]
    user_argv: list[str]


def _parse_argv(argv: list[str]) -> _ParsedArgv:
    """Split argv into (module_path, class_name, inline_overrides,
    extra_config_files, user_argv).

    The rust caller passes::

        <module-path> <class-name>
            [--inline-config NAME=VALUE]...
            [--extra-config-file PATH]...
            -- <user-argv...>

    The literal ``--`` separator is removed. The two override lists
    preserve their CLI order (later entries override earlier ones,
    matching the rust binary's own behaviour).
    """
    if len(argv) < 2:
        raise SystemExit("west._dispatch: expected at least <module-path> <class-name>")
    module_path, class_name, *rest = argv

    inline_overrides: list[_InlineOverride] = []
    extra_config_files: list[str] = []
    i = 0
    while i < len(rest) and rest[i] != "--":
        flag = rest[i]
        if flag == "--inline-config":
            i += 1
            if i >= len(rest):
                raise SystemExit("west._dispatch: --inline-config requires NAME=VALUE")
            pair = rest[i]
            if "=" not in pair:
                raise SystemExit(
                    f"west._dispatch: --inline-config expected NAME=VALUE, got {pair!r}"
                )
            name, _, value = pair.partition("=")
            inline_overrides.append((name, value))
        elif flag == "--extra-config-file":
            i += 1
            if i >= len(rest):
                raise SystemExit("west._dispatch: --extra-config-file requires PATH")
            extra_config_files.append(rest[i])
        else:
            raise SystemExit(f"west._dispatch: unexpected flag before --: {flag!r}")
        i += 1
    # `i` is either past-end or points at the `--`. Strip the
    # separator if present.
    user_argv = rest[i + 1 :] if i < len(rest) else []
    return _ParsedArgv(module_path, class_name, inline_overrides, extra_config_files, user_argv)


def _load_command_class(module_path: str, class_name: str) -> type[WestCommand]:
    """Load the extension's `.py` and return the named class."""
    p = Path(module_path).resolve()
    if not p.is_file():
        raise SystemExit(f"west._dispatch: module file not found: {module_path}")
    # Append the file's directory to sys.path so the extension can
    # do `from sibling_module import …`.
    sys.path.insert(0, str(p.parent))
    spec = importlib.util.spec_from_file_location(f"west.commands.ext.{class_name}", str(p))
    if spec is None or spec.loader is None:
        raise SystemExit(f"west._dispatch: failed to load spec for {module_path}")
    module = importlib.util.module_from_spec(spec)
    # Mirror v1's "could not import" wording when the extension's own
    # imports fail (typo, missing dep, etc.). Surface the original
    # exception type + message so authors don't need a traceback.
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        raise SystemExit(
            f"west._dispatch: could not import {module_path}: {type(e).__name__}: {e}"
        ) from e
    try:
        return getattr(module, class_name)
    except AttributeError as e:
        raise SystemExit(f"west._dispatch: class {class_name!r} not found in {module_path}") from e


def _build_config(
    topdir: str | None,
    inline_overrides: list[_InlineOverride],
    extra_config_files: list[str],
) -> Configuration | None:
    """Construct a `Configuration` matching the rust binary's view.

    The rust binary loads system/global/local from disk, appends any
    `--config-file PATH` paths at top file precedence, then attaches
    `--config NAME=VALUE` pairs as read-only inline overrides on top.
    We mirror that exactly so `self.config.get(...)` from inside the
    extension sees the same values the rust binary would see.

    Returns `None` outside a workspace — the extension may have
    `requires_workspace=False` and not care.
    """
    if topdir is None:
        return None
    cfg = Configuration(topdir=topdir, extra_files=extra_config_files or None)
    for name, value in inline_overrides:
        cfg.set_inline(name, value)
    return cfg


def _build_manifest(topdir: str | None, config: Configuration | None) -> Manifest | None:
    """Construct a `Manifest` for the workspace, or `None` when
    we're outside a workspace."""
    if topdir is None:
        return None
    return Manifest.from_topdir(topdir=topdir, config=config)


def main() -> int:
    module_path, class_name, inline_overrides, extra_config_files, user_argv = _parse_argv(
        sys.argv[1:]
    )
    topdir = os.environ.get("WEST_TOPDIR")

    cls = _load_command_class(module_path, class_name)
    try:
        # Extension subclasses override `__init__` to provide their
        # own `name` / `description` via `super().__init__('name', …)`,
        # so calling the constructor with no arguments is the contract
        # — mypy can't see that the subclass narrows the abstract
        # `WestCommand(name=…)` signature.
        cmd = cls()  # type: ignore[call-arg]
    except Exception as e:
        # Mirror v1's "command constructor threw an exception" wording
        # so any user docs / scripts grepping for that phrase keep
        # working. Surface the original exception's message so the
        # author can see what went wrong without digging through a
        # traceback.
        raise SystemExit(
            f"west._dispatch: {class_name!r} command constructor threw "
            f"an exception: {type(e).__name__}: {e}"
        ) from e
    # Surface the issue-927 deprecation note in this command's --help
    # when the extension's constructor set the ignored `help` field.
    # The help shown to users comes from west-commands.yml, not this
    # field; flag it so authors can drop it. See
    # https://github.com/zephyrproject-rtos/west/issues/927.
    if cmd.help:
        cmd.description += f'''
WARNING: in file {module_path},
  the WestCommand constructor of the west extension '{cmd.name}' sets
  the ignored 'help' field to "{cmd.help}"
  but only the help from the west-commands.yml file has ever been used.
  See west bug https://github.com/zephyrproject-rtos/west/issues/927.
  Change that help field to "" to silence this warning while preserving
  compatibility with older west versions that unfortunately required
  that help parameter.'''

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

    # Construction order: config first, then manifest (manifest can
    # consult config for `manifest.path` etc.). Both may be `None`
    # when no `WEST_TOPDIR` is in env — extensions that declare
    # `requires_workspace=False` handle that themselves; the rest
    # will surface a clean error from `WestCommand.run`.
    config = _build_config(topdir, inline_overrides, extra_config_files)
    manifest = _build_manifest(topdir, config) if cmd.requires_workspace else None

    try:
        cmd.run(args, unknown, topdir, manifest=manifest, config=config)
    except CommandError as e:
        if str(e):
            print(f"west: {e}", file=sys.stderr)
        return e.returncode
    except KeyboardInterrupt:
        # Mirrors v1's behavior. Catching this avoids dumping a Python
        # stack on Ctrl+C; the spawned children (cmake / ninja / the
        # built application) already saw SIGINT and unwound on their own.
        #
        # On Unix, reinstate the default SIGINT handler and re-send the
        # signal to self. Two effects:
        #   1. exit status becomes the conventional 128 + SIGINT = 130
        #      that `while`/`until` loops, make, etc. recognise as "user
        #      cancelled the whole chain";
        #   2. the parent rust process sees the child died from SIGINT
        #      rather than a Python-shaped non-zero exit.
        # On Windows there's no SIGINT-resend equivalent; emit
        # STATUS_CONTROL_C_EXIT (0xC000013A) as the conventional
        # "interrupted" exit code. See https://bugs.python.org/issue1054041
        # for the historical context for both branches.
        if sys.platform == "win32":
            CONTROL_C_EXIT_CODE = 0xC000013A - 2**32
            sys.exit(CONTROL_C_EXIT_CODE)
        else:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGINT)
    except BrokenPipeError:
        # `west <ext> | head` and similar — the pipe consumer closed
        # before we finished writing. Not an error worth a traceback.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
