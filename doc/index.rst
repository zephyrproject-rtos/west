.. _west:

West: an extensible workspace manager
#####################################

West is the swiss-army knife command line tool developed by the Zephyr
project\ [#west-name]_. It manages a :term:`workspace <west workspace>` of Git
repositories from a single :term:`manifest <west manifest>` file: its built-in
commands provide a multiple repository management system with features inspired
by Google's Repo tool and Git submodules. West is also "pluggable": you can
write your own :ref:`extension commands <west-extensions>` which add features
to west.

Quick start
***********

Install west in a virtual environment, then create a workspace from a manifest
repository and fetch the projects it lists. This example uses the Zephyr
manifest::

  python3 -m venv zephyrproject/.venv
  source zephyrproject/.venv/bin/activate
  pip install west
  west init -m https://github.com/zephyrproject-rtos/zephyr zephyrproject
  cd zephyrproject
  west update

``west init`` clones the manifest repository into the new ``zephyrproject``
workspace, and ``west update`` clones the other repositories named in its
``west.yml``. See :ref:`west-install` and :ref:`west-basics` for details.

Like ``git`` and ``docker``, the top-level ``west`` command takes some common
options, a sub-command to run, and then options and arguments for that
sub-command::

  west [common-opts] <command> [opts] <args>

If the ``west`` launcher is not on your ``PATH``, or you want to pick the
interpreter explicitly, run it as ``python3 -m west`` instead. Run ``west
--help`` (or ``west -h``) for top-level help on the available commands, and
``west <command> -h`` for detailed help on each command.

.. toctree::
   :caption: Getting started
   :maxdepth: 1

   install.rst
   basics.rst

.. toctree::
   :caption: Using west
   :maxdepth: 1

   built-in.rst
   workspaces.rst
   manifest.rst
   config.rst
   alias.rst
   troubleshooting.rst

.. toctree::
   :caption: Extending west
   :maxdepth: 1

   extensions.rst

.. toctree::
   :caption: Reference
   :maxdepth: 1

   west-apis.rst

.. toctree::
   :caption: Project
   :maxdepth: 1

   why.rst
   release-notes.rst

.. rubric:: Footnotes

.. [#west-name]

   Zephyr is an English name for the Latin `Zephyrus
   <https://en.wiktionary.org/wiki/Zephyrus>`_, the ancient Greek god of the
   west wind.
