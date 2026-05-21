# Copyright (c) 2018, 2019, 2020 Nordic Semiconductor ASA
# Copyright 2018, 2019 Foundries.io Ltd
# Copyright 2026 The Zephyr Project Contributors.
#
# SPDX-License-Identifier: Apache-2.0

'''West manifest module.

The data layer (parse + introspect + activity-check) is the rust
`west_core::manifest` implementation exposed via `west._west_native`.
This module wraps the binding with the workspace integration that
sits above it: file-discovery factories (`from_topdir`, `from_file`,
`from_data`), import resolution against the live workspace, project
git helpers, serializers, and the `Project` / `ManifestProject`
classes that callers iterate over.
'''

from __future__ import annotations

import enum
import errno
import logging
import os
import shlex
import subprocess
import warnings
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, NoReturn

from west import _west_native, util
from west._west_native import (
    GroupFilterEntry,
    MalformedManifest,
    ManifestImportFailed,
    ManifestRepo,
    Submodule,
)
from west.configuration import ConfigFile, Configuration, MalformedConfig
from west.util import PathType

__all__ = [
    'MANIFEST_PROJECT_INDEX',
    'MANIFEST_REV_BRANCH',
    'QUAL_MANIFEST_REV_BRANCH',
    'QUAL_REFS_WEST',
    'SCHEMA_VERSION',
    'GroupFilterEntry',
    'ImportFlag',
    'MalformedManifest',
    'Manifest',
    'ManifestImportFailed',
    'ManifestProject',
    'ManifestRepo',
    'ManifestVersionError',
    'Project',
    'Submodule',
    'is_group',
    'manifest_path',
    'validate',
]


#: Index in a `Manifest.projects` attribute where the `ManifestProject`
#: instance for the workspace is stored.
MANIFEST_PROJECT_INDEX = 0

#: A git revision which points to the most recent `Project` update.
MANIFEST_REV_BRANCH = 'manifest-rev'

#: A fully qualified reference to `MANIFEST_REV_BRANCH`.
QUAL_MANIFEST_REV_BRANCH = 'refs/heads/' + MANIFEST_REV_BRANCH

#: Git ref space used by west for internal purposes.
QUAL_REFS_WEST = 'refs/west/'

#: The highest manifest schema version supported by this west version.
SCHEMA_VERSION = '1.2'

_DEFAULT_REV = 'master'
_WEST_YML = 'west.yml'

_logger = logging.getLogger(__name__)


# Public type aliases preserved from the legacy module surface — some
# extensions import these directly. The IntFlag is python-side only;
# the rust resolver currently has a single behaviour and doesn't
# accept these flags (any non-DEFAULT use is a no-op for now).
WestCommandsType = str | list[str]
ImportedContentType = str | list[str] | None
ImporterType = Callable[['Project', str], ImportedContentType]
GroupFilterType = list[str]
GroupsType = list[str]
SubmodulesType = list[Submodule] | bool


class ImportFlag(enum.IntFlag):
    '''Bit flags for *import_flags* arguments to manifest factories.

    The values are mutually exclusive — combinations raise
    ``ValueError``.

    - ``DEFAULT``: resolve all imports (top-level, self, per-project).
    - ``IGNORE``: skip every import silently. The resulting manifest
      contains only the directly-defined projects.
    - ``FORCE_PROJECTS``: resolve per-project imports through a
      caller-supplied ``importer`` callback, but skip filesystem-anchored
      imports (top-level and ``self.import:``). Only meaningful with
      :py:meth:`Manifest.from_data` plus ``importer=``.
    - ``IGNORE_PROJECTS``: resolve filesystem imports normally but skip
      per-project imports. Used by tools that need the manifest before
      the projects it references have been cloned.
    '''

    DEFAULT = 0
    IGNORE = 1
    FORCE_PROJECTS = 2
    IGNORE_PROJECTS = 4


class ManifestVersionError(Exception):
    '''Raised when a manifest declares an unsupported schema version.

    Preserved for API compatibility; the rust resolver currently
    raises `MalformedManifest` instead. This class is kept so that
    extensions importing it don't break at import time.
    '''

    def __init__(self, version: str, file: str | None = None):
        super().__init__(version)
        self.version = version
        self.file = file


class _ManifestImportDepth(ManifestImportFailed):
    '''Carry-over for legacy main.py error handling.

    The rust resolver hard-codes a depth limit and reports it as
    `ManifestImportFailed`; this subclass exists only so that
    `except _ManifestImportDepth` still loads.
    '''


# ----------------------------------------------------------------------
# Module-level helper functions kept on the python side.
# ----------------------------------------------------------------------


def manifest_path() -> str:
    '''Absolute path of the current workspace's manifest file.

    Computed from the active `manifest.path` / `manifest.file`
    configuration options. Raises `MalformedConfig` if the workspace
    doesn't have a `manifest.path` set.
    '''
    topdir = util.west_topdir()
    config = Configuration(topdir=topdir)
    path = config.get('manifest.path', configfile=ConfigFile.LOCAL)
    if path is None:
        raise MalformedConfig('no local manifest.path option')
    manifest_file = config.get('manifest.file', _WEST_YML)
    full = Path(topdir) / path / manifest_file
    if not full.is_file():
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(full))
    return os.fspath(full)


def is_group(raw_group: Any) -> bool:
    '''Return True if *raw_group* is a syntactically valid group name.'''
    if not isinstance(raw_group, str) or not raw_group:
        return False
    if raw_group[0].isdigit() or raw_group in ('-', '+'):
        return False
    return all(c.isalnum() or c in ('_', '-') for c in raw_group)


# Per-format `(parser, schema-parser)` tuple table. The
# `schema-parser` is the binding's strict garde-validated entry
# point — it raises `MalformedManifest` on both format-level
# (parse) AND schema-level errors, with the error message
# referencing the original input language. The `parser` returns
# the raw dict for `validate()`'s return value.
_VALIDATE_PARSERS = {
    'yaml': (_west_native.parse_yaml, _west_native.Manifest.from_yaml_str),
    'json': (_west_native.parse_json, _west_native.Manifest.from_json_str),
    'toml': (_west_native.parse_toml, _west_native.Manifest.from_toml_str),
}


def validate(data: Any, fmt: str = 'yaml') -> dict[str, Any]:
    '''Validate manifest data and return it as a dict.

    *data* is either:

    - a ``dict`` (already-parsed manifest) — *fmt* is ignored;
    - a ``str`` containing the manifest in YAML, JSON, or TOML —
      *fmt* selects the parser. Defaults to ``'yaml'`` for
      backward compatibility.

    Raises:

    - ``TypeError`` if *data* is neither a dict nor a string.
    - ``ValueError`` if *fmt* isn't one of ``'yaml'`` / ``'json'``
      / ``'toml'``.
    - ``MalformedManifest`` if the parser rejects *data*, the
      parsed structure isn't a top-level mapping, or schema
      validation fails.
    '''
    if isinstance(data, dict):
        # `from_dict` hands the dict straight to rust via PyO3 →
        # `serde_json::Value` (no intermediate JSON text). Faster
        # than `dump_json + from_json_str` and clearer in intent:
        # "this dict is already parsed; just validate it."
        _west_native.Manifest.from_dict(data)
        return data
    if isinstance(data, str):
        try:
            parser, schema_parse = _VALIDATE_PARSERS[fmt]
        except KeyError as e:
            raise ValueError(
                f'validate(): unknown format {fmt!r}; '
                f'expected one of: {", ".join(_VALIDATE_PARSERS)}',
            ) from e
        # `schema_parse` catches both format-level parse errors
        # and schema-level violations, raising
        # `MalformedManifest` with a format-tagged message either
        # way. Running it before `parser` keeps the error message
        # in the input's native language (a malformed TOML report
        # would otherwise come from `parse_toml`'s ValueError
        # before we even reached the schema step — same outcome,
        # but the schema parser's message is richer).
        schema_parse(data)
        # If we got here the input is valid; parsing succeeds.
        return parser(data)
    raise TypeError(
        f'validate(): expected str or dict, got {type(data).__name__}',
    )


# ----------------------------------------------------------------------
# Project — has-a relationship with `_west_native.Project`. Constructed
# either directly (taking the full keyword-arg surface) or from a
# native instance via `Project._from_native`.
# ----------------------------------------------------------------------


def _wc_list(west_commands: WestCommandsType | None) -> list[str]:
    if west_commands is None:
        return []
    if isinstance(west_commands, str):
        return [west_commands]
    return list(west_commands)


def _wc_delist(west_commands: list[str]) -> WestCommandsType:
    return west_commands[0] if len(west_commands) == 1 else west_commands


class Project:
    '''Represents a project defined in a west manifest.

    Carries the per-project metadata plus the git helpers extensions
    rely on (`git`, `sha`, `is_cloned`, `read_at`, `listdir_at`, …).
    The data fields are the same as the rust `west_core::manifest::Project`
    surface; the wrapper adds `topdir` / `userdata` (which the rust
    core doesn't track) and the workspace-derived `abspath` /
    `posixpath` properties.
    '''

    def __init__(
        self,
        name: str,
        url: str,
        description: str | None = None,
        revision: str | None = None,
        path: PathType | None = None,
        submodules: SubmodulesType = False,
        clone_depth: int | None = None,
        west_commands: WestCommandsType | None = None,
        topdir: PathType | None = None,
        remote_name: str | None = None,
        groups: GroupsType | None = None,
        userdata: Any | None = None,
    ):
        self.name = name
        self.description = description
        self.url = url
        self.submodules: SubmodulesType = submodules
        self.revision = revision or _DEFAULT_REV
        self.clone_depth = clone_depth
        self.path = os.fspath(path or name)
        self.west_commands = _wc_list(west_commands)
        self.topdir = os.fspath(topdir) if topdir else None
        self.remote_name = remote_name or 'origin'
        self.groups: GroupsType = list(groups) if groups else []
        self.userdata: Any = userdata

    @classmethod
    def _from_native(
        cls,
        native: _west_native.Project,
        topdir: PathType | None = None,
    ) -> Project:
        return cls(
            name=native.name,
            url=native.url,
            description=native.description,
            revision=native.revision,
            path=native.path,
            submodules=native.submodules,
            clone_depth=native.clone_depth,
            west_commands=list(native.west_commands),
            topdir=topdir,
            remote_name=native.remote_name,
            groups=list(native.groups),
            userdata=native.userdata,
        )

    def __eq__(self, other: object) -> bool:
        return NotImplemented

    def __repr__(self) -> str:
        return (
            f'Project("{self.name}", "{self.url}", revision="{self.revision}", '
            f'path={self.path!r}, clone_depth={self.clone_depth}, '
            f'west_commands={self.west_commands}, topdir={self.topdir!r}, '
            f'groups={self.groups!r}, userdata={self.userdata!r})'
        )

    def __str__(self) -> str:
        return f'<Project {self.name} ({(self.abspath or self.path)!r}) at {self.revision}>'

    @property
    def path(self) -> str:
        return self._path

    @path.setter
    def path(self, value: PathType) -> None:
        self._path: str = os.fspath(value)
        # Invalidate cached absolute paths.
        self._abspath: str | None = None
        self._posixpath: str | None = None

    @property
    def abspath(self) -> str | None:
        if self._abspath is None and self.topdir:
            self._abspath = os.path.abspath(Path(self.topdir) / self.path)
        return self._abspath

    @property
    def posixpath(self) -> str | None:
        if self._posixpath is None and self.abspath is not None:
            self._posixpath = Path(self.abspath).as_posix()
        return self._posixpath

    @property
    def name_and_path(self) -> str:
        return f'{self.name} ({self.path})'

    def as_dict(self) -> dict[str, Any]:
        '''Return a representation of this project as a dict, in the same
        shape as the equivalent YAML manifest entry.'''
        ret: dict[str, Any] = {
            'name': self.name,
            'url': self.url,
            'revision': self.revision,
        }
        if self.description:
            ret['description'] = self.description
        if self.path != self.name:
            ret['path'] = self.path
        if self.clone_depth:
            ret['clone-depth'] = self.clone_depth
        if self.west_commands:
            ret['west-commands'] = _wc_delist(self.west_commands)
        if self.groups:
            ret['groups'] = list(self.groups)
        if isinstance(self.submodules, bool):
            if self.submodules:
                ret['submodules'] = True
        else:  # list[Submodule]
            ret['submodules'] = [
                {'path': s.path, **({'name': s.name} if s.name else {})} for s in self.submodules
            ]
        if self.userdata:
            ret['userdata'] = self.userdata
        return ret

    # ---- git helpers ----------------------------------------------------

    def git(
        self,
        cmd: str | list[str],
        extra_args: Iterable[str] = (),
        capture_stdout: bool = False,
        capture_stderr: bool = False,
        check: bool = True,
        cwd: PathType | None = None,
    ) -> subprocess.CompletedProcess:
        '''Run a git command in the project repository.'''
        cmd_list = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)
        extra_args = list(extra_args)
        if cwd is None:
            if self.abspath is None:
                raise ValueError('no abspath; cwd must be given')
            cwd = self.abspath
        args = ['git', *cmd_list, *extra_args]
        cmd_str = util.quote_sh_list(args)
        _logger.debug("running '%s' in %s", cmd_str, cwd)
        popen = subprocess.Popen(
            args,
            cwd=cwd,
            stdout=subprocess.PIPE if capture_stdout else None,
            stderr=subprocess.PIPE if capture_stderr else None,
        )
        stdout, stderr = popen.communicate()
        _logger.debug(
            '"%s" exit code: %d stdout: %r stderr: %r',
            cmd_str,
            popen.returncode,
            stdout,
            stderr,
        )
        if check and popen.returncode:
            raise subprocess.CalledProcessError(
                popen.returncode, cmd_list, output=stdout, stderr=stderr
            )
        return subprocess.CompletedProcess(popen.args, popen.returncode, stdout, stderr)

    def sha(self, rev: str, cwd: PathType | None = None) -> str:
        '''Resolve *rev* to a commit SHA in this project.'''
        cp = self.git(
            ['rev-parse', f'{rev}^{{commit}}'],
            capture_stdout=True,
            capture_stderr=True,
            cwd=cwd,
        )
        return cp.stdout.decode('ascii').strip()

    def is_ancestor_of(self, rev1: str, rev2: str, cwd: PathType | None = None) -> bool:
        '''Return True if *rev1* is an ancestor of *rev2* in this project.'''
        rc = self.git(
            f'merge-base --is-ancestor {rev1} {rev2}',
            check=False,
            cwd=cwd,
        ).returncode
        if rc == 0:
            return True
        if rc == 1:
            return False
        raise RuntimeError(f'unexpected git merge-base result {rc}')

    def is_up_to_date_with(self, rev: str, cwd: PathType | None = None) -> bool:
        return self.is_ancestor_of(rev, 'HEAD', cwd=cwd)

    def is_up_to_date(self, cwd: PathType | None = None) -> bool:
        return self.is_up_to_date_with(self.revision, cwd=cwd)

    def is_cloned(self, cwd: PathType | None = None) -> bool:
        '''Return True if `self.abspath` is the top-level dir of a git repo.'''
        if not self.abspath or not os.path.isdir(self.abspath):
            return False
        res = self.git(
            ['rev-parse', '--show-cdup'],
            check=False,
            cwd=cwd,
            capture_stderr=True,
            capture_stdout=True,
        )
        return not (res.returncode or res.stdout.strip())

    def read_at(
        self,
        path: PathType,
        rev: str | None = None,
        cwd: PathType | None = None,
    ) -> bytes:
        '''Read file contents at a specific revision.'''
        if rev is None:
            rev = self.revision
        cp = self.git(
            ['show', f'{rev}:{os.fspath(path)}'],
            capture_stdout=True,
            capture_stderr=True,
            cwd=cwd,
        )
        return cp.stdout

    def listdir_at(
        self,
        path: PathType,
        rev: str | None = None,
        cwd: PathType | None = None,
        encoding: str | None = None,
    ) -> list[str]:
        '''List directory contents at a specific revision.'''
        if rev is None:
            rev = self.revision
        if encoding is None:
            encoding = 'utf-8'
        out = self.git(
            ['ls-tree', '-z', f'{rev}:{os.fspath(path)}'],
            cwd=cwd,
            capture_stdout=True,
            capture_stderr=True,
        ).stdout
        return [f.decode(encoding).split('\t', 1)[1] for f in out.split(b'\x00') if f]


class ManifestProject(Project):
    '''The manifest repository as a `Project`.

    Synthetic Project for the manifest repo itself: `name = 'manifest'`,
    `url = ''`, `revision = 'HEAD'`. Carries `topdir`, `path`,
    `west_commands`, and `userdata`.
    '''

    def __init__(
        self,
        path: PathType | None = None,
        west_commands: WestCommandsType | None = None,
        topdir: PathType | None = None,
        userdata: Any | None = None,
    ):
        self.name: str = 'manifest'
        self.description = None
        self.url: str = ''
        self.submodules: SubmodulesType = False
        self.revision: str = 'HEAD'
        self.remote_name: str = ''
        self.clone_depth: int | None = None
        self.groups: GroupsType = []
        self.userdata: Any = userdata
        # `None` when the manifest YAML had no `self.path:` and no
        # workspace config supplied one. Distinct from `""`: legacy
        # callers test `mp.path is None`.
        self._path: str | None = os.fspath(path) if path else None  # type: ignore[assignment]
        self.topdir: str | None = os.fspath(topdir) if topdir else None
        self._abspath: str | None = None
        self._posixpath: str | None = None
        self.west_commands = _wc_list(west_commands)

    def __repr__(self) -> str:
        return (
            f'ManifestProject(path={self.path!r}, west_commands={self.west_commands}, '
            f'topdir={self.topdir!r}, userdata={self.userdata!r})'
        )

    @property
    def abspath(self) -> str | None:
        if self._abspath is None and self.topdir and self.path:
            self._abspath = os.path.abspath(os.path.join(self.topdir, self.path))
        return self._abspath

    def as_dict(self) -> dict[str, Any]:
        ret: dict[str, Any] = {}
        if self.path:
            ret['path'] = self.path
        if self.west_commands:
            ret['west-commands'] = _wc_delist(self.west_commands)
        if self.userdata:
            ret['userdata'] = self.userdata
        return ret


# ----------------------------------------------------------------------
# Manifest — wraps `_west_native.Manifest`.
# ----------------------------------------------------------------------


def _default_importer(project: Project, file: str) -> NoReturn:
    raise ManifestImportFailed(f'{project.name}:{file}')


def _filesystem_importer(
    topdir: Path, project_filter: _west_native.ProjectFilter
) -> tuple[Callable[[str, str, str], str | None], list[str]]:
    '''Build a read-only ImportSource callback for `Manifest.from_path_with_imports`.

    Reads import files directly off the workspace's project directories.
    Projects that the workspace config has marked inactive via
    `manifest.project-filter` short-circuit to `None` — the
    project-filter decision comes from the rust binding's
    :py:class:`west._west_native.ProjectFilter`, so the regex semantics
    match :py:class:`west._west_native.LoadedManifest.is_active` exactly.

    Returns `(callback, errors)`. `errors` is a list the callback appends
    to whenever an *active* project's import file is missing or
    unreadable; the rust resolver swallows source-side `Err` returns with
    a warning by design, so the caller is expected to inspect `errors`
    after the resolver returns and raise `ManifestImportFailed` if any
    were recorded.
    '''
    errors: list[str] = []

    def _read(name: str, project_path: str, relative_file: str) -> str | None:
        if project_filter.decide(name) is False:
            return None
        full = topdir / project_path / relative_file
        try:
            return full.read_text(encoding='utf-8')
        except FileNotFoundError:
            errors.append(f'{name}:{relative_file}: file not found')
            return None
        except OSError as e:
            errors.append(f'{name}:{relative_file}: {e}')
            return None

    return _read, errors


def _adapt_legacy_importer(
    importer: ImporterType,
) -> Callable[[str, str, str], str | None]:
    '''Adapt a legacy 2-arg ``importer(project, file)`` callback to the 3-arg
    ``(name, path, file)`` callback the native binding calls.

    The legacy callback receives a :py:class:`Project` instance; we synthesize
    a minimal stub here because the resolver hasn't built the real Project list
    yet when it asks for an import. The stub carries just ``name`` and ``path``,
    which is what every in-tree caller actually reads.

    Legacy return values may be ``str``, ``list[str]``, or ``None``. The native
    binding expects ``str | None``. We join lists with a newline separator —
    legacy behavior used these as concatenated manifest fragments.
    '''

    def _adapt(name: str, project_path: str, relative_file: str) -> str | None:
        stub = Project(name, url='', revision='', path=project_path)
        result = importer(stub, relative_file)
        if result is None:
            return None
        if isinstance(result, list):
            return '\n'.join(result)
        return result

    return _adapt


class Manifest:
    '''Parsed contents of a west manifest file.'''

    encoding: str = 'utf-8'

    @staticmethod
    def from_topdir(
        topdir: PathType | None = None,
        config: Configuration | None = None,
        importer: ImporterType | None = None,
        import_flags: ImportFlag = ImportFlag.DEFAULT,
    ) -> Manifest:
        '''Load the manifest associated with workspace *topdir*.'''
        if topdir is None:
            topdir = Path(util.west_topdir(start=Path.cwd())).resolve()
        return Manifest(
            topdir=topdir,
            config=config,
            importer=importer,
            import_flags=import_flags,
        )

    @staticmethod
    def from_file(
        source_file: PathType | None = None,
        importer: ImporterType | None = None,
        import_flags: ImportFlag = ImportFlag.DEFAULT,
    ) -> Manifest:
        '''Load a manifest from a specific YAML file.

        Resolves the workspace topdir starting from *source_file*'s
        directory (or cwd if omitted). The file may live in any git
        repo inside the workspace — the `manifest.path` / `manifest.file`
        config keys are synthesized to point at it.
        '''
        if source_file is None:
            start = Path.cwd()
        else:
            source_file = Path(source_file).resolve()
            start = source_file.parent
        topdir = Path(util.west_topdir(start=start)).resolve()
        if source_file is None:
            config: Configuration | None = Configuration(topdir=topdir)
            override = None
        else:
            manifest_repo_abs = Path(
                subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], cwd=start)[
                    :-1
                ].decode('utf-8')
            ).resolve()
            override = {
                'manifest.path': str(manifest_repo_abs.relative_to(topdir)),
                'manifest.file': str(source_file.relative_to(manifest_repo_abs)),
            }
            config = None
        return Manifest(
            topdir=topdir,
            config=config,
            importer=importer,
            import_flags=import_flags,
            _override=override,
        )

    @staticmethod
    def from_data(
        source_data: str | dict,
        importer: ImporterType | None = None,
        import_flags: ImportFlag = ImportFlag.DEFAULT,
    ) -> Manifest:
        '''Load a manifest from a YAML string or pre-parsed dict.

        Workspace concerns (topdir, config, imports) are skipped: the
        result has `topdir is None`, `abspath is None`, etc. Imports
        in the YAML raise `ManifestImportFailed`.
        '''
        if not source_data:
            raise MalformedManifest('manifest contains no data')
        return Manifest(
            source_data=source_data,
            importer=importer,
            import_flags=import_flags,
        )

    def __init__(
        self,
        *,
        source_data: str | dict | None = None,
        topdir: PathType | None = None,
        config: Configuration | None = None,
        importer: ImporterType | None = None,
        import_flags: ImportFlag = ImportFlag.DEFAULT,
        _override: dict[str, str] | None = None,
    ):
        # `source_data` is the "literal manifest, ignore the
        # workspace" path; `topdir` / `config` are the
        # "discover via workspace" path. Mixing them is
        # ambiguous — fail fast with the v1 wording so callers
        # that historically gated on these messages keep working.
        if source_data is not None and topdir is not None:
            raise ValueError('both topdir and source_data were given')
        if source_data is not None and config is not None:
            raise ValueError('both source_data and config were given')

        # Mirror legacy `_flags_ok`: the only constraint between bits is
        # that FORCE_PROJECTS is incompatible with IGNORE / IGNORE_PROJECTS.
        # `IGNORE | IGNORE_PROJECTS` is allowed (redundant but consistent —
        # IGNORE subsumes IGNORE_PROJECTS). Unknown bits are rejected.
        known = ImportFlag.IGNORE | ImportFlag.FORCE_PROJECTS | ImportFlag.IGNORE_PROJECTS
        if import_flags & ~known:
            raise ValueError(f'invalid import_flags: {import_flags!r} (unknown bits)')
        if (import_flags & ImportFlag.FORCE_PROJECTS) and (
            import_flags & (ImportFlag.IGNORE | ImportFlag.IGNORE_PROJECTS)
        ):
            raise ValueError(
                f'invalid import_flags: {import_flags!r} '
                f'(FORCE_PROJECTS cannot combine with IGNORE/IGNORE_PROJECTS)'
            )
        if (import_flags & ImportFlag.FORCE_PROJECTS) and importer is None:
            raise ValueError('ImportFlag.FORCE_PROJECTS requires an importer= callback')

        self.topdir: str | None = os.fspath(topdir) if topdir else None
        self.abspath: str | None = None
        self.posixpath: str | None = None
        self.relative_path: str | None = None
        # Literal `manifest.self.path:` from the YAML. `None` when the
        # key was omitted — distinct from the resolved default
        # `"manifest"`. See also the deprecated `yaml_path` alias below.
        self.path_raw: str | None = None
        self.repo_path: str | None = None
        self.repo_abspath: str | None = None
        self.repo_posixpath: str | None = None
        self.has_imports: bool = False
        self.userdata: Any = None
        self.group_filter: GroupFilterType = []
        # `manifest.project-filter` entries (`+regex` / `-regex`). Only
        # `_init_from_topdir` reads them from the workspace config; the
        # `from_data` path leaves this empty.
        # Workspace-aware view of the manifest (`LoadedManifest` from the
        # rust binding) — bundles `manifest.group-filter` and
        # `manifest.project-filter` so `is_active()` consults both in one
        # call. Populated by `_init_from_topdir`; `from_data()` leaves
        # it `None` since there's no workspace.
        self._loaded: _west_native.LoadedManifest | None = None

        if source_data is not None:
            self._init_from_data(source_data, importer, import_flags)
            return
        if self.topdir is None:
            raise ValueError(
                'Manifest() requires either source_data or topdir; '
                'use Manifest.from_topdir() / from_file() / from_data() instead'
            )
        self._init_from_topdir(Path(self.topdir), config, _override, importer, import_flags)

    # ---- Initialization paths ------------------------------------------

    def _init_from_data(
        self,
        source_data: str | dict,
        importer: ImporterType | None,
        import_flags: ImportFlag,
    ) -> None:
        if import_flags == ImportFlag.FORCE_PROJECTS:
            # importer non-None is enforced in __init__. FORCE_PROJECTS only
            # has a YAML-backed binding today (`from_yaml_str_with_imports`),
            # so a dict source is round-tripped through `dump_yaml` for this
            # niche case rather than carrying a parallel `from_dict_with_imports`.
            assert importer is not None
            if isinstance(source_data, dict):
                yaml_str = _west_native.dump_yaml(source_data)
            else:
                yaml_str = source_data
            self._native = _west_native.Manifest.from_yaml_str_with_imports(
                yaml_str, _adapt_legacy_importer(importer), int(import_flags)
            )
        elif isinstance(source_data, dict):
            # `from_dict` hands the python value straight to rust via PyO3 →
            # `serde_json::Value`, skipping the `dump_yaml` + re-parse pair.
            self._native = _west_native.Manifest.from_dict(source_data, int(import_flags))
        else:
            self._native = _west_native.Manifest.from_yaml_str(source_data, int(import_flags))
        self._finalize_from_native(self._native, repo_relpath=None, manifest_file=None)

    def _init_from_topdir(
        self,
        topdir: Path,
        config: Configuration | None,
        override: dict[str, str] | None,
        importer: ImporterType | None,
        import_flags: ImportFlag,
    ) -> None:
        cfg = config if config is not None else Configuration(topdir=topdir)
        if override is not None:
            repo_relpath = override['manifest.path']
            manifest_file = override['manifest.file']
        else:
            # Detect "no workspace local config" via the resolver's actual
            # path set rather than hard-coding `topdir/.west/config.toml`,
            # so the check honors `WEST_CONFIG_LOCAL` overrides.
            if not cfg.get_existing_paths(ConfigFile.LOCAL):
                raise MalformedConfig('local configuration file not found')
            mp = cfg.get('manifest.path', configfile=ConfigFile.LOCAL)
            if mp is None:
                raise MalformedConfig('no local manifest.path option')
            repo_relpath = mp
            manifest_file = cfg.get('manifest.file', _WEST_YML) or _WEST_YML
        # Parse + validate `manifest.project-filter` *before* the native
        # parse so the importer can short-circuit for inactive projects.
        # Invalid filter values surface as `MalformedConfig` here.
        project_filter = _west_native.ProjectFilter.from_config(cfg)
        manifest_repo_root = topdir / repo_relpath
        manifest_path = manifest_repo_root / manifest_file
        if not manifest_path.is_file():
            raise MalformedConfig(f'manifest file not found: {manifest_path}')
        # Legacy: an explicit `importer=` argument on a workspace flow replaces
        # the filesystem reader. The rust resolver still drives the calls.
        if importer is not None:
            native_importer = _adapt_legacy_importer(importer)
            importer_errors: list[str] = []
        else:
            native_importer, importer_errors = _filesystem_importer(topdir, project_filter)
        self._native = _west_native.Manifest.from_path_with_imports(
            manifest_path, manifest_repo_root, native_importer, int(import_flags)
        )
        # The rust resolver swallows source-side errors with a warning; surface
        # the first one as a hard `ManifestImportFailed` so the legacy
        # "active project, missing import file" contract holds.
        if importer_errors:
            raise ManifestImportFailed(importer_errors[0])
        # Workspace-context check: no project may claim the manifest
        # repository's own path. The rust core can't enforce this because
        # the manifest-repo path is a workspace-config concept, not a
        # data-layer one.
        repo_path_str = os.fspath(repo_relpath)
        for np in self._native.projects:
            if np.path == repo_path_str:
                raise MalformedManifest(
                    f'{np.name} path "{repo_path_str}" is taken by the manifest repository'
                )
        # Bundle the parsed manifest with its workspace filters into a
        # `LoadedManifest`. From here on `is_active()` consults
        # `manifest.project-filter` + `manifest.group-filter` in one call,
        # matching the rust CLI commands. Validation already happened
        # above (via `ProjectFilter.from_config`); this just composes.
        self._loaded = _west_native.LoadedManifest.from_components(self._native, cfg)
        self.abspath = os.fspath(manifest_path)
        self.posixpath = manifest_path.as_posix()
        self.relative_path = os.fspath(Path(repo_relpath) / manifest_file)
        self.repo_path = os.fspath(repo_relpath)
        self.repo_abspath = os.fspath(manifest_repo_root)
        self.repo_posixpath = manifest_repo_root.as_posix()
        self._finalize_from_native(
            self._native,
            repo_relpath=repo_relpath,
            manifest_file=str(manifest_file),
        )

    def _finalize_from_native(
        self,
        native: _west_native.Manifest,
        repo_relpath: str | None,
        manifest_file: str | None,
    ) -> None:
        self.path_raw = native.self_.path_raw
        self.group_filter = [
            f'-{e.group}' if e.disabled else f'+{e.group}' for e in native.group_filter
        ]
        # Project list: index 0 is the synthetic ManifestProject; the
        # rest are wrappers over the native projects, with `topdir`
        # injected from the workspace context.
        repo_relpath_for_mp = repo_relpath if repo_relpath is not None else native.self_.path_raw
        self_userdata = native.self_.userdata
        self.userdata = self_userdata
        mp = ManifestProject(
            path=repo_relpath_for_mp,
            west_commands=list(native.self_.west_commands) or None,
            topdir=self.topdir,
            userdata=self_userdata,
        )
        self._projects: list[Project] = [mp]
        for np in native.projects:
            self._projects.append(Project._from_native(np, topdir=self.topdir))

    # ---- Public surface ------------------------------------------------

    @property
    def projects(self) -> list[Project]:
        return list(self._projects)

    @property
    def yaml_path(self) -> str | None:
        '''Deprecated alias for :attr:`path_raw`.'''
        warnings.warn(
            'Manifest.yaml_path is deprecated; use Manifest.path_raw instead.',
            DeprecationWarning,
            stacklevel=2,
        )
        return self.path_raw

    def is_active(
        self,
        project: Project,
        extra_filter: Iterable[str] | None = None,
    ) -> bool:
        '''Return True if *project* passes the manifest's group filter
        (composed with *extra_filter* if any) and isn't explicitly
        disabled by `manifest.project-filter`.

        The two-stage evaluation lives in the rust binding's
        :py:class:`west._west_native.LoadedManifest`, so python and the
        rust CLI commands agree on the answer.
        '''
        if project is self._projects[MANIFEST_PROJECT_INDEX]:
            return True  # the synthetic manifest project is always active
        # Look up the native project by name to hand to the binding.
        native_proj = self._native.project(project.name) if self._native else None
        if native_proj is None or self._loaded is None:
            # Either the caller passed a Project not from this manifest,
            # or there's no workspace (from_data path) so no
            # LoadedManifest exists. Fall back to a python-side
            # group-filter-only evaluation — project-filter is a
            # workspace-config concept and doesn't apply here.
            return self._python_is_active(project, extra_filter)
        extra = _west_native.parse_cli_group_filter(list(extra_filter)) if extra_filter else None
        return self._loaded.is_active(native_proj, extra)

    def _python_is_active(
        self,
        project: Project,
        extra_filter: Iterable[str] | None,
    ) -> bool:
        # Pure-python evaluation, used when callers pass an
        # externally-constructed Project. Matches the rust
        # `is_active` semantics: disabled groups defaulted from the
        # manifest's `group-filter`, optionally composed with extras.
        disabled: set[str] = set()
        for token in self.group_filter:
            self._apply_filter_token(disabled, token)
        if extra_filter:
            for token in extra_filter:
                self._apply_filter_token(disabled, token)
        return not (project.groups and all(g in disabled for g in project.groups))

    @staticmethod
    def _apply_filter_token(disabled: set[str], token: str) -> None:
        if not token or token[0] not in ('+', '-'):
            return
        sign = token[0]
        name = token[1:]
        if sign == '+':
            disabled.discard(name)
        else:
            disabled.add(name)

    def get_projects(
        self,
        project_ids: Iterable[PathType],
        allow_paths: bool = True,
        only_cloned: bool = False,
    ) -> list[Project]:
        '''Look up projects by name (or, with *allow_paths*, by path).

        An empty *project_ids* iterable selects every project in the
        manifest (legacy contract).

        Raises `ValueError` for unknown selectors, and a chained
        `ValueError` carrying `(unknown, uncloned)` lists for
        compatibility with the legacy contract.
        '''
        ids = [os.fspath(raw) for raw in project_ids]
        if not ids:
            candidates = list(self._projects)
        else:
            by_name = {p.name: p for p in self._projects}
            by_path: dict[str, Project] = {}
            if allow_paths:
                for p in self._projects:
                    if p.path:
                        by_path[os.path.normpath(p.path)] = p
            candidates = []
            unknown: list[str] = []
            for sel in ids:
                if sel in by_name:
                    candidates.append(by_name[sel])
                elif allow_paths and os.path.normpath(sel) in by_path:
                    candidates.append(by_path[os.path.normpath(sel)])
                else:
                    unknown.append(sel)
            if unknown:
                raise ValueError(unknown, [])

        if only_cloned:
            uncloned = [p for p in candidates if not p.is_cloned()]
            if uncloned:
                raise ValueError([], uncloned)

        return candidates

    # ---- Serialization -------------------------------------------------

    def as_dict(self, active_only: bool = False) -> dict[str, Any]:
        '''Dict representation in manifest-YAML shape.'''
        manifest_block: dict[str, Any] = {}
        # Serialize only the effective (disabling) entries, sorted by
        # group name for deterministic output. `+enabled` tokens are the
        # default state and round-trip-redundant.
        effective_filter = sorted(t for t in self.group_filter if t.startswith('-'))
        if effective_filter:
            manifest_block['group-filter'] = effective_filter
        # `self:` block.
        mp = self._projects[MANIFEST_PROJECT_INDEX]
        self_block = mp.as_dict() if isinstance(mp, ManifestProject) else {}
        if self_block:
            manifest_block['self'] = self_block
        # Projects block.
        projects: list[dict[str, Any]] = []
        for p in self._projects[1:]:
            if active_only and not self.is_active(p):
                continue
            projects.append(p.as_dict())
        manifest_block['projects'] = projects
        return {'manifest': manifest_block}

    def as_yaml(self, active_only: bool = False) -> str:
        '''YAML serialization of `as_dict(active_only=...)`.

        Output is emitted by the rust serializer (`serde_yaml_ng`);
        the legacy `**kwargs` passthrough to `yaml.safe_dump` is gone.
        '''
        return _west_native.dump_yaml(self.as_dict(active_only=active_only))

    def as_frozen_dict(self, active_only: bool = False) -> dict[str, Any]:
        '''Like `as_dict`, but with each project's `revision` replaced
        by the SHA it currently points to. Requires the projects to be
        cloned; raises `RuntimeError` otherwise.'''
        frozen_projects: list[dict[str, Any]] = []
        for p in self._projects[1:]:
            if active_only and not self.is_active(p):
                continue
            entry = p.as_dict()
            if not p.is_cloned():
                raise RuntimeError(f'cannot freeze: project {p.name} is uncloned')
            try:
                entry['revision'] = p.sha(QUAL_MANIFEST_REV_BRANCH)
            except subprocess.CalledProcessError as e:
                raise RuntimeError(
                    f'project {p.name} revision {p.revision!r} cannot be resolved to a SHA'
                ) from e
            frozen_projects.append(entry)
        manifest_block: dict[str, Any] = {}
        # Serialize only the effective (disabling) entries, sorted by
        # group name for deterministic output. `+enabled` tokens are the
        # default state and round-trip-redundant.
        effective_filter = sorted(t for t in self.group_filter if t.startswith('-'))
        if effective_filter:
            manifest_block['group-filter'] = effective_filter
        mp = self._projects[MANIFEST_PROJECT_INDEX]
        self_block = mp.as_dict() if isinstance(mp, ManifestProject) else {}
        if self_block:
            manifest_block['self'] = self_block
        manifest_block['projects'] = frozen_projects
        return {'manifest': manifest_block}

    def as_frozen_yaml(self, active_only: bool = False) -> str:
        '''YAML serialization of `as_frozen_dict(active_only=...)`.'''
        return _west_native.dump_yaml(self.as_frozen_dict(active_only=active_only))
