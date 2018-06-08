# Copyright 2018 Open Source Foundries Limited.
#
# SPDX-License-Identifier: Apache-2.0

import setuptools

with open('README.rst', 'r') as f:
    long_description = f.read()

setuptools.setup(
    name='west',
    version='0.2.5',
    author='Zephyr Project',
    author_email='devel@lists.zephyrproject.org',
    description='Zephyr RTOS Project meta-tool (wrapper and bootstrap)',
    long_description=long_description,
    # http://docutils.sourceforge.net/FAQ.html#what-s-the-official-mime-type-for-restructuredtext-data
    long_description_content_type="text/x-rst",
    url='https://github.com/zephyrproject-rtos/west',
    packages=setuptools.find_packages('bootstrap'),
    classifiers=(
        'Programming Language :: Python :: 3',
        'License :: OSI Approved :: Apache Software License',
        'Operating System :: POSIX :: Linux',
        'Operating System :: MacOS :: MacOS X',
        'Operating System :: Microsoft :: Windows',
        ),
    # Note: the bootstrap script only depends on the standard library;
    #       these dependencies are for West itself.
    install_requires=(
        'PyYAML',
        ),
    python_requires='>=3.4',
    entry_points={
        'console_scripts': (
            'west = bootstrap.main:main',
            ),
        },
    )
