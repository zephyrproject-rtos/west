.. _west-install:

Installing west
###############

West is written in Python 3 and distributed through `PyPI`_.
Use :file:`pip3` to install or upgrade west:

On Linux::

  pip3 install --user -U west

On Windows and macOS::

  pip3 install -U west

.. note::
   See :ref:`python-pip` for additional clarification on using the
   ``--user`` switch.

Afterwards, you can run ``pip3 show -f west`` for information on where the west
binary and related files were installed.

Once west is installed, you can use it to :ref:`clone the Zephyr repositories
<clone-zephyr>`.

.. _west-struct:

Structure
*********

West's code is distributed via PyPI in a Python package named ``west``.
This distribution includes a launcher executable, which is also named
``west`` (or ``west.exe`` on Windows).

When west is installed, the launcher is placed by :file:`pip3` somewhere in
the user's filesystem (exactly where depends on the operating system, but
should be on the ``PATH`` :ref:`environment variable <env_vars>`). This
launcher is the command-line entry point to running both built-in commands
like ``west init``, ``west update``, along with any extensions discovered
in the workspace.

In addition to its command-line interface, you can also use west's Python
APIs directly. See :ref:`west-apis` for details.


.. _PyPI:
   https://pypi.org/project/west/
