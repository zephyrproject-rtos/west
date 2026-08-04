import runpy
import sys
from pathlib import Path

import pytest
from conftest import cmd, cmd_subprocess

import west.version
from west.app.main import parse_early_args


def test_main():
    # A quick check that the package can be executed as a module which
    # takes arguments, using e.g. "python3 -m west --version" to
    # produce the same results as "west --version", and that both are
    # sane (i.e. the actual version number is printed instead of
    # simply an error message to stderr).

    expected_version = west.version.__version__

    # call west executable directly
    output_directly = cmd(['--version'])
    assert expected_version in output_directly

    output_subprocess = cmd_subprocess('--version')
    assert expected_version in output_subprocess

    # output must be same in both cases
    assert output_subprocess.rstrip() == output_directly.rstrip()


def test_module_run(tmp_path, monkeypatch):
    actual_path = ['initial-path']

    # mock sys.argv and sys.path
    monkeypatch.setattr(sys, 'path', actual_path)
    monkeypatch.setattr(sys, 'argv', ['west', '--version'])

    # ensure that west.app.main is freshly loaded
    sys.modules.pop('west.app.main', None)

    # run west.app.main as module
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module('west.app.main', run_name='__main__')

    # check that exit code is 0
    assert exit_info.value.code == 0

    # check that that the sys.path was correctly inserted
    expected_path = Path(__file__).parents[1] / 'src'
    assert actual_path == [f'{expected_path}', 'initial-path']


# What parse_early_args() returns when given no arguments at all.
EARLY_ARGS_DEFAULTS = {
    'help': False,
    'version': False,
    'zephyr_base': None,
    'verbosity': 0,
    'command_name': None,
    'unexpected_arguments': [],
}


@pytest.mark.parametrize(
    ('argv', 'expected'),
    [
        ([], {}),
        (['topdir'], {'command_name': 'topdir'}),
        (['-h'], {'help': True}),
        (['-h', 'topdir'], {'help': True, 'command_name': 'topdir'}),
        (['-V'], {'version': True}),
        (['--version'], {'version': True}),
        (['-v', 'topdir'], {'verbosity': 1, 'command_name': 'topdir'}),
        (['--verbose', 'topdir'], {'verbosity': 1, 'command_name': 'topdir'}),
        (['-q', 'topdir'], {'verbosity': -1, 'command_name': 'topdir'}),
        (['--quiet', 'topdir'], {'verbosity': -1, 'command_name': 'topdir'}),
        (['-vvv', 'topdir'], {'verbosity': 3, 'command_name': 'topdir'}),
        # An option taking a value must not swallow the command name.
        (['-z', '/p', 'topdir'], {'zephyr_base': '/p', 'command_name': 'topdir'}),
        (['-z/p', 'topdir'], {'zephyr_base': '/p', 'command_name': 'topdir'}),
        (['-z=/p', 'topdir'], {'zephyr_base': '/p', 'command_name': 'topdir'}),
        (['-vz', '/p', 'topdir'], {'verbosity': 1, 'zephyr_base': '/p', 'command_name': 'topdir'}),
        (['-hV', 'topdir'], {'help': True, 'version': True, 'command_name': 'topdir'}),
        # Everything after the command name belongs to the command.
        (['topdir', '-h', '-z', '/p'], {'command_name': 'topdir'}),
        # Unknown options are collected, not treated as the command name.
        (['--nope', 'topdir'], {'command_name': 'topdir', 'unexpected_arguments': ['--nope']}),
    ],
)
def test_parse_early_args(argv, expected):
    # parse_early_args() must agree with the top level argument parser
    # about which arguments are west's own and where the command name
    # is. Alias expansion depends on both.
    assert parse_early_args(argv)._asdict() == EARLY_ARGS_DEFAULTS | expected
