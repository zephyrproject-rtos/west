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
  locally with ``poe``. See `Running the Tests`_ in the README. A CI failure
  you cannot reproduce on your own workstation is considered a bug; please
  report it.

* The `Zephyr Discord server`_ (see the ``#west`` channel) and the `Zephyr
  devel mailing list`_ are good places to ask questions and engage with the
  community.

.. _The Zephyr Project: https://www.zephyrproject.org/
.. _Zephyr Contribution Guidelines: https://docs.zephyrproject.org/latest/contribute/index.html
.. _Apache 2.0 license: https://github.com/zephyrproject-rtos/west/blob/main/LICENSE
.. _DCO: https://docs.zephyrproject.org/latest/contribute/guidelines.html#developer-certification-of-origin-dco
.. _PR requirements: https://docs.zephyrproject.org/latest/contribute/contributor_expectations.html#pr-requirements
.. _Running the Tests: https://github.com/zephyrproject-rtos/west/blob/main/README.rst#running-the-tests
.. _Zephyr Discord server: https://chat.zephyrproject.org/
.. _Zephyr devel mailing list: https://lists.zephyrproject.org/g/devel
