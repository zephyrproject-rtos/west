.. _west-install:

Installing west
###############

West is written in Python 3 and distributed through `PyPI`_ in a package named
``west``. The supported Python versions are listed on its PyPI page.

In a virtual environment
************************

The recommended way to install west is in a Python virtual environment, next
to the workspace it manages. This keeps west together with the Python packages
that the workspace's :ref:`extension commands <west-extensions>` need.

Create the environment, activate it, and install west with ``pip``:

.. tabs::

   .. group-tab:: Linux and macOS

      .. code-block:: shell

         python3 -m venv zephyrproject/.venv
         source zephyrproject/.venv/bin/activate
         pip install west

   .. group-tab:: Windows

      .. code-block:: bat

         python -m venv zephyrproject\.venv
         zephyrproject\.venv\Scripts\activate.bat
         pip install west

      In PowerShell, run ``zephyrproject\.venv\Scripts\Activate.ps1`` to
      activate the environment instead.

The ``west`` command is available whenever the environment is active, so
activate it again in each new terminal. Here :file:`zephyrproject` is the
directory that will become the :term:`west workspace`; the environment can
live anywhere else if you prefer.

Once west is installed, continue with :ref:`west-basics` to create a
workspace.

Other ways to install
*********************

Some distributions package west, for example Debian and Ubuntu::

  sudo apt install west

The packaged version follows the distribution's release cycle and can be older
than the one on PyPI.

You can install west as a standalone tool that is available in every terminal,
using `pipx`_ or `uv`_::

  pipx install west
  # or
  uv tool install west

You can also install west with ``pip`` outside of a virtual environment::

  pip3 install --user west

Many Linux distributions mark their Python installation as externally managed
and refuse this with an ``externally-managed-environment`` error.

.. warning::

   Only use one of these if your workspace needs nothing but west's built-in
   commands. West installed this way does not use packages from a virtual
   environment, even an active one, so extension commands whose dependencies
   are not installed alongside west fail with a ``ModuleNotFoundError``.

Upgrading and uninstalling
**************************

Use the tool you installed west with. With ``pip``, in the active virtual
environment::

  pip install --upgrade west
  pip uninstall west

Run ``west --version`` to see which version is installed, and ``pip show -f
west`` for information on where the west launcher and related files are.

.. _west-struct:

Structure
*********

West's code is distributed via PyPI in a Python package named ``west``.
This distribution includes a launcher executable, which is also named
``west`` (or ``west.exe`` on Windows).

When west is installed, the launcher is placed somewhere in the user's
filesystem: in the virtual environment's :file:`bin` (or :file:`Scripts`)
directory, or in a location that depends on the operating system and the
installation method. It has to be on the ``PATH`` :ref:`environment variable
<env_vars>` to be found; see :ref:`west-troubleshooting` if it is not. This
launcher is the command-line entry point to running both built-in commands
like ``west init``, ``west update``, along with any extensions discovered
in the workspace.

In addition to its command-line interface, you can also use west's Python
APIs directly. See :ref:`west-apis` for details.

.. _PyPI:
   https://pypi.org/project/west/

.. _pipx:
   https://pipx.pypa.io/

.. _uv:
   https://docs.astral.sh/uv/
