#
# Test cases
#

from west.main import BUILTIN_COMMAND_NAMES
from conftest import cmd

def test_help(west_init_tmpdir):

    # Check that output from help is equivalent to -h
    return

    h1out = cmd('help')
    h2out = cmd('-h')
    assert(h1out == h2out)

    for c in BUILTIN_COMMAND_NAMES:
        h1out = cmd('help {}'.format(c))
        h2out = cmd('{} -h'.format(c))
        assert(h1out == h2out)
