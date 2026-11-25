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


WINE and wenv
=============

While Python provides a great OS-abstraction layer, there is still a few, dark
corners of west code that remain OS-specific, most notably around file paths.
See for instance `west issue 961`_ and the many other issues linked from there.

Switching back and forth between machines during interactive testing is slow
and inconvenient. It requires either dual-booting or copying/sharing files
across multiple systems - and a Windows license. Thanks to WINE and wenv, this
section provides a very quick way to simulate running west's test suite on
Windows. It has been successfully used in the past to reproduce, debug and fix
Windows-specific issues on Linux.

.. warning::

   What this section documents is only a development shortcut.  A "real"
   Windows system *must* still be used to validate official releases.

WINE
----

`WINE`_ provides a way to run Windows binaries on Linux.  First you need to
install WINE. Try the following commands and your Linux distribution will most
likely tell you what packages are missing::

  wine   --help
  wine64 --help
  wine32 --help

At least one of these must print WINE's help message before you proceed.
Some distributions let you install both wine32 and wine64 at the same
time. To find which one the ``wine`` command currently points at::

  wine cmd
  dir c:

If you see *both* ``Program Files`` *and* ``Program Files (x86)``, then you are
running 64 bits WINE.

wenv
----

`WENV`_ has a comprehensive but complex documentation.  The recipe below
provides all the necessary shortcuts to get you started before even looking at
the wenv documentation yet::

  pipx install wenv
  export WENV_PYTHONVERSION=3.12.10 # or any other version that works. Required.
  export WENV_ARCH=win64            # Optional - wenv defaults to WINE 32 bits.

  # This copies a couple gigabytes from your local WINE installation into
  # $HOME/.local/share/wenv/. Then it downloads and install Python there.
  wenv init

  # wenv help should now list "python" and "pip"
  wenv help

  # Among others, Python's interactive prompt shows whether it's 32 or 64 bits
  wenv python
  # Warning: pykwalify is being replaced by jsonschema, see west issue #904 and west/pyproject.toml
  wenv pip -v install setuptools
  wenv pip -v install pyyaml pykwalify pytest pytest-xdist pytest-env

``~/.wenv.json`` offers a permanent alternative to the ``WENV_*`` environment variables above::

  echo '{ "pythonversion": "3.12.10" }' >> ~/.wenv.json
  # Or, for WINE64
  echo '{ "pythonversion": "3.12.10", "arch":"win64" }' >> ~/.wenv.json

Download a ``MinGit-*-.zip`` file from https://github.com/git-for-windows/git/releases/
and "install" it in wenv's ``C:`` disk::

  unzip -d ~/.local/share/wenv/win64/drive_c/MinGit/  ~/Downloads/MinGit-version-64-bit.zip

Add this somewhere in ``west/pyproject.toml``::

  # TODO: find a way to do this in "wenv" itself instead of modifying
  # a file tracked in git.
  # Useful flag: wenv pytest --pytest-env-verbose ...
  [tool.pytest_env]
  PATH = { value = "{PATH};C:\\MinGit\\cmd", transform = true }

At last::

  cd west
  wenv pytest -h
  wenv pytest -v -s -k round_trip -n auto
  ....
  ============== 9 passed ==============

When not used anymore, delete the current ``$WENV_ARCH`` +
``$WENV_PYTHONVERSION`` combination and reclaim the couple gigabytes consumed by
``wenv init`` in ``$HOME/.local/share/wenv/``::

  wenv help

    => The following interpreters, scripts and modules are installed and available:
    - wenv python
    - wenv pythonw
    - wenv pip3.12
    - wenv py.test
    - wenv pykwalify
    - wenv pytest
    - ...

  wenv clean

  wenv help

   => The following interpreters, scripts and modules are installed and available:

     (None)


.. _uv: https://docs.astral.sh/uv/
.. _The Zephyr Project: https://www.zephyrproject.org/
.. _Zephyr Contribution Guidelines: https://docs.zephyrproject.org/latest/contribute/index.html
.. _Apache 2.0 license: https://github.com/zephyrproject-rtos/west/blob/main/LICENSE
.. _DCO: https://docs.zephyrproject.org/latest/contribute/guidelines.html#developer-certification-of-origin-dco
.. _PR requirements: https://docs.zephyrproject.org/latest/contribute/contributor_expectations.html#pr-requirements
.. _Zephyr Discord server: https://chat.zephyrproject.org/
.. _Zephyr devel mailing list: https://lists.zephyrproject.org/g/devel
.. _WINE: https://www.winehq.org/
.. _west issue 961: https://github.com/zephyrproject-rtos/west/issues/961
.. _WENV: https://wenv.readthedocs.io/en/latest/usage.html
