# Copyright 2018 Open Source Foundries Limited.
#
# SPDX-License-Identifier: Apache-2.0

import setuptools

with open('README.rst', 'r') as f:
    long_description = f.read()

with open('src/west/version.py', 'r') as f:
    exec(f.read())

setuptools.setup(
    name='west',
    version=__version__,
    author='Zephyr Project',
    author_email='devel@lists.zephyrproject.org',
    description='Zephyr RTOS Project meta-tool',
    long_description=long_description,
    # http://docutils.sourceforge.net/FAQ.html#what-s-the-official-mime-type-for-restructuredtext-data
    long_description_content_type="text/x-rst",
    url='https://github.com/zephyrproject-rtos/west',
    packages=setuptools.find_namespace_packages(where='src'),
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
        'PyYAML',
        'pykwalify',
        'configobj',
        'setuptools>=v40.1.0',  # for find_namespace_packages
    ],
    python_requires='>=3.4',
    entry_points={'console_scripts': ('west = west.main:main',)},
)
