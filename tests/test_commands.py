# Copyright (c) 2021, Nordic Semiconductor ASA

import pytest

from west.commands import Verbosity, WestCommand

gv = WestCommand._parse_git_version


def test_parse_git_version():
    # White box test for git parsing behavior.
    assert gv(b'git version 2.25.1\n') == (2, 25, 1)
    assert gv(b'git version 2.28.0.windows.1\n') == (2, 28, 0)
    assert gv(b'git version 2.24.3 (Apple Git-128)\n') == (2, 24, 3)
    assert gv(b'git version 2.29.GIT\n') == (2, 29)
    assert gv(b'not a git version') is None


class WestCommandImpl(WestCommand):
    def do_add_parser(self):
        pass

    def do_run(self):
        pass


cmd = WestCommandImpl(name="x", help="y", description="z")

TEST_STR = "This is some test string"
COL_RED = "\x1b[91m"
COL_YELLOW = "\x1b[93m"
COL_OFF = "\x1b[0m"

EXPECTED_LOG_DEFAULT = f'{TEST_STR}\n'
EXPECTED_LOG_WARNING = f'{COL_YELLOW}WARNING: {TEST_STR}\n{COL_OFF}'
EXPECTED_LOG_ERROR = f'{COL_RED}ERROR: {TEST_STR}\n{COL_OFF}'
EXPECTED_LOG_FATAL_ERROR = f'{COL_RED}FATAL ERROR: {TEST_STR}\n{COL_OFF}'

TEST_CASES_LOG = [
    # max_log_level, log_cmd, expected_stdout, expected_stderr
    (Verbosity.DBG_EXTREME, cmd.dbg, EXPECTED_LOG_DEFAULT, ''),
    (Verbosity.DBG_EXTREME, cmd.inf, EXPECTED_LOG_DEFAULT, ''),
    (Verbosity.DBG_EXTREME, cmd.wrn, '', EXPECTED_LOG_WARNING),
    (Verbosity.DBG_EXTREME, cmd.err, '', EXPECTED_LOG_ERROR),
    (Verbosity.DBG_MORE, cmd.dbg, EXPECTED_LOG_DEFAULT, ''),
    (Verbosity.DBG_MORE, cmd.inf, EXPECTED_LOG_DEFAULT, ''),
    (Verbosity.DBG_MORE, cmd.wrn, '', EXPECTED_LOG_WARNING),
    (Verbosity.DBG_MORE, cmd.err, '', EXPECTED_LOG_ERROR),
    (Verbosity.DBG, cmd.dbg, EXPECTED_LOG_DEFAULT, ''),
    (Verbosity.DBG, cmd.inf, EXPECTED_LOG_DEFAULT, ''),
    (Verbosity.DBG, cmd.wrn, '', EXPECTED_LOG_WARNING),
    (Verbosity.DBG, cmd.err, '', EXPECTED_LOG_ERROR),
    (Verbosity.INF, cmd.dbg, '', ''),
    (Verbosity.INF, cmd.inf, EXPECTED_LOG_DEFAULT, ''),
    (Verbosity.INF, cmd.wrn, '', EXPECTED_LOG_WARNING),
    (Verbosity.INF, cmd.err, '', EXPECTED_LOG_ERROR),
    (Verbosity.WRN, cmd.dbg, '', ''),
    (Verbosity.WRN, cmd.inf, '', ''),
    (Verbosity.WRN, cmd.wrn, '', EXPECTED_LOG_WARNING),
    (Verbosity.WRN, cmd.err, '', EXPECTED_LOG_ERROR),
    (Verbosity.ERR, cmd.dbg, '', ''),
    (Verbosity.ERR, cmd.inf, '', ''),
    (Verbosity.ERR, cmd.wrn, '', ''),
    (Verbosity.ERR, cmd.err, '', EXPECTED_LOG_ERROR),
    (Verbosity.QUIET, cmd.dbg, '', ''),
    (Verbosity.QUIET, cmd.inf, '', ''),
    (Verbosity.QUIET, cmd.wrn, '', ''),
    (Verbosity.QUIET, cmd.err, '', ''),
]


@pytest.mark.parametrize("test_case", TEST_CASES_LOG)
def test_log(capsys, test_case):
    max_log_level, log_cmd, exp_out, exp_err = test_case
    cmd.verbosity = max_log_level
    log_cmd(TEST_STR)
    captured = capsys.readouterr()
    stdout = captured.out
    stderr = captured.err
    assert stderr == exp_err
    assert stdout == exp_out


TEST_CASES_DIE = [
    # max_log_level, exp_out, exp_err, exp_exit, exp_exc
    (
        Verbosity.DBG_EXTREME,
        '',
        EXPECTED_LOG_FATAL_ERROR,
        None,
        RuntimeError('die with -vvv or more shows a stack trace. exit_code argument is ignored.'),
    ),
    (Verbosity.DBG_MORE, '', EXPECTED_LOG_FATAL_ERROR, SystemExit(1), None),
    (Verbosity.DBG, '', EXPECTED_LOG_FATAL_ERROR, SystemExit(1), None),
    (Verbosity.INF, '', EXPECTED_LOG_FATAL_ERROR, SystemExit(1), None),
    (Verbosity.WRN, '', EXPECTED_LOG_FATAL_ERROR, SystemExit(1), None),
    (Verbosity.ERR, '', EXPECTED_LOG_FATAL_ERROR, SystemExit(1), None),
    (Verbosity.QUIET, '', '', SystemExit(1), None),
]


@pytest.mark.parametrize("test_case", TEST_CASES_DIE)
def test_die(capsys, test_case):
    max_log_level, exp_out, exp_err, exp_exit, exp_exc = test_case
    cmd.verbosity = max_log_level

    if exp_exit:
        with pytest.raises(SystemExit) as exit_info:
            cmd.die(TEST_STR)
        assert exit_info.type is type(exp_exit)
        assert exit_info.value.code == exp_exit.code
    if exp_exc:
        with pytest.raises(Exception) as exc_info:
            cmd.die(TEST_STR)
        assert type(exc_info.value) is type(exp_exc)
        assert str(exc_info.value) == str(exp_exc)

    captured = capsys.readouterr()
    stdout = captured.out
    stderr = captured.err
    assert stderr == exp_err
    assert stdout == exp_out
