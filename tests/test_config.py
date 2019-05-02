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
import pytest
import subprocess
from west import configuration as config
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
    assert config.ConfigFile.GLOBAL.value == \
        os.path.join(os.environ.get('TOXTEMPDIR'), 'pytest-home/.westconfig')

    testkey_value = cmd('config pytest.testkey_global')
    assert testkey_value == ''

    # Set value in user's testing home
    cmd('config --global pytest.testkey_global ' + str(west_init_tmpdir))

    # Readback from --local, to ensure that is empty.
    testkey_value = cmd('config --local pytest.testkey_global')
    assert testkey_value == ''

    # Readback from --system, to ensure that is empty.
    testkey_value = cmd('config --system pytest.testkey_global')
    assert testkey_value == ''

    # Readback from --global, and compare the value with the expected.
    testkey_value = cmd('config --global pytest.testkey_global')
    assert testkey_value.strip('\n') == str(west_init_tmpdir)

    # Readback in from all files should also provide the value.
    testkey_value = cmd('config pytest.testkey_global')
    assert testkey_value.strip('\n') == str(west_init_tmpdir)


def test_config_local(west_init_tmpdir):
    if not os.path.exists(os.path.expanduser('~')):
        os.mkdir(os.path.expanduser('~'))

    test_value = str(west_init_tmpdir) + '_local'

    testkey_value = cmd('config pytest.testkey_local')
    assert testkey_value == ''

    # Set value in project local
    cmd('config --local pytest.testkey_local ' + test_value)

    # Readback from --global, to ensure that is empty.
    testkey_value = cmd('config --global pytest.testkey_local')
    assert testkey_value == ''

    # Readback from --local, and compare the value with the expected.
    testkey_value = cmd('config --local pytest.testkey_local')
    assert testkey_value.strip('\n') == test_value

    # Readback in from all files should also provide the value.
    testkey_value = cmd('config pytest.testkey_local')
    assert testkey_value.strip('\n') == test_value

    # Update the value in user's testing home without --local and see it's
    # default to --local when reading back
    test_value_update = str(west_init_tmpdir) + '_update'
    cmd('config pytest.testkey_local ' + test_value_update)

    # Readback from --global, to ensure that is empty.
    testkey_value = cmd('config --global pytest.testkey_local')
    assert testkey_value == ''

    # Readback from --local, and compare the value with the expected.
    testkey_value = cmd('config --local pytest.testkey_local')
    assert testkey_value.strip('\n') == test_value_update

    # Readback in from all files should also provide the value.
    testkey_value = cmd('config pytest.testkey_local')
    assert testkey_value.strip('\n') == test_value_update


# We skip this test if executed directly using pytest, to avoid modifying
# user's real ~/.westconfig.
# We want to ensure HOME is pointing inside TOX temp dir before continuing.
@pytest.mark.skipif(os.environ.get('TOXTEMPDIR') is None,
                    reason="This test requires to be executed using tox")
def test_config_precendence(west_init_tmpdir):
    if not os.path.exists(os.path.expanduser('~')):
        os.mkdir(os.path.expanduser('~'))

    test_value_local = str(west_init_tmpdir) + '_precedence'
    test_value_global = str(west_init_tmpdir) + '_global'

    testkey_value = cmd('config pytest.testkey_precedence')
    assert testkey_value == ''

    # Set value in user's testing home
    cmd('config --global pytest.testkey_precedence ' + test_value_global)

    # Readback from --global, to verify it is set.
    testkey_value = cmd('config --global pytest.testkey_precedence')
    assert testkey_value.strip('\n') == test_value_global

    # Readback from --local, to ensure it is not available using --local
    testkey_value = cmd('config --local pytest.testkey_precedence')
    assert testkey_value.strip('\n') == ''

    # Set value in project local and verify it can be read back.
    cmd('config --local pytest.testkey_precedence ' + test_value_local)
    testkey_value = cmd('config --local pytest.testkey_precedence')
    assert testkey_value.strip('\n') == test_value_local

    # Readback without specifying --local or --global and see that project
    # specific value takes precedence.
    testkey_value = cmd('config pytest.testkey_precedence')
    assert testkey_value.strip('\n') == test_value_local

    # Make additional verification that --global is still available.
    testkey_value = cmd('config --global pytest.testkey_precedence')
    assert testkey_value.strip('\n') == test_value_global


def test_config_missing_key(west_init_tmpdir):
    if not os.path.exists(os.path.expanduser('~')):
        os.mkdir(os.path.expanduser('~'))

    with pytest.raises(subprocess.CalledProcessError) as e:
        cmd('config pytest')
        assert str(e) == 'west config: error: missing key, please invoke ' \
            'as: west config <section>.<key>\n'
