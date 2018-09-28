# Copyright (c) 2018 Foundries.io
#
# SPDX-License-Identifier: Apache-2.0

import argparse
from unittest.mock import patch, call

import pytest

from west.runners.nrfjprog import NrfJprogBinaryRunner
from conftest import RC_KERNEL_HEX


#
# Test values
#

TEST_DEF_SNR = 'test-default-serial-number'  # for mocking user input
TEST_OVR_SNR = 'test-override-serial-number'

#
# Expected results.
#
# This dictionary maps different configurations to the commands we expect to be
# executed for them. Verification is done by mocking the check_call() method,
# which is used to run the commands.
#
# The key naming scheme is <F><SR><SN><E>, where:
#
# - F: family, 1 for 'NRF51' or 2 for 'NRF52'
# - SR: soft reset, Y for yes, N for pin reset
# - SNR: serial number override, Y for yes, N for 'use default'
# - E: full chip erase, Y for yes, N for sector / sector and UICR only
#

EXPECTED_COMMANDS = {
    # NRF51:
    '1NNN':
    (['nrfjprog', '--program', RC_KERNEL_HEX, '-f', 'NRF51', '--snr', TEST_DEF_SNR, '--sectorerase'],  # noqa: E501
     ['nrfjprog', '--pinreset', '-f', 'NRF51', '--snr', TEST_DEF_SNR]),

    '1NNY':
    (['nrfjprog', '--eraseall', '-f', 'NRF51', '--snr', TEST_DEF_SNR],
     ['nrfjprog', '--program', RC_KERNEL_HEX, '-f', 'NRF51', '--snr', TEST_DEF_SNR],  # noqa: E501
     ['nrfjprog', '--pinreset', '-f', 'NRF51', '--snr', TEST_DEF_SNR]),

    '1NYN':
    (['nrfjprog', '--program', RC_KERNEL_HEX, '-f', 'NRF51', '--snr', TEST_OVR_SNR, '--sectorerase'],  # noqa: E501
     ['nrfjprog', '--pinreset', '-f', 'NRF51', '--snr', TEST_OVR_SNR]),

    '1NYY':
    (['nrfjprog', '--eraseall', '-f', 'NRF51', '--snr', TEST_OVR_SNR],
     ['nrfjprog', '--program', RC_KERNEL_HEX, '-f', 'NRF51', '--snr', TEST_OVR_SNR],  # noqa: E501
     ['nrfjprog', '--pinreset', '-f', 'NRF51', '--snr', TEST_OVR_SNR]),

    '1YNN':
    (['nrfjprog', '--program', RC_KERNEL_HEX, '-f', 'NRF51', '--snr', TEST_DEF_SNR, '--sectorerase'],  # noqa: E501
     ['nrfjprog', '--reset', '-f', 'NRF51', '--snr', TEST_DEF_SNR]),

    '1YNY':
    (['nrfjprog', '--eraseall', '-f', 'NRF51', '--snr', TEST_DEF_SNR],
     ['nrfjprog', '--program', RC_KERNEL_HEX, '-f', 'NRF51', '--snr', TEST_DEF_SNR],  # noqa: E501
     ['nrfjprog', '--reset', '-f', 'NRF51', '--snr', TEST_DEF_SNR]),

    '1YYN':
    (['nrfjprog', '--program', RC_KERNEL_HEX, '-f', 'NRF51', '--snr', TEST_OVR_SNR, '--sectorerase'],  # noqa: E501
     ['nrfjprog', '--reset', '-f', 'NRF51', '--snr', TEST_OVR_SNR]),

    '1YYY':
    (['nrfjprog', '--eraseall', '-f', 'NRF51', '--snr', TEST_OVR_SNR],
     ['nrfjprog', '--program', RC_KERNEL_HEX, '-f', 'NRF51', '--snr', TEST_OVR_SNR],  # noqa: E501
     ['nrfjprog', '--reset', '-f', 'NRF51', '--snr', TEST_OVR_SNR]),

    # NRF52:
    '2NNN':
    (['nrfjprog', '--program', RC_KERNEL_HEX, '-f', 'NRF52', '--snr', TEST_DEF_SNR, '--sectoranduicrerase'],  # noqa: E501
     ['nrfjprog', '--pinresetenable', '-f', 'NRF52', '--snr', TEST_DEF_SNR],
     ['nrfjprog', '--pinreset', '-f', 'NRF52', '--snr', TEST_DEF_SNR]),

    '2NNY':
    (['nrfjprog', '--eraseall', '-f', 'NRF52', '--snr', TEST_DEF_SNR],
     ['nrfjprog', '--program', RC_KERNEL_HEX, '-f', 'NRF52', '--snr', TEST_DEF_SNR],  # noqa: E501
     ['nrfjprog', '--pinresetenable', '-f', 'NRF52', '--snr', TEST_DEF_SNR],
     ['nrfjprog', '--pinreset', '-f', 'NRF52', '--snr', TEST_DEF_SNR]),

    '2NYN':
    (['nrfjprog', '--program', RC_KERNEL_HEX, '-f', 'NRF52', '--snr', TEST_OVR_SNR, '--sectoranduicrerase'],  # noqa: E501
     ['nrfjprog', '--pinresetenable', '-f', 'NRF52', '--snr', TEST_OVR_SNR],
     ['nrfjprog', '--pinreset', '-f', 'NRF52', '--snr', TEST_OVR_SNR]),

    '2NYY':
    (['nrfjprog', '--eraseall', '-f', 'NRF52', '--snr', TEST_OVR_SNR],
     ['nrfjprog', '--program', RC_KERNEL_HEX, '-f', 'NRF52', '--snr', TEST_OVR_SNR],  # noqa: E501
     ['nrfjprog', '--pinresetenable', '-f', 'NRF52', '--snr', TEST_OVR_SNR],
     ['nrfjprog', '--pinreset', '-f', 'NRF52', '--snr', TEST_OVR_SNR]),

    '2YNN':
    (['nrfjprog', '--program', RC_KERNEL_HEX, '-f', 'NRF52', '--snr', TEST_DEF_SNR, '--sectoranduicrerase'],  # noqa: E501
     ['nrfjprog', '--reset', '-f', 'NRF52', '--snr', TEST_DEF_SNR]),

    '2YNY':
    (['nrfjprog', '--eraseall', '-f', 'NRF52', '--snr', TEST_DEF_SNR],
     ['nrfjprog', '--program', RC_KERNEL_HEX, '-f', 'NRF52', '--snr', TEST_DEF_SNR],  # noqa: E501
     ['nrfjprog', '--reset', '-f', 'NRF52', '--snr', TEST_DEF_SNR]),

    '2YYN':
    (['nrfjprog', '--program', RC_KERNEL_HEX, '-f', 'NRF52', '--snr', TEST_OVR_SNR, '--sectoranduicrerase'],  # noqa: E501
     ['nrfjprog', '--reset', '-f', 'NRF52', '--snr', TEST_OVR_SNR]),

    '2YYY':
    (['nrfjprog', '--eraseall', '-f', 'NRF52', '--snr', TEST_OVR_SNR],
     ['nrfjprog', '--program', RC_KERNEL_HEX, '-f', 'NRF52', '--snr', TEST_OVR_SNR],  # noqa: E501
     ['nrfjprog', '--reset', '-f', 'NRF52', '--snr', TEST_OVR_SNR]),
}


def expected_commands(family, softreset, snr, erase):
    '''Expected NrfJprogBinaryRunner results given parameters.

    Returns a factory function which expects the following arguments:

    - family: string, 'NRF51' or 'NRF52'
    - softreset: boolean, controls whether soft reset is performed
    - snr: string serial number of board, or None
    - erase: boolean, whether to do a full chip erase or not
    '''
    expected_key = '{}{}{}{}'.format(
        '1' if family == 'NRF51' else '2',
        'Y' if softreset else 'N',
        'Y' if snr else 'N',
        'Y' if erase else 'N')

    return EXPECTED_COMMANDS[expected_key]


#
# Test cases
#

TEST_CASES = [(f, sr, snr, e)
              for f in ('NRF51', 'NRF52')
              for sr in (False, True)
              for snr in (TEST_OVR_SNR, None)
              for e in (False, True)]


def get_board_snr_patch():
    return TEST_DEF_SNR


def id_fn(test_case):
    ret = ''
    for x in test_case:
        if x in ('NRF51', 'NRF52'):
            ret += x[-1:]
        else:
            ret += 'Y' if x else 'N'
    return ret


@pytest.mark.parametrize('test_case', TEST_CASES, ids=id_fn)
@patch('west.runners.nrfjprog.NrfJprogBinaryRunner.get_board_snr_from_user',
       side_effect=get_board_snr_patch)
@patch('west.runners.nrfjprog.NrfJprogBinaryRunner.check_call')
def test_nrfjprog_init(cc, get_snr, test_case, runner_config):
    family, softreset, snr, erase = test_case

    runner = NrfJprogBinaryRunner(runner_config, family, softreset, snr,
                                  erase=erase)
    runner.run('flash')

    assert cc.call_args_list == [call(x) for x in
                                 expected_commands(*test_case)]
    if snr is None:
        get_snr.assert_called_once_with()
    else:
        get_snr.assert_not_called()


@pytest.mark.parametrize('test_case', TEST_CASES, ids=id_fn)
@patch('west.runners.nrfjprog.NrfJprogBinaryRunner.get_board_snr_from_user',
       side_effect=get_board_snr_patch)
@patch('west.runners.nrfjprog.NrfJprogBinaryRunner.check_call')
def test_nrfjprog_create(cc, get_snr, test_case, runner_config):
    family, softreset, snr, erase = test_case

    args = ['--nrf-family', family]
    if softreset:
        args.append('--softreset')
    if snr is not None:
        args.extend(['--snr', snr])
    if erase:
        args.append('--erase')

    parser = argparse.ArgumentParser()
    NrfJprogBinaryRunner.add_parser(parser)
    arg_namespace = parser.parse_args(args)
    runner = NrfJprogBinaryRunner.create(runner_config, arg_namespace)
    runner.run('flash')

    assert cc.call_args_list == [call(x) for x in
                                 expected_commands(*test_case)]
    if snr is None:
        get_snr.assert_called_once_with()
    else:
        get_snr.assert_not_called()
