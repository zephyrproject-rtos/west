# Copyright (c) 2019, Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

import configparser
import os
import subprocess

import pytest

from west import configuration as config
from west.util import canon_path

from conftest import cmd

@pytest.fixture
def config_tmpdir(tmpdir):
    # Fixture for running from a temporary directory with a .west
    # inside. We also:
    #
    # - ensure we're being run under tox, to avoid messing with
    #   the user's actual configuration files
    # - ensure configuration files point where they should inside
    #   the temporary tox directories, for the same reason
    # - ensure ~ exists (since the test environment
    #   doesn't let us run in the true user $HOME).
    # - set ZEPHYR_BASE (to avoid complaints in subcommand stderr)
    #
    # Using this makes the tests run faster than if we used
    # west_init_tmpdir from conftest.py, and also ensures that the
    # configuration code doesn't depend on features like the existence
    # of a manifest file, helping separate concerns.
    assert 'TOXTEMPDIR' in os.environ, 'you must run tests using tox'
    assert canon_path(config.ConfigFile.GLOBAL.value) == \
        canon_path(os.path.join(os.environ.get('TOXTEMPDIR'),
                                'pytest-home', '.westconfig'))
    if not os.path.exists(os.path.expanduser('~')):
        os.mkdir(os.path.expanduser('~'))
    os.environ['ZEPHYR_BASE'] = str(tmpdir.join('no-zephyr-here'))
    tmpdir.mkdir('.west')
    tmpdir.chdir()

def cfg_obj():
    return configparser.ConfigParser(allow_no_value=True)


def test_config_global(config_tmpdir):
    # Make sure the value is currently unset.
    testkey_value = cmd('config pytest.testkey_global')
    assert testkey_value == ''

    # Set value globally.
    cmd('config --global pytest.testkey_global foo')

    # Read from --local, to ensure that is empty.
    testkey_value = cmd('config --local pytest.testkey_global')
    assert testkey_value == ''

    # Read from --system, to ensure that is empty.
    testkey_value = cmd('config --system pytest.testkey_global')
    assert testkey_value == ''

    # Read from --global, and check the value.
    testkey_value = cmd('config --global pytest.testkey_global')
    assert testkey_value.rstrip() == 'foo'

    # Without an explicit config source, the global value (the only
    # one set) should be returned.
    testkey_value = cmd('config pytest.testkey_global')
    assert testkey_value.rstrip() == 'foo'

def test_config_local(config_tmpdir):
    testkey_value = cmd('config pytest.testkey_local')
    assert testkey_value == ''

    # Set a config option in the installation.
    cmd('config --local pytest.testkey_local foo')

    # It should not be available in the global or system files.
    testkey_value = cmd('config --global pytest.testkey_local')
    assert testkey_value == ''
    testkey_value = cmd('config --system pytest.testkey_local')
    assert testkey_value == ''

    # It should be available with --local.
    testkey_value = cmd('config --local pytest.testkey_local')
    assert testkey_value.rstrip() == 'foo'

    # Without an explicit config source, the local value (the only one
    # set) should be returned.
    testkey_value = cmd('config pytest.testkey_local')
    assert testkey_value.rstrip() == 'foo'

    # Update the value without a config destination, which should
    # default to --local.
    cmd('config pytest.testkey_local foo2')

    # The --global and --system settings should still be empty.
    testkey_value = cmd('config --global pytest.testkey_local')
    assert testkey_value == ''
    testkey_value = cmd('config --system pytest.testkey_local')
    assert testkey_value == ''

    # Read from --local, and check the value.
    testkey_value = cmd('config --local pytest.testkey_local')
    assert testkey_value.rstrip() == 'foo2'

    # Without an explicit config source, the local value (the only one
    # set) should be returned.
    testkey_value = cmd('config pytest.testkey_local')
    assert testkey_value.rstrip() == 'foo2'

def test_config_precedence(config_tmpdir):
    # Make sure the value is not set.
    testkey_value = cmd('config pytest.testkey_precedence')
    assert testkey_value == ''

    # Set value globally and verify it is set.
    cmd('config --global pytest.testkey_precedence foo_global')
    testkey_value = cmd('config --global pytest.testkey_precedence')
    assert testkey_value.rstrip() == 'foo_global'

    # Read with --local and ensure it is not set.
    testkey_value = cmd('config --local pytest.testkey_precedence')
    assert testkey_value.rstrip() == ''

    # Set with --local and verify it is set.
    cmd('config --local pytest.testkey_precedence foo_local')
    testkey_value = cmd('config --local pytest.testkey_precedence')
    assert testkey_value.rstrip() == 'foo_local'

    # Read without specifying --local or --global and verify that
    # --local takes precedence.
    testkey_value = cmd('config pytest.testkey_precedence')
    assert testkey_value.rstrip() == 'foo_local'

    # Make sure the --global value is still available.
    testkey_value = cmd('config --global pytest.testkey_precedence')
    assert testkey_value.rstrip() == 'foo_global'

def test_config_missing_key(config_tmpdir):
    with pytest.raises(subprocess.CalledProcessError) as e:
        cmd('config pytest')
        assert str(e) == 'west config: error: missing key, please invoke ' \
            'as: west config <section>.<key>\n'
