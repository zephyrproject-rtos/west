import os
import subprocess
import sys

import west.version

assert 'TOXTEMPDIR' in os.environ, "you must run these tests using tox"

def test_main():
    # A quick check that the package can be executed as a module which
    # takes arguments, using e.g. "python3 -m west --version" to
    # produce the same results as "west --version", and that both are
    # sane (i.e. the actual version number is printed instead of
    # simply an error message to stderr).

    output_as_module = subprocess.check_output([sys.executable, '-m', 'west',
                                                '--version']).decode()
    output_directly = subprocess.check_output(['west', '--version']).decode()
    assert west.version.__version__ in output_as_module
    assert output_as_module == output_directly
