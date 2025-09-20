# Copyright (c) 2021, Nordic Semiconductor ASA

from west.commands import WestCommand

gv = WestCommand._parse_git_version


def test_parse_git_version():
    # White box test for git parsing behavior.
    assert gv(b'git version 2.25.1\n') == (2, 25, 1)
    assert gv(b'git version 2.28.0.windows.1\n') == (2, 28, 0)
    assert gv(b'git version 2.24.3 (Apple Git-128)\n') == (2, 24, 3)
    assert gv(b'git version 2.29.GIT\n') == (2, 29)
    assert gv(b'not a git version') is None
