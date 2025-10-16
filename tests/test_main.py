from conftest import cmd, cmd_subprocess

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
