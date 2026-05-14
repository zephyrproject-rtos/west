"""Minimum WestCommand surface for Phase 1.

Carries just enough of the existing python ``west.commands`` API
that the rust ↔ python bridge (`west._dispatch`) can wire up
extension commands and call their ``do_run`` hook. The full surface
(properties for ``manifest`` / ``config``, logging helpers, IO
forwarding, etc.) is copied verbatim from src/west/commands.py in
a follow-up commit. What's here today is the contract those tests
exercise — anything beyond it will be added when src/west/ moves.
"""

import argparse
from enum import IntEnum


class Verbosity(IntEnum):
    QUIET = 0
    ERR = 1
    WRN = 2
    INF = 3
    DBG = 4
    DBG_MORE = 5
    DBG_EXTREME = 6


class CommandError(RuntimeError):
    """Raised by command implementations to fail cleanly with an
    exit code. The bridge converts this into ``sys.exit(returncode)``.
    """

    def __init__(self, *args, returncode=1):
        super().__init__(*args)
        self.returncode = returncode


class CommandContextError(CommandError):
    """A command was invoked in an invalid context (e.g. outside a
    workspace when one is required)."""


class ExtensionCommandError(CommandError):
    """Raised when an extension command can't be loaded / dispatched
    correctly. The bridge surfaces the message; traceback only at
    `-vvv`."""

    def __init__(self, *args, hint=None, **kw):
        super().__init__(*args, **kw)
        self.hint = hint


class WestCommand:
    """Base class extension authors subclass.

    Phase 1 keeps just the ``__init__`` + lifecycle methods the
    bridge needs. ``self.manifest`` / ``self.config`` are set as
    plain attributes by ``run()`` — properties + lazy parsing
    arrive when we bring in the full src/west/commands.py.
    """

    def __init__(
        self,
        name,
        help=None,
        description=None,
        accepts_unknown_args=False,
        requires_workspace=True,
        verbosity=Verbosity.INF,
    ):
        if not description or not description.strip():
            raise ValueError("description must not be empty")
        self.name = name
        self.help = help
        self.description = description
        self.accepts_unknown_args = accepts_unknown_args
        self.requires_workspace = requires_workspace
        self.verbosity = verbosity
        self.topdir = None
        self.manifest = None
        self.config = None
        self.parser = None
        self._hooks = []

    # ---- abstract hooks ----

    def do_add_parser(self, parser_adder):
        """Register the command's argparse parser. Subclasses must
        return the created parser."""
        raise NotImplementedError

    def do_run(self, args, unknown):
        """Run the command. Subclasses override."""
        raise NotImplementedError

    # ---- lifecycle ----

    def add_parser(self, parser_adder):
        """Build + cache the parser. Returns the cached parser on
        repeat calls."""
        if self.parser is None:
            self.parser = self.do_add_parser(parser_adder)
        return self.parser

    def add_pre_run_hook(self, hook):
        """Register a callable invoked before ``do_run``. Hooks are
        run in registration order."""
        self._hooks.append(hook)

    def run(self, args, unknown, topdir, manifest=None, config=None):
        """Wire context, run pre-run hooks, dispatch ``do_run``.

        The bridge passes ``topdir`` always; ``manifest`` / ``config``
        are ``None`` in Phase 1 (added when the full python package
        copy lands).
        """
        self.topdir = topdir
        self.manifest = manifest
        self.config = config
        if self.requires_workspace and topdir is None:
            raise CommandContextError(
                f"{self.name} requires a workspace; no topdir was passed"
            )
        for hook in self._hooks:
            hook(self)
        return self.do_run(args, unknown)

    # ---- minimal logging passthroughs ----

    def dbg(self, *args, level=Verbosity.DBG, end="\n"):
        if self.verbosity >= level:
            print(*args, end=end)

    def inf(self, *args, colorize=False, end="\n"):
        if self.verbosity >= Verbosity.INF:
            print(*args, end=end)

    def wrn(self, *args, end="\n"):
        import sys
        if self.verbosity >= Verbosity.WRN:
            print("WARNING:", *args, file=sys.stderr, end=end)

    def err(self, *args, fatal=False, end="\n"):
        import sys
        prefix = "FATAL ERROR:" if fatal else "ERROR:"
        if self.verbosity >= Verbosity.ERR:
            print(prefix, *args, file=sys.stderr, end=end)

    def die(self, *args, exit_code=1):
        import sys
        self.err(*args, fatal=True)
        sys.exit(exit_code)

    def banner(self, *args):
        self.inf("===", *args)

    def small_banner(self, *args):
        self.inf("--", *args)
