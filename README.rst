.. image:: https://img.shields.io/pypi/pyversions/west?logo=python
   :target: https://pypi.org/project/west/

.. image:: https://api.securityscorecards.dev/projects/github.com/zephyrproject-rtos/west/badge
   :target: https://scorecard.dev/viewer/?uri=github.com/zephyrproject-rtos/west

.. image:: https://codecov.io/gh/zephyrproject-rtos/west/graph/badge.svg
   :target: https://codecov.io/gh/zephyrproject-rtos/west

This is the Zephyr RTOS meta tool, ``west``.

https://docs.zephyrproject.org/latest/develop/west/index.html

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

West used to default to `upstream Zephyr's manifest file
<https://github.com/zephyrproject-rtos/zephyr/blob/main/west.yml>`_.
You are still able and will always be able to use this manifest but you
are now required to pass it explicitly and, you are more encouraged than
before to design your own manifest(s) instead and minimized security SBOMs
based on your specific needs.

For more details, see the `West (Zephyr's meta-tool)
<https://docs.zephyrproject.org/latest/develop/west/index.html>`_ in the Zephyr
documentation.

Example usage using the upstream manifest file::

  mkdir zephyrproject && cd zephyrproject
  west init -m https://github.com/zephyrproject-rtos/zephyr/
  west update

What just happened:

- ``west init -m ...`` clones the upstream *west manifest* repository, which in this
  case is the zephyr repository. The manifest repository contains ``west.yml``,
  a YAML description of the Zephyr installation, including Git repositories and
  other metadata.

- ``west update`` clones the other repositories named in the manifest file,
  creating working trees in the installation directory ``zephyrproject``.

Use ``west init -m`` to specify another manifest repository. Use ``--mr`` to
use a revision to initialize from; if not given, the remote's default branch is used.
Use ``--mf`` to use a manifest file other than ``west.yml``.

Parallel Project Updates
------------------------

Updates remain serial by default. Opt in with a positive job count::

  west update                         # unchanged live, serial execution
  west update -j 4                    # complete buffered project blocks
  west update -j 4 --unbuffered       # live output, possibly interleaved

``-j`` / ``--jobs`` requires a positive integer; ``-j 1`` uses the serial
path. Import providers are always updated synchronously with live output
before the remaining ordinary projects can run concurrently.

Buffered mode displays each completed project's stdout and stderr without
waiting for earlier projects in the manifest. Ordering between the two
streams is not preserved. Git sees a non-terminal output sink, so its
automatic colors and progress may differ; explicit Git color settings and
West's ``color.ui`` setting remain effective. Use ``--unbuffered`` for live
terminal output, not to enable concurrent interactive input.

Parallel operations must not need coordinated credential prompts, rebase
editors, signing/pinentry, hooks or other interactive helpers. Use the serial
default for these workflows. Interruption stops pending work and cleans up
this invocation's owned Git children and output buffers; completed updates
are not rolled back. Detached external helpers are not a process-lifecycle API.

Before ordinary parallel work, West checks selected project paths and existing
Git common directories. Overlapping paths or shared Git metadata produce a
warning and serialize the entire ordinary list, preserving the requested output
mode. A failed safety inspection aborts before ordinary updates; import providers
may already have been updated. Projects sharing an auto-cache entry are also
serialized across their dependent operations. These protections are local to
one invocation, not locks against other West or Git processes.

Additional Commands
-------------------

West has multiple sub-commands. After running ``west init``, you can
run them from anywhere under ``zephyrproject``.

For a list of available commands, run ``west -h``. Get help on a
command with ``west <command> -h``.

West is extensible: you can add new commands to west without modifying its
source code. See `Extensions
<https://docs.zephyrproject.org/latest/develop/west/extensions.html>`_ in the
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
where ``x.y.z`` is the current version in pyproject.toml.

To install the wheel::

  pip3 install -U dist/west-x.y.z-py3-none-any.whl

You can ``pip3 uninstall west`` to remove this wheel before re-installing the
version from PyPI, etc.

Measuring Parallel Updates
~~~~~~~~~~~~~~~~~~~~~~~~~~

Freeze the manifest, imported providers and project revisions before comparing
``west update -j 1``, ``-j 2`` and ``-j 4``. Use separate disposable workspaces
and equivalent cache/fetch/network settings, with at least three samples for
each reported condition. Measure initial population separately from incremental
updates and report raw times, medians, job-count order, source revision,
Python/Git/OS versions and filesystem-cache limitations.

After every run, compare the selected project names/paths, HEADs,
``refs/heads/manifest-rev`` and submodule revisions against the serial result.
Timing does not prove correctness. Synchronous imports, shared caches,
serialized conflicts, a large critical-path project and disk/network contention
can limit the benefit; no fixed speedup is promised.

Running the Tests
~~~~~~~~~~~~~~~~~

First, install the dependencies::

  # Using uv
  uv sync --frozen

  # Using pip (requires v25.1 or newer)
  # Recommended in an active virtual environment
  pip3 install --group dev


Then, run the tests (from the project's top level directory)::

  # Run the full test suite with uv (recommended)
  uv run poe all

  # Or run the full test suite with poe directly.
  # (recommended in an active virtual environment)
  poe all

  # Run only the pytest tests (against the local worktree)
  pytest

The ``all`` target from ``poe`` runs multiple tasks sequentially. Use ``poe -h``
to get a list of all available tasks.
You can pass arguments to any ``poe`` task, which are forwarded to the
underlying command. This is especially helpful when you want to run
only specific tests to save time. Examples::

  # Run a subset of tests (file)
  poe test tests/test_project.py

  # Run a single test
  poe test tests/test_project.py::test_workspace

  # Run the ``test_update_narrow()`` code with ``pdb`` (but _not_ the
  # west code which is running in subprocesses)
  poe test --exitfirst --trace -k test_update_narrow

  # Run all tests with "import" in their name and let them log to the
  # current terminal
  poe test -k import --capture=no
