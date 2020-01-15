# Copyright 2018 (c) Foundries.io.
#
# SPDX-License-Identifier: Apache-2.0

'''Deprecated; do not use.

Preserved for Zephyr v1.14 LTS compatibility only. This should never
have been part of west, and will be removed when Zephyr v1.14 is
obsoleted.
'''

import warnings

from west import cmake
from west import log

# This has no effect by default from 'west build' unless explicitly
# enabled, e.g. with PYTHONWARNINGS.
warnings.warn(
    'west.build was deprecated after west v0.6, and will be removed after '
    'Zephyr v1.14 is obsoleted',
    DeprecationWarning)

DEFAULT_BUILD_DIR = 'build'
'''Name of the default Zephyr build directory.'''

DEFAULT_CMAKE_GENERATOR = 'Ninja'
'''Name of the default CMake generator.'''

def is_zephyr_build(path):
    '''Return true if and only if `path` appears to be a valid Zephyr
    build directory.

    "Valid" means the given path is a directory which contains a CMake
    cache with a 'ZEPHYR_TOOLCHAIN_VARIANT' key.
    '''
    try:
        cache = cmake.CMakeCache.from_build_dir(path)
    except FileNotFoundError:
        cache = {}

    if 'ZEPHYR_TOOLCHAIN_VARIANT' in cache:
        log.dbg(f'{path} is a zephyr build directory',
                level=log.VERBOSE_EXTREME)
        return True
    else:
        log.dbg(f'{path} is NOT a valid zephyr build directory',
                level=log.VERBOSE_EXTREME)
        return False
