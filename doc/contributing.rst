.. _west-contributing:

Contribution Guidelines
#######################

West lives under the umbrella of `The Zephyr Project`_, and contributions
follow the same process as the rest of the project. The authoritative reference
is the full `Zephyr Contribution Guidelines`_; the essentials are:

* West is licensed under the permissive open source `Apache 2.0 license`_.

* Contributions are made under the Developer Certificate of Origin (`DCO`_):
  sign off each commit with a ``Signed-off-by`` line using
  ``git commit --signoff``.

* West follows Zephyr's git workflow, which differs from the typical GitHub
  flow: keep each commit a self-contained change, and update a pull request by
  amending or rebasing and force-pushing, not by adding fixup or merge
  commits. See `PR requirements`_ for the details.

* West is developed as a standalone, general-purpose tool: although it lives
  under the Zephyr Project and follows its processes, west itself aims to stay
  free of Zephyr-specific assumptions. Its source lives in its own repository,
  separate from the main Zephyr tree, and issues are tracked there:
  https://github.com/zephyrproject-rtos/west

* CI runs on every pull request, verifying coding style, type checks, and the
  test suite across supported platforms. Every one of these checks can be run
  locally with ``poe``. See `Running the Tests`_ below. A CI failure
  you cannot reproduce on your own workstation is considered a bug; please
  report it.

* The `Zephyr Discord server`_ (see the ``#west`` channel) and the `Zephyr
  devel mailing list`_ are good places to ask questions and engage with the
  community.

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

.. _The Zephyr Project: https://www.zephyrproject.org/
.. _Zephyr Contribution Guidelines: https://docs.zephyrproject.org/latest/contribute/index.html
.. _Apache 2.0 license: https://github.com/zephyrproject-rtos/west/blob/main/LICENSE
.. _DCO: https://docs.zephyrproject.org/latest/contribute/guidelines.html#developer-certification-of-origin-dco
.. _PR requirements: https://docs.zephyrproject.org/latest/contribute/contributor_expectations.html#pr-requirements
.. _Zephyr Discord server: https://chat.zephyrproject.org/
.. _Zephyr devel mailing list: https://lists.zephyrproject.org/g/devel
