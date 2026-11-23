.. _west-contributing:

Contributing
############

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
  locally with ``poe``; see :ref:`west-contributing-tests` below. A CI failure
  you cannot reproduce on your own workstation is considered a bug; please
  report it.

* Most CI checks test the pull request as a whole, not its individual
  commits. It is your responsibility to make sure every commit passes all
  checks, so that ``git bisect`` and branching from any commit keep working.

* The `Zephyr Discord server`_ (see the ``#west`` channel) and the `Zephyr
  devel mailing list`_ are good places to ask questions and engage with the
  community.

Hacking on west
***************

This section contains notes for getting started developing west itself.

``pip`` offers many different ways to (download and) install Python software.
Below are common ways relevant to west; for a complete list check
https://pip.pypa.io/en/stable/topics/. Where `uv`_ offers an alternative, the
commands for both are given.

Quick install
=============

If you are interested in neither west source code nor its history, GitHub
offers ``pip`` the ability to download and install a .zip archive of any random
version with a single command. This allows testing work in progress very
quickly. Examples::

  pip uninstall west
  # Pull request 830
  pip install --dry-run https://github.com/zephyrproject-rtos/west/archive/pull/830/head.zip
  # Release v1.4
  pip install https://github.com/zephyrproject-rtos/west/archive/v1.4-branch.zip
  # Someone's random version
  pip install https://github.com/someone_you_trust/west/archive/some_branch_or_tag.zip

.. warning::

   Never install software from people or locations you do not trust!

Editable install
================

To run west "live" from the current source code tree, install it in editable
mode from the top level directory in the west repository:

.. tabs::

   .. group-tab:: uv

      ``uv sync`` creates a virtual environment in :file:`.venv` and installs
      west in it in editable mode; ``uv run`` runs commands inside it.

      .. code-block:: shell

         uv sync --frozen
         uv run west --version

   .. group-tab:: pip

      Preferably in an active virtual environment:

      .. code-block:: shell

         pip install -e .

This is useful if you are actively working on west and don't want to
re-package and install a wheel each time you run it.

Creating a wheel package
========================

You can create a wheel package and distribute it to others.

To build the west wheel file:

.. tabs::

   .. group-tab:: uv

      .. code-block:: shell

         uv build

   .. group-tab:: pip

      .. code-block:: shell

         pip install --upgrade build
         python -m build

This will create a file named :file:`dist/west-x.y.z-py3-none-any.whl`, where
``x.y.z`` is the current version in :file:`pyproject.toml`.

To install the wheel::

  pip install -U dist/west-x.y.z-py3-none-any.whl

You can ``pip uninstall west`` to remove this wheel before re-installing the
version from PyPI, etc.

.. _west-contributing-tests:

Running the tests
=================

The test suite, linter, formatter and type checker are run through `poe`_
tasks defined in :file:`pyproject.toml`; CI runs the same checks. Install west
in editable mode together with the development dependencies, which include
``poe`` itself, then run everything:

.. tabs::

   .. group-tab:: uv

      ``uv sync`` installs west and the development dependencies.

      .. code-block:: shell

         uv sync --frozen
         uv run poe all

   .. group-tab:: pip

      Requires pip v25.1 or newer, preferably in an active virtual
      environment. The dependency group does not include west itself, hence
      the editable install.

      .. code-block:: shell

         pip install -e . --group dev
         poe all

      ``poe`` runs its tasks in a :file:`.venv` directory at the top of the
      repository when there is one, such as the environment ``uv`` creates,
      instead of the active environment.

``poe all`` runs these tasks in sequence; each can also be run on its own:

- ``poe test``: the test suite with ``pytest``, with warnings turned into
  errors like in CI
- ``poe lint``: ``ruff check``
- ``poe format``: ``ruff format``, which rewrites files in place. CI only
  checks the formatting, so run this before committing.
- ``poe types``: ``mypy``

Run ``poe`` without arguments to list all tasks. Arguments given to a task are
forwarded to the underlying command, which is useful to run a subset of the
tests. In the examples below, prefix the commands with ``uv run`` when not in
an active virtual environment::

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

  # Run the test suite in parallel, using pytest-xdist
  poe test-xdist

Running ``pytest`` directly also works, since it is configured in
:file:`pyproject.toml`, but without the warnings-as-errors setting ``poe test``
adds. Either way the tests run against the source tree, not against an
installed west; pass ``-o pythonpath=`` to test the installed west instead.

.. _poe: https://poethepoet.natn.io/

Building the documentation
==========================

The documentation you are reading lives in the :file:`doc` directory of the
west repository and is built with Sphinx. Install the documentation
dependencies and build it:

.. tabs::

   .. group-tab:: uv

      The documentation dependencies are in the ``docs`` dependency group,
      which ``uv`` does not install by default; select it on each command.

      .. code-block:: shell

         uv run --group docs poe build-docs

   .. group-tab:: pip

      .. code-block:: shell

         pip install -e . --group dev --group docs
         poe build-docs

The HTML output is in :file:`doc/_build/html`. While editing, ``poe
serve-docs`` serves the documentation on http://127.0.0.1:8000 and rebuilds it
whenever a source file changes, and ``poe linkcheck-docs`` checks the external
links. Both need the ``docs`` dependency group as well.

The theme files under :file:`doc/_static` and :file:`doc/_templates` are
copied from the Zephyr repository; :file:`doc/README.rst` records where they
come from and how to synchronize them.

.. _uv: https://docs.astral.sh/uv/
.. _The Zephyr Project: https://www.zephyrproject.org/
.. _Zephyr Contribution Guidelines: https://docs.zephyrproject.org/latest/contribute/index.html
.. _Apache 2.0 license: https://github.com/zephyrproject-rtos/west/blob/main/LICENSE
.. _DCO: https://docs.zephyrproject.org/latest/contribute/guidelines.html#developer-certification-of-origin-dco
.. _PR requirements: https://docs.zephyrproject.org/latest/contribute/contributor_expectations.html#pr-requirements
.. _Zephyr Discord server: https://chat.zephyrproject.org/
.. _Zephyr devel mailing list: https://lists.zephyrproject.org/g/devel
