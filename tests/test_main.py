import runpy
import sys
from pathlib import Path

import pytest
from conftest import cmd, cmd_subprocess

import west.app.main as west_main
import west.version


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


@pytest.mark.parametrize(
    "argv, expected_color, expected_command, expected_unexpected",
    [
        (['--color=always', 'help'], 'always', 'help', []),
        (['--color', 'never', 'status'], 'never', 'status', []),
        (['--color', 'invalid', 'status'], None, 'status', ['--color=invalid']),
    ],
)
def test_parse_early_args_color(argv, expected_color, expected_command, expected_unexpected):
    ea = west_main.parse_early_args(argv)
    assert ea.color == expected_color
    assert ea.command_name == expected_command
    assert ea.unexpected_arguments == expected_unexpected
