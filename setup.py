# Copyright 2018 Open Source Foundries Limited.
#
# SPDX-License-Identifier: Apache-2.0

import setuptools

with open('README.rst', 'r') as f:
    long_description = f.read()

with open('requirements.txt', 'r') as f:
    install_requires = f.readlines()

with open('tests_requirements.txt', 'r') as f:
    tests_require = f.readlines()

setuptools.setup(
    name='west',
    version='0.1.0',
    author='Zephyr Project',
    author_email='devel@lists.zephyrproject.org',
    description='Zephyr RTOS Project meta-tool (wrapper and bootstrap)',
    long_description=long_description,
    # http://docutils.sourceforge.net/FAQ.html#what-s-the-official-mime-type-for-restructuredtext-data
    long_description_content_type="text/x-rst",
    url='https://github.com/zephyrproject-rtos/west',
    packages=setuptools.find_packages('src', include=('bootstrap',)),
    package_dir={'': 'src'},
    classifiers=[
        'Programming Language :: Python :: 3',
        'License :: OSI Approved :: Apache Software License',
        'Operating System :: POSIX :: Linux',
        'Operating System :: MacOS :: MacOS X',
        'Operating System :: Microsoft :: Windows',
        ],
    install_requires=install_requires,
    python_requires='>=3.4',
    tests_require=tests_require,
    setup_requires=('pytest-runner',),
    entry_points={
        'console_scripts': (
            'west = bootstrap.main:main',
            ),
        },
    )
