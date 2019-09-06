#
# Test cases
#

from west.main import BUILTIN_COMMAND_NAMES
from conftest import cmd

def test_help_and_dash_h(west_init_tmpdir):
    # Test "west help" and "west -h" are the same.

    h1out = cmd('help')
    h2out = cmd('-h')
    assert h1out == h2out

    # Test "west help <command>" and "west <command> -h" for built-in
    # commands.
    for c in BUILTIN_COMMAND_NAMES:
        h1out = cmd('help {}'.format(c))
        h2out = cmd('{} -h'.format(c))
        assert h1out == h2out
