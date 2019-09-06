#
# Test cases
#

from west.main import BUILTIN_COMMAND_NAMES
from conftest import cmd

def test_builtin_help_and_dash_h(west_init_tmpdir):
    # Test "west help" and "west -h" are the same for built-in
    # functionality.

    h1out = cmd('help')
    h2out = cmd('-h')
    assert h1out == h2out

    # Test "west help <command>" and "west <command> -h" for built-in
    # commands.
    for c in BUILTIN_COMMAND_NAMES:
        h1out = cmd('help {}'.format(c))
        h2out = cmd('{} -h'.format(c))
        assert h1out == h2out

def test_extension_help_and_dash_h(west_init_tmpdir):
    # Test "west help <command>" and "west <command> -h" for extension
    # commands (west_init_tmpdir has a command with one).

    cmd('update')
    ext1out = cmd('help test-extension')
    ext2out = cmd('test-extension -h')
    assert ext1out == ext2out
    assert ext1out == EXTENSION_EXPECTED


EXTENSION_EXPECTED = '''\
usage: west test-extension [-h]

optional arguments:
  -h, --help  show this help message and exit

'''
