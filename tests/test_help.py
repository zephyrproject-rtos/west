# Copyright (c) 2020, Nordic Semiconductor ASA

import itertools

from conftest import cmd

from west.app.main import BUILTIN_COMMAND_GROUPS


def test_builtin_help_and_dash_h(west_init_tmpdir):
    # Test "west help" and "west -h" are the same for built-in
    # functionality.

    h1out = cmd('help')
    h2out = cmd('-h')
    assert h1out == h2out

    for cls in itertools.chain(*BUILTIN_COMMAND_GROUPS.values()):
        c = cls()
        h1out = cmd(f'help {c.name}')
        h2out = cmd(f'{c.name} -h')
        assert h1out == h2out

def test_extension_help_and_dash_h(west_init_tmpdir):
    # Test "west help <command>" and "west <command> -h" for extension
    # commands (west_init_tmpdir has a command with one).

    cmd('update')
    ext1out = cmd('help test-extension')
    ext2out = cmd('test-extension -h')

    expected = EXTENSION_EXPECTED
    assert ext1out == ext2out
    assert ext1out in expected

# argparse changed its behavior at some point; patch over that here.
EXTENSION_EXPECTED = ['''\
usage: west test-extension [-h]

optional arguments:
  -h, --help  show this help message and exit
''', '''\
usage: west test-extension [-h]

options:
  -h, --help  show this help message and exit
''']
