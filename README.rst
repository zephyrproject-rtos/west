.. image:: https://img.shields.io/pypi/pyversions/west?logo=python
   :target: https://pypi.org/project/west/

.. image:: https://api.securityscorecards.dev/projects/github.com/zephyrproject-rtos/west/badge
   :target: https://scorecard.dev/viewer/?uri=github.com/zephyrproject-rtos/west

.. image:: https://codecov.io/gh/zephyrproject-rtos/west/graph/badge.svg
   :target: https://codecov.io/gh/zephyrproject-rtos/west

This is the Zephyr RTOS meta tool, ``west``.

https://docs.zephyrproject.org/west/latest/

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

For more details, see the `west documentation
<https://docs.zephyrproject.org/west/latest/>`_.

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

Additional Commands
-------------------

West has multiple sub-commands. After running ``west init``, you can
run them from anywhere under ``zephyrproject``.

For a list of available commands, run ``west -h``. Get help on a
command with ``west <command> -h``.

West is extensible: you can add new commands to west without modifying its
source code. See `Extensions
<https://docs.zephyrproject.org/west/latest/extensions.html>`_ in the
documentation for details.
