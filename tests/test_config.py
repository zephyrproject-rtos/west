# Copyright (c) 2019, Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

#
# Test cases
#
# For some tests cases, the environment setting HOME must be redirected to a
# tmp folder.
# This is configured in tox.ini to ensure tests can run without modifying the
# settings of the calling user.
# If running this test directly using pytest, those tests will be skipped.
#

import os
import subprocess

import pytest

from west import configuration as config
from west.util import canon_path

from conftest import cmd


# We skip this test if executed directly using pytest, to avoid modifying
# user's real ~/.westconfig.
# We want to ensure HOME is pointing inside TOX temp dir before continuing.
@pytest.mark.skipif(os.environ.get('TOXTEMPDIR') is None,
                    reason="This test requires to be executed using tox")
def test_config_global(west_init_tmpdir):
    if not os.path.exists(os.path.expanduser('~')):
        os.mkdir(os.path.expanduser('~'))

    # To ensure that GLOBAL home folder points into tox temp dir.
    # Otherwise fail the test, as we don't want to risk manipulating user's
    # west config
    assert canon_path(config.ConfigFile.GLOBAL.value) == \
        canon_path(os.path.join(os.environ.get('TOXTEMPDIR'),
                                'pytest-home', '.westconfig'))

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

def test_config_local(west_init_tmpdir):
    if not os.path.exists(os.path.expanduser('~')):
        os.mkdir(os.path.expanduser('~'))

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

# We skip this test if executed directly using pytest, to avoid modifying
# user's real ~/.westconfig.
# We want to ensure HOME is pointing inside TOX temp dir before continuing.
@pytest.mark.skipif(os.environ.get('TOXTEMPDIR') is None,
                    reason="This test requires to be executed using tox")
def test_config_precedence(west_init_tmpdir):
    if not os.path.exists(os.path.expanduser('~')):
        os.mkdir(os.path.expanduser('~'))

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


def test_config_missing_key(west_init_tmpdir):
    if not os.path.exists(os.path.expanduser('~')):
        os.mkdir(os.path.expanduser('~'))

    with pytest.raises(subprocess.CalledProcessError) as e:
        cmd('config pytest')
        assert str(e) == 'west config: error: missing key, please invoke ' \
            'as: west config <section>.<key>\n'
