# Copyright 2018 Open Source Foundries Limited.
# Copyright (c) 2020, Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

import os

import setuptools

with open('README.rst', 'r') as f:
    long_description = f.read()

with open('src/west/version.py', 'r') as f:
    __version__ = None
    exec(f.read())
    assert __version__ is not None

version = os.environ.get('WEST_VERSION', __version__)

setuptools.setup(
    name='west',
    version=version,
    author='Zephyr Project',
    author_email='devel@lists.zephyrproject.org',
    description='Zephyr RTOS Project meta-tool',
    long_description=long_description,
    # http://docutils.sourceforge.net/FAQ.html#what-s-the-official-mime-type-for-restructuredtext-data
    long_description_content_type="text/x-rst",
    url='https://github.com/zephyrproject-rtos/west',
    packages=setuptools.find_packages(where='src'),
    package_dir={'': 'src'},
    include_package_data=True,
    classifiers=[
        'Programming Language :: Python :: 3',
        'License :: OSI Approved :: Apache Software License',
        'Operating System :: POSIX :: Linux',
        'Operating System :: MacOS :: MacOS X',
        'Operating System :: Microsoft :: Windows',
    ],
    install_requires=[
        'colorama',
        'PyYAML>=5.1',
        'pykwalify',
        'configobj',
        'setuptools',
        'packaging',
    ],
    python_requires='>=3.6',
    entry_points={'console_scripts': ('west = west.app.main:main',)},
)
