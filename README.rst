This package contains the Zephyr RTOS meta tool, 'west'.

WARNING
-------

DO NOT INSTALL WITH "python3 setup.py install".

Please use pip to install in development mode as documented below.

Important Note
--------------

West is distributed in two pieces:

1. A bootstrap/wrapper script, which is distributed via PyPI.
2. The "main" west package and entry points, which are fetched by the
   bootstrap script.

This somewhat unusual arrangement is because:

- One of West's jobs is to manage interaction with Zephyr's multiple
  Git repositories, including its own.
- West is in its experimental stages and is moving quickly, meaning
  users need to stay on HEAD.

The default setup.py installs the **wrapper script only**.

Installation
------------

To install the West bootstrapping/wrapper script from this package in
development mode, clone this repository and run this from the top
level directory:

$ pip3 install -e .

Then use the wrapper script to initialize a Zephyr installation with::

  $ west init your/zephyr/install-dir
  $ cd your/zephyr/install-dir
  $ west <command>

The ``west init`` call will:

- create your/zephyr/install-dir
- clone the Zephyr manifest repository (whose URL can be overridden
  with the ``-u`` option, and branch with ``--mr`` / ``--manifest-rev``)
- clone the latest West repository (the URL override is ``-w``, and
  revision/branch override is ``--wr`` / ``--west-rev``)

Running ``west <command>`` from :file:`your/zephyr/install-dir` or
underneath it will invoke west in "wrapper" mode: any commands other
than ``init`` will be delegated to the West tree pulled by ``west
init``.

This arrangement may seem familiar to Android (platform, not app)
developers. The source management features of West were indeed
inspired by the Android Repo tool's features, but West makes
significant departures from Repo's behavior.

Alternative Usage
-----------------

If you don't want to change your system outside of cloning this
repository, you can also clone West's Git repository and run the
package as a module:

$ python3 -m west

Only do this if you know what you're doing (and make sure to install
the requirements as specified by the install_requires line in
setup.py).

Test Suite
----------

To run the test suite, use:

```
$ python3 setup.py test
```
