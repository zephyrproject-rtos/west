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

Running the Tests
-----------------

First, install tox::

  # macOS, Windows
  pip3 install tox

  # Linux
  pip3 install --user tox

Then, run the test suite locally from the top level directory::

  tox

You can use ``--`` to tell tox to pass arguments to ``pytest``. This is
especially useful to focus on specific tests and save time. Examples::

  # Run a subset of tests
  tox  --  tests/test_project.py

  # Debug the ``test_update_narrow()`` code with ``pdb`` (but _not_ the
  # west code which is running in subprocesses)
  tox  --  --verbose --exitfirst --trace -k test_update_narrow

  # Run all tests with "import" in their name and let them log to the
  # current terminal
  tox  --  -v -k import --capture=no

The tests cannot be run with ``pytest`` directly, they require the tox
environment.

See the tox configuration file, tox.ini, for more details.

Hacking on West
---------------

This section contains notes for getting started developing west itself.

Editable Install
~~~~~~~~~~~~~~~~

To run west "live" from the current source code tree, run this command from the
top level directory in the west repository::

  pip3 install -e .

This is useful if you are actively working on west and don't want to re-package
and install a wheel each time you run it.

Installing from Source
~~~~~~~~~~~~~~~~~~~~~~

You can create and install a wheel package to install west as well.

To build the west wheel file::

  pip3 install --upgrade build
  python -m build

This will create a file named ``dist/west-x.y.z-py3-none-any.whl``,
where ``x.y.z`` is the current version in setup.py.

To install the wheel::

  pip3 install -U dist/west-x.y.z-py3-none-any.whl

You can ``pip3 uninstall west`` to remove this wheel before re-installing the
version from PyPI, etc.
