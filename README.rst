This is the Zephyr RTOS meta tool, ``west``.

For more information about west, see:

https://docs.zephyrproject.org/latest/west/index.html

Installation
------------

Install west's bootstrapper with pip::

  pip3 install west==0.3.0rc1

Then install the rest of west and a Zephyr development environment in
a directory of your choosing::

  mkdir zephyrproject && cd zephyrproject
  west init
  west fetch

What just happened:

- ``west init`` runs the bootstrapper, which clones the west source
  repository and a *west manifest* repository. The manifest contains a
  YAML description of the Zephyr installation, including Git
  repositories and other metadata. The ``init`` command is the only
  one supported by the bootstrapper itself; all other commands are
  implemented in the west source repository it clones.

- ``west fetch`` clones the repositories in the manifest, creating
  working trees in the installation directory. In this case, the
  bootstrapper notices the command (``fetch``) is not ``init``, and
  delegates handling to the "main" west implementation in the source
  repository it cloned in the previous step.

(For those familiar with it, this is similar to how Android's Repo
tool works.)

Command auto-completion for Bash
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``scripts/west-completion.bash`` script adds auto-completion for West
subcommands and flags. See the top of file for installation instructions.

Usage
-----

West has multiple sub-commands. After running ``west init``, you can
run them from anywhere under ``zephyrproject``.

For a list of available commands, run ``west -h``. Get help on a
command with ``west <command> -h``. For example::

  $ west -h
  usage: west [-h] [-z ZEPHYR_BASE] [-v]
              {build,flash,debug,debugserver,attach,list-projects,fetch,pull,rebase,branch,checkout,diff,status,forall}
              ...
  [snip]
  $ west flash -h
  usage: west flash [-h] [-H] [-d BUILD_DIR] ...
  [snip]

Test Suite
----------

To run the test suite, run this from the west repository::

  pip3 install -r tests_requirements.txt

Then, in a Bash shell::

  PYTHONPATH=src py.test

On Windows::

  cmd /C "set PYTHONPATH=/path/to/west/src && py.test"

Hacking on West
---------------

West is distributed as two Python packages:

1. A ``bootstrap`` package, which is distributed via PyPI. Running
   ``pip3 install west`` installs this **bootstrapper package only**.
2. The "main" ``west`` package, which is fetched by the bootstrapper
   when ``west init`` is run.

This somewhat unusual arrangement is because:

- One of west's jobs is to manage a Zephyr installation's Git
  repositories, including its own.
- It allows easy customization of the version of west that's shipped
  with non-upstream distributions of Zephyr.
- West is experimental and is not stable. Users need to stay in sync
  with upstream, and this allows west to automatically update itself.

Using a Custom "Main" West
~~~~~~~~~~~~~~~~~~~~~~~~~~

To initialize west from a non-default location::

  west init -w https://example.com/your-west-repository.git

You can also add ``--west-rev some-branch`` to use ``some-branch``
instead of ``master``.

To use another manifest repository (optionally with ``--mr
some-manifest-branch``)::

  west init -m https://example.com/your-manifest-repository.git

After ``init`` time, you can hack on the west tree in ``zephyrproject``.

Using a Custom West Bootstrapper
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

To package and install the west bootstrapper from a west repository
checkout, `wheel`_ must be installed. It probably already is, but see
"Installing Wheel" below if these instructions fail.

To build the west bootstrapper wheel file::

  python3 setup.py bdist_wheel

On Windows::

  py -3 setup.py bdist_wheel

This will create a file named ``dist/west-x.y.z-py3-none-any.whl``,
where ``x.y.z`` is the current version in setup.py. Install it with::

  pip3 install -U dist/west-x.y.z-py3-none-any.whl

You can then run ``west init`` with a bootstrapper created from the
current repository contents.  (On Linux, make sure ``~/.local/bin`` is
in your ``PATH``.)

To uninstall this bootstrapper, use::

  pip3 uninstall west

You can then reinstall the mainline version from PyPI, etc.

Installing Wheel
~~~~~~~~~~~~~~~~

On macOS and Windows, you can install wheel with::

  pip3 install wheel

That also works on Linux, but you may want to install wheel from your
system package manager instead -- e.g. if you installed pip from your
system package manager. The wheel package is likely named something
like ``python3-wheel`` in that case.

.. _wheel: https://wheel.readthedocs.io/en/latest/
