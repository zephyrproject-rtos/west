.. image:: https://img.shields.io/pypi/pyversions/west?logo=python
   :target: https://pypi.org/project/west/

.. image:: https://api.securityscorecards.dev/projects/github.com/zephyrproject-rtos/west/badge
   :target: https://scorecard.dev/viewer/?uri=github.com/zephyrproject-rtos/west

.. image:: https://codecov.io/gh/zephyrproject-rtos/west/graph/badge.svg
   :target: https://codecov.io/gh/zephyrproject-rtos/west

This is the Zephyr RTOS meta tool, ``west``.

https://docs.zephyrproject.org/latest/guides/west/index.html

Installation
------------

Using pip::

  pip3 install west

(Use ``pip3 uninstall west`` to uninstall it.)

Basic Usage
-----------

West lets you manage multiple Git repositories under a single directory using a
single file, called the *west manifest file*, or *manifest* for short.
By default the manifest file is named ``west.yml``.
You use ``west init`` to set up this directory, then ``west update`` to fetch
and/or update the repositories named in the manifest.

By default, west uses `upstream Zephyr's manifest file
<https://github.com/zephyrproject-rtos/zephyr/blob/main/west.yml>`_, but west
doesn't care if the manifest repository is zephyr or not. You can and are
encouraged to make your own manifest repositories to meet your needs.

For more details, see the `West guide
<https://docs.zephyrproject.org/latest/guides/west/index.html>`_ in the Zephyr
documentation.

Example usage using the upstream manifest file::

  mkdir zephyrproject && cd zephyrproject
  west init
  west update

What just happened:

- ``west init`` clones the upstream *west manifest* repository, which in this
  case is the zephyr repository. The manifest repository contains ``west.yml``,
  a YAML description of the Zephyr installation, including Git repositories and
  other metadata.

- ``west update`` clones the other repositories named in the manifest file,
  creating working trees in the installation directory ``zephyrproject``.

Use ``west init -m`` to specify another manifest repository. Use ``--mr`` to
use a revision to inialize from; if not given, the remote's default branch is used.
Use ``--mf`` to use a manifest file other than ``west.yml``.

Additional Commands
-------------------

West has multiple sub-commands. After running ``west init``, you can
run them from anywhere under ``zephyrproject``.

For a list of available commands, run ``west -h``. Get help on a
command with ``west <command> -h``.

West is extensible: you can add new commands to west without modifying its
source code. See `Extensions
<https://docs.zephyrproject.org/latest/guides/west/extensions.html>`_ in the
documentation for details.


Hacking on West
---------------

This section contains notes for getting started developing west itself.

`pip` offers many different ways to (download and) install Python
software. Below are common ways relevant to `west`; for a complete list
check `<https://pip.pypa.io/en/stable/topics/>`_

Quick Install
~~~~~~~~~~~~~

If you are not interested in source code and git history, GitHub offers
`pip` the ability to download and install a .zip archive of any random
version with a single command.  This allows testing work in progress
very quickly. Examples::

  pip3 uninstall west
  # Pull request 830
  pip3 install --dry-run https://github.com/zephyrproject-rtos/west/archive/pull/830/head.zip
  # Release v1.4
  pip3 install https://github.com/zephyrproject-rtos/west/archive/v1.4-branch.zip
  # Someone's random version
  pip3 install https://github.com/someone_you_trust/west/archive/some_branch_or_tag.zip

Warning: never install software from people or locations you do not trust!

Editable Install
~~~~~~~~~~~~~~~~

To run west "live" from the current source code tree, run this command from the
top level directory in the west repository::

  pip3 install -e .

This is useful if you are actively working on west and don't want to re-package
and install a wheel each time you run it.

Creating a wheel package
~~~~~~~~~~~~~~~~~~~~~~~~

You can create a wheel package and distribute it to others.

To build the west wheel file::

  # Using uv
  uv build

  # Without uv
  pip3 install --upgrade build
  python -m build

This will create a file named ``dist/west-x.y.z-py3-none-any.whl``,
where ``x.y.z`` is the current version in setup.py.

To install the wheel::

  pip3 install -U dist/west-x.y.z-py3-none-any.whl

You can ``pip3 uninstall west`` to remove this wheel before re-installing the
version from PyPI, etc.

Running the Tests
~~~~~~~~~~~~~~~~~

First, install the dependencies::

  # Using uv
  uv sync --frozen

  # Using pip (requires v25.1 or newer)
  # Recommended in an active virtual environment
  pip3 install --group dev

Then, run the test suite locally from the top level directory::

  # Using uv
  uv run poe all

  # Using poe
  # Recommended in an active virtual environment
  poe all

  # Manually (test the installed west version)
  pytest

  # Manually (test the local copy)
  pytest -o pythonpath=src

The ``all`` target from ``poe`` runs multiple tasks sequentially. Run ``poe -h``
to get the list of configured tasks.
You can pass arguments to the task running ``poe``. This is especially useful
on specific tests and save time. Examples::

  # Run a subset of tests
  poe test tests/test_project.py

  # Run the ``test_update_narrow()`` code with ``pdb`` (but _not_ the
  # west code which is running in subprocesses)
  poe test --exitfirst --trace -k test_update_narrow

  # Run all tests with "import" in their name and let them log to the
  # current terminal
  poe test -k import --capture=no
