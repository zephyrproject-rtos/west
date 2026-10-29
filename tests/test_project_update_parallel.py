# Copyright 2026 NXP
#
# SPDX-License-Identifier: Apache-2.0

import concurrent.futures
import io
import json
import logging
import os
import shlex
import subprocess
import sys
import tempfile
import threading
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import GIT, add_commit, cmd, cmd_subprocess, create_repo, create_workspace, rev_parse

from west.app import project as project_commands
from west.app.main import LogHandler
from west.app.project import Update
from west.commands import CommandError
from west.manifest import QUAL_MANIFEST_REV_BRANCH, Project

WAIT = 10
GIT_TIMEOUT = 60
CLI_TIMEOUT = 300


def _entries(root, names=('alpha', 'bravo', 'charlie')):
    return [{'name': name, 'url': (root / 'remote').as_uri()} for name in names]


def _workspace(root, entries, **options):
    workspace = root / 'ws'
    create_workspace(workspace)
    data = {'manifest': {'projects': entries, **options}}
    (workspace / 'mp' / 'west.yml').write_text(json.dumps(data), encoding='utf-8')
    return workspace


def _projects(root, names=('alpha', 'bravo', 'charlie')):
    return [Project(name, (root / 'remote').as_uri(), topdir=root) for name in names]


def _current_context():
    execution = sys.modules.get('west._execution')
    return execution.current_context() if execution is not None else None


def _no_executor(*args, **kwargs):
    pytest.fail('this update must not create an executor')


def _guard_spools(monkeypatch, allowed):
    def guarded(factory):
        def create(*args, **kwargs):
            assert allowed(), 'a direct/live phase allocated a spool'
            return factory(*args, **kwargs)

        return create

    for name in (
        'TemporaryFile',
        'NamedTemporaryFile',
        'SpooledTemporaryFile',
        'TemporaryDirectory',
        'mkstemp',
    ):
        monkeypatch.setattr(tempfile, name, guarded(getattr(tempfile, name)))


class _ObservedStream(io.StringIO):
    def __init__(self, token):
        super().__init__()
        self.token = token
        self.ready = threading.Event()
        self.lock = threading.Lock()

    def write(self, text):
        with self.lock:
            written = super().write(text)
            if self.token in super().getvalue():
                self.ready.set()
            return written

    def getvalue(self):
        with self.lock:
            return super().getvalue()


@pytest.fixture
def update_command(tmp_path):
    command = Update()
    command.args = SimpleNamespace(jobs=2, buffered=None)
    command.updated = set()
    command.auto_cache = command.name_cache = command.path_cache = None
    command._git_ver = (2, 40, 0)
    command.topdir = command._topdir = str(tmp_path)
    return command


@pytest.mark.parametrize(
    'options',
    [('--jobs', '-1'), ('--jobs', 'many'), ('-j',)],
    ids=['negative', 'noninteger', 'bare'],
)
def test_jobs_rejected_before_update(tmp_path, monkeypatch, options):
    workspace = _workspace(tmp_path, _entries(tmp_path))
    config = workspace / '.west' / 'config'
    before = config.read_bytes()
    errors = io.StringIO()

    def unexpected_run(*args, **kwargs):
        pytest.fail('invalid jobs reached update execution')

    monkeypatch.setattr(Update, 'do_run', unexpected_run)
    monkeypatch.setattr(concurrent.futures, 'ThreadPoolExecutor', _no_executor)
    with pytest.raises(SystemExit) as exc:
        cmd(['update', *options], cwd=workspace, stderr=errors)

    assert exc.value.code == 2
    assert 'jobs' in errors.getvalue()
    assert 'unrecognized arguments' not in errors.getvalue()
    assert config.read_bytes() == before
    assert all(not (workspace / name).exists() for name in ('alpha', 'bravo', 'charlie'))


@pytest.mark.parametrize(
    'configured, options, cpus, expected',
    [
        (None, (), 16, 1),
        ('3', (), 16, 3),
        ('3', ('-j', '2'), 16, 2),
        ('many', ('-j', '1'), 16, 1),
        ('-1', ('--jobs', '2'), 16, 2),
        ('3', ('-j', '0'), 16, 8),
        (None, ('-j', '0'), None, 1),
        ('0', (), None, 1),
        ('0', (), 2, 2),
        ('0', (), 16, 8),
    ],
)
def test_jobs_configuration_precedence(tmp_path, monkeypatch, configured, options, cpus, expected):
    workspace = _workspace(tmp_path, _entries(tmp_path))
    if configured is not None:
        cmd(['config', '--local', 'update.jobs', configured], cwd=workspace)
    resolved = []

    def update_all(self):
        resolved.append(self.args.jobs)

    monkeypatch.setattr(os, 'cpu_count', lambda: cpus)
    monkeypatch.setattr(Update, 'update_all', update_all)
    cmd(['update', *options], cwd=workspace)
    assert resolved == [expected]


@pytest.mark.parametrize('configured', ['-1', 'many', '1.5', ''])
def test_invalid_jobs_config_prevents_imports(tmp_path, monkeypatch, configured):
    workspace = _workspace(tmp_path, _entries(tmp_path))
    cmd(['config', '--local', 'update.jobs', configured], cwd=workspace)
    config = workspace / '.west' / 'config'
    before = config.read_bytes()
    errors = io.StringIO()

    def unexpected_update(*args, **kwargs):
        pytest.fail('invalid jobs configuration reached imports or updates')

    monkeypatch.setattr(Update, 'update_all', unexpected_update)
    monkeypatch.setattr(Update, 'update', unexpected_update)
    with pytest.raises(SystemExit) as exc:
        cmd(['update'], cwd=workspace, stderr=errors)
    assert exc.value.code == 1
    assert 'update.jobs' in errors.getvalue()
    assert 'non-negative integer' in errors.getvalue()
    assert config.read_bytes() == before


@pytest.mark.parametrize(
    'options, expected',
    [
        ((), None),
        (('--buffered',), True),
        (('--no-buffered',), False),
        (('--buffered', '--no-buffered'), False),
        (('--no-buffered', '--buffered'), True),
    ],
)
def test_buffering_flags(tmp_path, monkeypatch, options, expected):
    workspace = _workspace(tmp_path, _entries(tmp_path))
    resolved = []

    def update_all(self):
        resolved.append(self.args.buffered)

    monkeypatch.setattr(Update, 'update_all', update_all)
    cmd(['update', '-j', '2', *options], cwd=workspace)
    assert resolved == [expected]


def test_removed_unbuffered_flag_is_rejected(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path, _entries(tmp_path))
    errors = io.StringIO()

    def unexpected_run(*args, **kwargs):
        pytest.fail('removed flag reached update execution')

    monkeypatch.setattr(Update, 'do_run', unexpected_run)
    with pytest.raises(SystemExit) as exc:
        cmd(['update', '--unbuffered'], cwd=workspace, stderr=errors)
    assert exc.value.code == 2
    assert '--unbuffered' in errors.getvalue()


def test_automatic_jobs_are_announced(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path, _entries(tmp_path, tuple(f'project-{i}' for i in range(10))))
    workers = []
    original_executor = concurrent.futures.ThreadPoolExecutor

    def executor(max_workers=None, *args, **kwargs):
        workers.append(max_workers)
        return original_executor(max_workers, *args, **kwargs)

    monkeypatch.setattr(os, 'cpu_count', lambda: 16)
    monkeypatch.setattr(concurrent.futures, 'ThreadPoolExecutor', executor)
    monkeypatch.setattr(Update, 'update', lambda self, project: None)
    output = cmd(['update', '-j', '0'], cwd=workspace)
    assert workers == [8]
    assert 'up to 8 parallel jobs' in output


def test_parallel_update_help_describes_foreground_interaction(tmp_path):
    workspace = _workspace(tmp_path, _entries(tmp_path))
    help_text = ' '.join(cmd(['update', '--help'], cwd=workspace).split())
    assert 'Git stays in the foreground process group' in help_text
    assert '--no-buffered for credential prompts' in help_text
    assert 'without force-killing Git' in help_text
    assert 'must not need interactive credentials' not in help_text


@pytest.mark.parametrize(
    'options, buffered',
    [((), None), (('-j', '1', '--buffered'), True), (('-j', '1', '--no-buffered'), False)],
    ids=['default', 'one-buffered', 'one-live'],
)
def test_serial_update_stays_live(tmp_path, monkeypatch, options, buffered):
    workspace = _workspace(tmp_path, _entries(tmp_path))
    create_repo(workspace / 'alpha')
    owner = threading.get_ident()
    seen = []
    original_popen = subprocess.Popen

    def popen(args, *popen_args, **kwargs):
        assert '--git-common-dir' not in args, 'jobs=1 performed parallel preflight'
        return original_popen(args, *popen_args, **kwargs)

    def update(self, project):
        assert threading.get_ident() == owner
        assert self.args.jobs == 1
        assert self.args.buffered is buffered
        assert _current_context() is None
        self.inf(f'SERIAL-OUT:{project.name}')
        self.err(f'SERIAL-ERR:{project.name}')
        assert f'SERIAL-OUT:{project.name}' in sys.stdout.getvalue()
        assert f'SERIAL-ERR:{project.name}' in sys.stderr.getvalue()
        seen.append(project.name)

    monkeypatch.setattr(subprocess, 'Popen', popen)
    monkeypatch.setattr(concurrent.futures, 'ThreadPoolExecutor', _no_executor)
    monkeypatch.setattr(Update, 'update', update)
    _guard_spools(monkeypatch, lambda: False)
    cmd(['update', *options], cwd=workspace)
    assert seen == ['alpha', 'bravo', 'charlie']


@pytest.mark.parametrize('selection', ['all', 'explicit'])
def test_parallel_cli_revisions(tmp_path, selection):
    remote = tmp_path / 'remote'
    create_repo(remote)
    entries = _entries(tmp_path)
    for entry in entries:
        name = entry['name']
        add_commit(remote, f'parallel revision {name}', files={f'{name}.txt': name})
        entry['revision'] = rev_parse(remote, 'HEAD')
    entries[0]['path'] = str(Path('nested') / 'alpha')
    entries[2]['groups'] = ['off']
    workspace = _workspace(tmp_path, entries, **{'group-filter': ['-off']})
    selected = {'alpha', 'bravo'} if selection == 'all' else {'alpha', 'charlie'}
    options = [] if selection == 'all' else ['charlie', entries[0]['path']]

    serial_root = tmp_path / 'serial'
    serial_root.mkdir()
    serial_workspace = _workspace(serial_root, entries, **{'group-filter': ['-off']})
    serial_output = cmd_subprocess(
        ['-vv', 'update', *options],
        cwd=serial_workspace,
        stderr=subprocess.STDOUT,
        timeout=CLI_TIMEOUT,
        env={**os.environ, 'LC_ALL': 'C'},
    )
    output = cmd_subprocess(
        ['-vv', 'update', '--jobs', '2', *options],
        cwd=workspace,
        stderr=subprocess.STDOUT,
        timeout=CLI_TIMEOUT,
        env={**os.environ, 'LC_ALL': 'C'},
    )

    assert 'west.manifest: DEBUG:' in output
    for entry in entries:
        destination = workspace / entry.get('path', entry['name'])
        if entry['name'] in selected:
            assert rev_parse(destination, 'HEAD') == entry['revision']
            assert rev_parse(destination, 'manifest-rev') == entry['revision']
            assert f'=== updating {entry["name"]}' in output
            assert f'parallel revision {entry["name"]}' in output
        else:
            assert not destination.exists()
    serial_records = Counter(
        line for line in serial_output.splitlines() if line.startswith('HEAD is now at ')
    )
    parallel_records = Counter(
        line for line in output.splitlines() if line.startswith('HEAD is now at ')
    )
    assert serial_records, 'serial oracle emitted no raw checkout records'
    assert parallel_records == serial_records


def _import_workspace(root):
    entries = _entries(root, ('alpha', 'outer', 'skipped'))
    entries[1]['import'] = ['first.yml', 'again.yml']
    entries[2]['groups'] = ['off']
    workspace = _workspace(root, entries, **{'group-filter': ['-off']})
    first = _entries(root, ('inner', 'bravo'))
    first[0]['import'] = True
    for provider, files in (
        (
            'outer',
            {
                'first.yml': json.dumps({'manifest': {'projects': first}}),
                'again.yml': json.dumps({'manifest': {'projects': _entries(root, ('delta',))}}),
            },
        ),
        (
            'inner',
            {'west.yml': json.dumps({'manifest': {'projects': _entries(root, ('charlie',))}})},
        ),
    ):
        create_repo(workspace / provider)
        add_commit(workspace / provider, f'{provider} imports', files=files)
    return workspace


def test_import_providers_finish_before_dispatch(tmp_path, monkeypatch):
    workspace = _import_workspace(tmp_path)
    owner = threading.get_ident()
    events = []
    ordinary = []
    commands = []
    lock = threading.Lock()
    expected = [
        ('update', 'outer'),
        ('read', 'outer', 'first.yml'),
        ('update', 'inner'),
        ('read', 'inner', 'west.yml'),
        ('update', 'outer'),
        ('read', 'outer', 'again.yml'),
    ]
    read_content = project_commands._manifest_content_at

    def read(project, path, encoding):
        assert threading.get_ident() == owner
        assert _current_context() is None
        assert events[-1] == ('update', project.name)
        events.append(('read', project.name, path))
        return read_content(project, path, encoding)

    def update(self, project):
        if project.name in ('outer', 'inner'):
            assert threading.get_ident() == owner
            assert _current_context() is None
            events.append(('update', project.name))
            commands.append(self)
            self.inf(f'PROVIDER:{project.name}')
            assert f'PROVIDER:{project.name}' in sys.stdout.getvalue()
            project.git(['update-ref', QUAL_MANIFEST_REV_BRANCH, 'HEAD'])
        else:
            assert events == expected
            assert threading.get_ident() != owner
            assert _current_context() is not None
            assert {'alpha', 'bravo', 'charlie', 'delta'} <= {
                p.name for p in self.manifest.projects
            }
            with lock:
                ordinary.append(project.name)

    monkeypatch.setattr(project_commands, '_manifest_content_at', read)
    monkeypatch.setattr(Update, 'update', update)
    _guard_spools(monkeypatch, lambda: events == expected)
    output = cmd(['update', '-j', '2'], cwd=workspace)
    assert events == expected
    assert sorted(ordinary) == ['alpha', 'bravo', 'charlie', 'delta']
    assert output.count('PROVIDER:outer') == 2
    assert commands[0].updated == {'outer', 'inner', 'alpha', 'bravo', 'charlie', 'delta'}


def test_explicit_imported_project_is_refused(tmp_path, monkeypatch):
    workspace = _import_workspace(tmp_path)
    for name in ('outer', 'inner'):
        subprocess.run(
            [GIT, 'update-ref', QUAL_MANIFEST_REV_BRANCH, 'HEAD'],
            cwd=workspace / name,
            check=True,
            timeout=GIT_TIMEOUT,
        )

    def unexpected_update(*args, **kwargs):
        pytest.fail('refused imported-only selection performed an update')

    monkeypatch.setattr(Update, 'update', unexpected_update)
    monkeypatch.setattr(concurrent.futures, 'ThreadPoolExecutor', _no_executor)
    errors = io.StringIO()
    with pytest.raises(SystemExit) as exc:
        cmd(['update', '-j', '2', 'charlie'], cwd=workspace, stderr=errors)
    assert exc.value.code == 1
    assert 'refusing to update project: charlie' in errors.getvalue()
    assert 'imports' in errors.getvalue()
    assert not (workspace / 'charlie').exists()


def test_jobs_bound_threads_and_coordinator_success(tmp_path, monkeypatch, update_command):
    update_projects = update_command._update_projects
    projects = _projects(tmp_path, ('alpha', 'bravo', 'charlie', 'delta'))
    Path(projects[1].abspath).mkdir()
    owner = threading.get_ident()
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    returned = set()
    seen = []
    worker_ids = set()
    streams = []
    active = peak = 0
    pools = []
    wait_timeouts = []
    original_executor = concurrent.futures.ThreadPoolExecutor
    original_wait = concurrent.futures.wait
    before = (sys.stdout, sys.stderr, os.getcwd(), dict(os.environ))

    class CoordinatorSet(set):
        def add(self, name):
            assert threading.get_ident() == owner
            assert name in returned, 'success recorded before update returned'
            super().add(name)

        def update(self, names):
            for name in names:
                self.add(name)

    def executor(max_workers=None, *args, **kwargs):
        assert max_workers == 2
        pool = original_executor(max_workers, *args, **kwargs)
        pools.append(pool)
        return pool

    def wait(futures, *, timeout, return_when):
        wait_timeouts.append(timeout)
        return original_wait(futures, timeout=timeout, return_when=return_when)

    def update(project):
        nonlocal active, peak
        assert os.getcwd() == before[2]
        assert dict(os.environ) == before[3]
        assert project.name not in update_command.updated
        with lock:
            active += 1
            peak = max(peak, active)
            seen.append(project)
            worker_ids.add(threading.get_ident())
            streams.append((sys.stdout, sys.stderr))
        try:
            barrier.wait(timeout=WAIT)
            assert project.name not in update_command.updated
            assert os.getcwd() == before[2]
            assert dict(os.environ) == before[3]
            with lock:
                streams.append((sys.stdout, sys.stderr))
                returned.add(project.name)
        finally:
            with lock:
                active -= 1

    update_command.updated = CoordinatorSet()
    monkeypatch.setattr(concurrent.futures, 'ThreadPoolExecutor', executor)
    monkeypatch.setattr(concurrent.futures, 'wait', wait)
    monkeypatch.setattr(update_command, 'update', update)
    try:
        assert update_projects(projects) == []
    finally:
        barrier.abort()
        for pool in pools:
            pool.shutdown(wait=True, cancel_futures=True)

    assert pools
    assert wait_timeouts and set(wait_timeouts) == {0.25}
    assert peak == 2
    assert owner not in worker_ids
    assert len(worker_ids) >= 2
    assert sorted(map(id, seen)) == sorted(map(id, projects))
    assert update_command.updated == {'alpha', 'bravo', 'charlie', 'delta'}
    assert all(out is streams[0][0] and err is streams[0][1] for out, err in streams)
    assert sys.stdout is before[0] and sys.stderr is before[1]
    assert os.getcwd() == before[2] and dict(os.environ) == before[3]


def test_helper_failures_keep_identity_and_order(tmp_path, monkeypatch, update_command):
    update_projects = update_command._update_projects
    update_command.args.jobs = 3
    projects = _projects(tmp_path)
    output = _ObservedStream('CHARLIE-FAILED')
    monkeypatch.setattr(sys, 'stdout', output)

    def update(project):
        if project is projects[0]:
            assert output.ready.wait(WAIT), 'later failure was not replayed while alpha blocked'
        elif project is projects[1]:
            return
        else:
            update_command.inf('CHARLIE-FAILED')
        raise subprocess.CalledProcessError(1, ['git', 'fetch', project.name])

    monkeypatch.setattr(update_command, 'update', update)
    try:
        failed = update_projects(projects)
    finally:
        output.ready.set()
    assert len(failed) == 2
    assert failed[0] is projects[0]
    assert failed[1] is projects[2]
    assert update_command.updated == {'bravo'}


def test_parallel_cli_reports_ordered_failures(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path, _entries(tmp_path))
    commands = []
    errors = io.StringIO()

    def update(self, project):
        commands.append(self)
        if project.name != 'bravo':
            raise subprocess.CalledProcessError(1, ['git', 'fetch', project.name])

    monkeypatch.setattr(Update, 'update', update)
    with pytest.raises(SystemExit) as exc:
        cmd(
            ['update', '-j', '3', 'charlie', 'bravo', 'alpha'],
            cwd=workspace,
            stderr=errors,
        )
    assert exc.value.code == 1
    assert 'update failed for projects: charlie, alpha' in errors.getvalue()
    assert len(commands) == 3
    assert all(command is commands[0] for command in commands)
    assert commands[0].updated == {'bravo'}


@pytest.mark.parametrize('kind', ['fatal', 'system-exit', 'command-error'])
def test_worker_fatal_propagates(tmp_path, monkeypatch, update_command, kind):
    update_projects = update_command._update_projects
    projects = _projects(tmp_path, ('alpha', 'bravo'))
    failure = {
        'fatal': RuntimeError('fatal update sentinel'),
        'system-exit': SystemExit(7),
        'command-error': CommandError(9),
    }[kind]

    def update(project):
        if project is projects[0]:
            raise failure

    monkeypatch.setattr(update_command, 'update', update)
    with pytest.raises(type(failure)) as exc:
        update_projects(projects)
    assert exc.value is failure
    assert 'alpha' not in update_command.updated


@pytest.mark.parametrize('unbuffered', [False, True], ids=['buffered', 'unbuffered'])
def test_output_readiness(tmp_path, monkeypatch, update_command, unbuffered):
    update_projects = update_command._update_projects
    update_command.args.buffered = not unbuffered
    projects = _projects(tmp_path, ('alpha', 'bravo'))
    output = _ObservedStream('bravo:out:ready')
    errors = _ObservedStream('bravo:err:ready')
    alpha_started = threading.Event()
    bravo_emitted = threading.Event()
    release_alpha = threading.Event()
    release_bravo = threading.Event()
    logger = logging.getLogger('west.manifest')
    old_level = logger.level
    handler = LogHandler()
    monkeypatch.setattr(logger, 'handlers', [handler])
    monkeypatch.setattr(logger, 'propagate', False)
    logger.setLevel(logging.DEBUG)
    monkeypatch.setattr(sys, 'stdout', output)
    monkeypatch.setattr(sys, 'stderr', errors)

    def update(project):
        name = project.name
        if name == 'bravo':
            assert alpha_started.wait(WAIT)
        update_command.inf(f'{name}:out:begin')
        update_command.err(f'{name}:err:begin')
        update_command.inf(f'{name}:out:ready')
        logger.warning('%s:err:ready', name)
        if name == 'alpha':
            alpha_started.set()
            assert release_alpha.wait(WAIT)
        else:
            bravo_emitted.set()
            assert release_bravo.wait(WAIT)
        update_command.inf(f'{name}:out:end')
        update_command.err(f'{name}:err:end')

    monkeypatch.setattr(update_command, 'update', update)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as coordinator:
            future = coordinator.submit(update_projects, projects)
            try:
                assert alpha_started.wait(WAIT)
                assert bravo_emitted.wait(WAIT)
                if unbuffered:
                    assert output.ready.wait(WAIT)
                    assert errors.ready.wait(WAIT)
                else:
                    assert not output.ready.is_set()
                    assert not errors.ready.is_set()
                    assert 'alpha:out:' not in output.getvalue()
                    assert 'alpha:err:' not in errors.getvalue()
                assert 'bravo' not in update_command.updated
                release_bravo.set()
                assert output.ready.wait(WAIT), 'bravo stdout waited for blocked alpha'
                assert errors.ready.wait(WAIT), 'bravo stderr waited for blocked alpha'
                assert not future.done()
                assert 'alpha' not in update_command.updated
                if not unbuffered:
                    assert 'alpha:out:' not in output.getvalue()
                    assert 'alpha:err:' not in errors.getvalue()
                release_alpha.set()
                assert future.result(timeout=WAIT) == []
            finally:
                release_alpha.set()
                release_bravo.set()
                future.result(timeout=WAIT)
    finally:
        logger.setLevel(old_level)
        handler.close()

    assert update_command.updated == {'alpha', 'bravo'}
    for stream, channel in ((output, 'out'), (errors, 'err')):
        text = stream.getvalue()
        for name in ('alpha', 'bravo'):
            tokens = [f'{name}:{channel}:{part}' for part in ('begin', 'ready', 'end')]
            assert all(text.count(token) == 1 for token in tokens)
            assert [text.index(token) for token in tokens] == sorted(
                text.index(token) for token in tokens
            )
        if not unbuffered:
            assert text.index(f'bravo:{channel}:end') < text.index(f'alpha:{channel}:begin')


def _conflicting_projects(tmp_path, conflict, directory_alias):
    paths = ['shared', 'shared', 'unrelated']
    if conflict == 'ancestor':
        paths[1] = str(Path('shared') / 'nested')
    elif conflict == 'alias':
        paths[1] = 'alias'
        (tmp_path / 'shared').mkdir()
        directory_alias(tmp_path / 'alias', tmp_path / 'shared')
    elif conflict == 'common-dir':
        paths[1] = 'linked'
        create_repo(tmp_path / 'shared')
        subprocess.run(
            [GIT, 'worktree', 'add', '--detach', str(tmp_path / 'linked'), 'HEAD'],
            cwd=tmp_path / 'shared',
            check=True,
            timeout=GIT_TIMEOUT,
        )
    return [
        Project(name, (tmp_path / 'remote').as_uri(), path=path, topdir=tmp_path)
        for name, path in zip(('alpha', 'bravo', 'charlie'), paths, strict=True)
    ]


@pytest.mark.parametrize(
    'conflict, buffered',
    [('equal', None), ('equal', True), ('ancestor', False), ('common-dir', None), ('alias', None)],
    ids=['equal', 'explicit-buffered', 'ancestor', 'common-dir', 'alias'],
)
def test_single_conflict_group_stays_serial(
    tmp_path, monkeypatch, update_command, conflict, buffered, directory_alias
):
    projects = _conflicting_projects(tmp_path, conflict, directory_alias)[:2]
    output = io.StringIO()
    errors = io.StringIO()
    seen = []
    owner = threading.get_ident()
    update_command.args.buffered = buffered

    def unexpected_resource(*args, **kwargs):
        pytest.fail('effective-one dispatch reached parallel resource setup')

    monkeypatch.setattr(sys, 'stdout', output)
    monkeypatch.setattr(sys, 'stderr', errors)
    monkeypatch.setattr(concurrent.futures, 'ThreadPoolExecutor', _no_executor)
    monkeypatch.setattr(update_command, 'project_auto_cache', unexpected_resource)
    monkeypatch.setattr(Update, 'git_version_info', property(unexpected_resource))
    _guard_spools(monkeypatch, lambda: False)

    def update(project):
        assert threading.get_ident() == owner
        assert _current_context() is None
        marker = f'FALLBACK:{project.name}'
        update_command.inf(marker)
        assert marker in output.getvalue()
        if seen:
            assert f'FALLBACK:{seen[-1]}' in output.getvalue()
        seen.append(project.name)

    monkeypatch.setattr(update_command, 'update', update)
    assert update_command._update_projects(projects) == []
    assert seen == ['alpha', 'bravo']
    assert update_command.updated == set(seen)
    warning = errors.getvalue()
    assert warning.count('WARNING:') == 1
    assert 'alpha' in warning and 'bravo' in warning
    assert 'serial' in warning.lower()
    assert all(f'FALLBACK:{name}' in output.getvalue() for name in seen)


@pytest.mark.parametrize('buffered', [None, False, True])
def test_single_selected_project_stays_live(tmp_path, monkeypatch, update_command, buffered):
    projects = _projects(tmp_path, ('alpha',))
    output = io.StringIO()
    update_command.args.buffered = buffered

    def unexpected_resource(*args, **kwargs):
        pytest.fail('one selected project reached parallel resource setup')

    def update(project):
        assert _current_context() is None
        update_command.inf('ONE-PROJECT-LIVE')
        assert 'ONE-PROJECT-LIVE' in output.getvalue()

    monkeypatch.setattr(sys, 'stdout', output)
    monkeypatch.setattr(update_command, 'update', update)
    monkeypatch.setattr(update_command, 'project_auto_cache', unexpected_resource)
    monkeypatch.setattr(concurrent.futures, 'ThreadPoolExecutor', _no_executor)
    _guard_spools(monkeypatch, lambda: False)
    assert update_command._update_projects(projects) == []
    assert update_command.updated == {'alpha'}


def test_buffering_failure_is_not_hidden_by_cancelled_sibling(
    tmp_path, monkeypatch, update_command
):
    from west import _execution

    projects = _projects(tmp_path, names=('alpha', 'bravo'))
    for project in projects:
        create_repo(project.abspath)
    release = tmp_path / 'release'
    helper = tmp_path / 'emit then wait.py'
    helper.write_text(
        'import sys, time\nfrom pathlib import Path\n'
        'print("spool-failure-marker", flush=True)\n'
        'while not Path(sys.argv[1]).exists():\n    time.sleep(0.02)\n',
        encoding='utf-8',
    )
    original_write = _execution._ProjectOutput.write_bytes
    original_shutdown = concurrent.futures.ThreadPoolExecutor.shutdown

    def write(output, data, *, python=False):
        if not python and b'spool-failure-marker' in data:
            raise OSError('disk failure sentinel')
        return original_write(output, data, python=python)

    def shutdown(executor, *args, **kwargs):
        release.touch()
        return original_shutdown(executor, *args, **kwargs)

    def update(project):
        if project.name == 'alpha':
            invocation = shlex.join([sys.executable, os.fspath(helper), os.fspath(release)])
            project.git(['-c', f'alias.emit=!{invocation}', 'emit'])
        else:
            assert _current_context()._cancelled.wait(WAIT)
            project.git(['rev-parse', 'HEAD'], capture_stdout=True)

    monkeypatch.setattr(_execution._ProjectOutput, 'write_bytes', write)
    monkeypatch.setattr(concurrent.futures.ThreadPoolExecutor, 'shutdown', shutdown)
    monkeypatch.setattr(update_command, 'update', update)
    try:
        with pytest.raises(OSError, match='disk failure sentinel'):
            update_command._update_projects(projects)
    finally:
        release.touch()


@pytest.mark.parametrize('case_sensitive', [False, True])
def test_darwin_path_keys_respect_case_sensitivity(tmp_path, monkeypatch, case_sensitive):
    def pathconf(path, option):
        assert path == tmp_path
        assert option == Update._DARWIN_PC_CASE_SENSITIVE
        return int(case_sensitive)

    monkeypatch.setattr(project_commands, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(os, 'pathconf', pathconf, raising=False)
    upper = Update._path_key(tmp_path / 'Drivers')
    lower = Update._path_key(tmp_path / 'drivers')
    assert (upper == lower) is not case_sensitive
    assert Update._path_key(tmp_path / 'caf\u00e9') == Update._path_key(tmp_path / 'cafe\u0301')


@pytest.mark.parametrize('failure', ['unknown', 'error'])
def test_darwin_path_probe_failure_aborts_batch(tmp_path, monkeypatch, update_command, failure):
    def pathconf(path, option):
        if failure == 'error':
            raise PermissionError('path capability probe failed')
        return -1

    def unexpected_update(project):
        pytest.fail('project updated before path capabilities were established')

    errors = io.StringIO()
    monkeypatch.setattr(project_commands, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(os, 'pathconf', pathconf, raising=False)
    monkeypatch.setattr(sys, 'stderr', errors)
    monkeypatch.setattr(concurrent.futures, 'ThreadPoolExecutor', _no_executor)
    monkeypatch.setattr(update_command, 'update', unexpected_update)
    with pytest.raises(SystemExit) as raised:
        update_command._update_projects(_projects(tmp_path))
    assert raised.value.code == 1
    assert 'alpha: cannot inspect project path' in errors.getvalue()
    assert not update_command.updated


@pytest.mark.parametrize('failure', ['failed', 'returned-failure', 'empty', 'unusable'])
def test_bad_common_dir_probe_aborts_batch(tmp_path, monkeypatch, failure):
    workspace = _workspace(tmp_path, _entries(tmp_path))
    create_repo(workspace / 'alpha')
    monkeypatch.setenv('GIT_COMMON_DIR', os.fspath(workspace / 'alpha' / '.git'))
    unusable = workspace / 'not-a-directory'
    unusable.write_text('not Git metadata', encoding='utf-8')
    errors = io.StringIO()
    original_git = Project.git
    probes = []

    def git(project, args, *git_args, **kwargs):
        if project.name == 'alpha' and '--git-common-dir' in args:
            probes.append(project.name)
            if failure == 'failed':
                raise subprocess.CalledProcessError(
                    128, args, stderr=b'common-dir probe failure sentinel'
                )
            if failure == 'returned-failure':
                return subprocess.CompletedProcess(args, 128, stdout=b'', stderr=b'probe failed')
            stdout = b'' if failure == 'empty' else os.fsencode(unusable) + b'\n'
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr=b'')
        return original_git(project, args, *git_args, **kwargs)

    def unexpected_update(*args, **kwargs):
        pytest.fail('ordinary update began before a failed preflight completed')

    monkeypatch.setattr(Project, 'git', git)
    monkeypatch.setattr(Update, 'update', unexpected_update)
    monkeypatch.setattr(concurrent.futures, 'ThreadPoolExecutor', _no_executor)
    with pytest.raises(SystemExit) as exc:
        cmd(['update', '-j', '2'], cwd=workspace, stderr=errors)
    assert exc.value.code == 1
    assert probes == ['alpha']
    assert 'alpha' in errors.getvalue()
    assert any(
        word in errors.getvalue().lower() for word in ('common', 'metadata', 'git directory')
    )
    assert not (workspace / 'bravo').exists()
    assert not (workspace / 'charlie').exists()


def test_path_key_does_not_retry_an_unavailable_root(tmp_path, monkeypatch):
    calls = []

    def missing(path):
        calls.append(path)
        raise FileNotFoundError('filesystem root unavailable')

    path = tmp_path / 'missing'
    with monkeypatch.context() as scoped:
        scoped.setattr(Path, 'stat', missing)
        with pytest.raises(FileNotFoundError, match='filesystem root unavailable'):
            Update._path_key(path)
    assert calls[-1].parent == calls[-1]
    assert len(calls) == len(path.parents) + 1


@pytest.mark.parametrize(
    'failure, diagnostic',
    [
        ('no-workspace', 'without a workspace path'),
        ('ancestor', 'cannot inspect project path'),
        ('directory', 'cannot inspect project directory'),
        ('metadata', 'cannot inspect Git metadata'),
        ('cache', 'cannot resolve auto-cache path'),
    ],
)
def test_preflight_filesystem_errors_prevent_dispatch(
    tmp_path, monkeypatch, update_command, failure, diagnostic
):
    projects = _projects(tmp_path)
    errors = io.StringIO()
    if failure == 'no-workspace':
        projects[0] = Project('alpha', 'unused')
    elif failure in ('ancestor', 'cache'):
        real_key = Update._path_key
        if failure == 'cache':
            update_command.auto_cache = os.fspath(tmp_path / 'cache')
        target = Update._normalized_path(tmp_path / 'cache' if failure == 'cache' else tmp_path)

        def path_key(path):
            if path == target or (failure == 'cache' and target in path.parents):
                raise PermissionError('filesystem identity unavailable')
            return real_key(path)

        monkeypatch.setattr(Update, '_path_key', staticmethod(path_key))
    else:
        project_path = Update._normalized_path(projects[0].abspath)
        project_path.mkdir()
        if failure == 'directory':
            real_stat = Path.stat
            real_key = Update._path_key
            identity_known = False

            def identify(path):
                nonlocal identity_known
                key = real_key(path)
                if path == project_path:
                    identity_known = True
                return key

            def stat(path, *args, **kwargs):
                if path == project_path and identity_known:
                    raise PermissionError('directory became inaccessible')
                return real_stat(path, *args, **kwargs)

            monkeypatch.setattr(Update, '_path_key', staticmethod(identify))
            monkeypatch.setattr(Path, 'stat', stat)
        else:
            real_lstat = Path.lstat

            def lstat(path, *args, **kwargs):
                if path == project_path / '.git':
                    raise PermissionError('Git metadata inaccessible')
                return real_lstat(path, *args, **kwargs)

            monkeypatch.setattr(Path, 'lstat', lstat)

    def unexpected_update(project):
        pytest.fail('project updated after a preflight failure')

    monkeypatch.setattr(update_command, 'update', unexpected_update)
    monkeypatch.setattr(concurrent.futures, 'ThreadPoolExecutor', _no_executor)
    monkeypatch.setattr(sys, 'stderr', errors)
    _guard_spools(monkeypatch, lambda: False)
    with pytest.raises(SystemExit) as raised:
        update_command._update_projects(projects)
    assert raised.value.code == 1
    assert 'alpha:' in errors.getvalue()
    assert diagnostic in errors.getvalue()
    assert not update_command.updated


@pytest.mark.parametrize('jobs', [1, 2], ids=['direct', 'single-group'])
@pytest.mark.parametrize('unbuffered', [False, True], ids=['buffered', 'live'])
def test_serial_failures_keep_order_and_allow_siblings(
    tmp_path, monkeypatch, update_command, jobs, unbuffered
):
    projects = [
        Project(name, 'unused', path=path, topdir=tmp_path)
        for name, path in [('alpha', 'shared'), ('bravo', 'shared'), ('charlie', 'shared/child')]
    ]
    update_command.args.jobs = jobs
    update_command.args.buffered = not unbuffered
    owner = threading.get_ident()
    seen = []

    def update(project):
        assert threading.get_ident() == owner
        assert _current_context() is None
        seen.append(project.name)
        if project.name != 'bravo':
            raise subprocess.CalledProcessError(1, ['git', 'fetch', project.name])

    monkeypatch.setattr(update_command, 'update', update)
    monkeypatch.setattr(concurrent.futures, 'ThreadPoolExecutor', _no_executor)
    _guard_spools(monkeypatch, lambda: False)
    assert update_command._update_projects(projects) == [projects[0], projects[2]]
    assert seen == ['alpha', 'bravo', 'charlie']
    assert update_command.updated == {'bravo'}


@pytest.mark.parametrize(
    'resource, primary_failure',
    [('scope-close', True), ('scope-close', False)],
)
def test_cleanup_resource_errors_preserve_primary_failure(
    tmp_path, monkeypatch, update_command, capsys, resource, primary_failure
):
    from west import _execution

    target, method = {
        'scope-close': (_execution.ProjectContext, 'close'),
    }[resource]
    original = getattr(target, method)
    message = f'injected {resource} failure'
    primary = RuntimeError('primary update failure')

    def cleanup(self):
        original(self)
        raise OSError(message)

    def update(project):
        if primary_failure:
            raise primary

    monkeypatch.setattr(target, method, cleanup)
    monkeypatch.setattr(update_command, 'update', update)
    with pytest.raises(RuntimeError if primary_failure else OSError) as raised:
        update_command._update_projects(_projects(tmp_path, ('alpha', 'bravo')))
    if primary_failure:
        assert raised.value is primary
        assert isinstance(raised.value.__cause__, OSError)
        assert message in str(raised.value.__cause__)
    else:
        assert message in str(raised.value)
        assert not update_command.updated
    assert message in capsys.readouterr().err
    assert _current_context() is None
    assert not any(thread.name.startswith('west-update') for thread in threading.enumerate())
