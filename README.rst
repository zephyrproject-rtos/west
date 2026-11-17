.. image:: https://img.shields.io/pypi/pyversions/west?logo=python
   :target: https://pypi.org/project/west/

.. image:: https://api.securityscorecards.dev/projects/github.com/zephyrproject-rtos/west/badge
   :target: https://scorecard.dev/viewer/?uri=github.com/zephyrproject-rtos/west

.. image:: https://codecov.io/gh/zephyrproject-rtos/west/graph/badge.svg
   :target: https://codecov.io/gh/zephyrproject-rtos/west

West
====

West is the swiss-army knife command line tool developed by the `Zephyr
Project`_. It manages a workspace of Git repositories from a single file, the
*west manifest*: ``west init`` creates the workspace and ``west update`` clones
and updates the repositories the manifest names. West is also extensible: you
can add your own commands to it without modifying its source code.

Installation
------------

West is distributed through `PyPI`_. Install it with ``pip``, preferably in a
virtual environment::

  pip install west

See `Installing west`_ for the details and for other ways to install it.

Quick start
-----------

Create a workspace from a manifest repository and fetch the repositories it
lists. This example uses the Zephyr manifest::

  west init -m https://github.com/zephyrproject-rtos/zephyr zephyrproject
  cd zephyrproject
  west update

- ``west init -m`` clones the manifest repository, here the Zephyr repository,
  into the new ``zephyrproject`` workspace. The manifest repository contains
  ``west.yml``, the manifest file listing the other repositories. Use ``--mr``
  to select a revision of the manifest repository and ``--mf`` to use a
  manifest file other than ``west.yml``.

- ``west update`` clones the repositories named in the manifest file into the
  workspace, and updates them when it is run again.

After ``west init``, you can run west commands from anywhere in the workspace.
For a list of the available commands, run ``west -h``; for help on a command,
run ``west <command> -h``. See `Extensions`_ for adding your own commands.

Resources
---------

Documentation
~~~~~~~~~~~~~

  | 📖 `West Documentation`_
  | 🚀 `Installing west`_
  | 📝 `Release notes`_

Code and Development
~~~~~~~~~~~~~~~~~~~~

  | 🌐 `Source Code Repository`_
  | 📦 `Releases`_
  | 🤝 `Contributing`_: guidelines, installing west from source, running its
    tests and building its documentation

Community and Support
~~~~~~~~~~~~~~~~~~~~~

  | 💬 `Discord Server`_ for real-time community discussions, in the ``#west``
    channel
  | 📧 `User mailing list (users@lists.zephyrproject.org)`_
  | 📧 `Developer mailing list (devel@lists.zephyrproject.org)`_

Issue Tracking and Security
~~~~~~~~~~~~~~~~~~~~~~~~~~~

  | 🐛 `GitHub Issues`_
  | ⚠️ Report security vulnerabilities at vulnerabilities@zephyrproject.org

.. _Zephyr Project: https://www.zephyrproject.org/
.. _PyPI: https://pypi.org/project/west/
.. _Installing west: https://docs.zephyrproject.org/west/latest/install.html
.. _Extensions: https://docs.zephyrproject.org/west/latest/extensions.html
.. _Contributing: https://docs.zephyrproject.org/west/latest/contributing.html
.. _West Documentation: https://docs.zephyrproject.org/west/latest/
.. _Release notes: https://docs.zephyrproject.org/west/latest/release-notes.html
.. _Source Code Repository: https://github.com/zephyrproject-rtos/west
.. _Releases: https://github.com/zephyrproject-rtos/west/releases
.. _Discord Server: https://chat.zephyrproject.org
.. _User mailing list (users@lists.zephyrproject.org): https://lists.zephyrproject.org/g/users
.. _Developer mailing list (devel@lists.zephyrproject.org): https://lists.zephyrproject.org/g/devel
.. _GitHub Issues: https://github.com/zephyrproject-rtos/west/issues
