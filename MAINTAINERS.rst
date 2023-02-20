Notes for maintainers.

Pre-release test plan
---------------------

0. If no release branch exists, fork the version numbers in the release branch
   (vX.Y-branch) and the main branch. See "Cutting a release branch", below,
   for details.

   The rest of these steps should be done in the release branch::

     git checkout vX.Y-branch

1. Make tox happy on the following first-party platforms:

   - Windows 10
   - the latest macOS
   - the latest Ubuntu LTS

2. Make tox happy on other popular Linux distributions as resources allow.
   Doing this in a container is fine.

   - Arch
   - the latest Ubuntu release (if different than the latest LTS)
   - Debian stable (if its Python 3 is still supported)
   - Debian testing
   - Fedora

3. Build alpha N (N=1 to start, then N=2 if you need more commits, etc.) and
   upload to pypi. See "Building and uploading the release wheels" below for
   a procedure.

4. Install the alpha on test platforms. ::

     pip3 install west==X.YaN

5. Create and update a default (Zephyr) workspace on all of the platforms from
   1., using the installed alpha::

     west init zephyrproject
     cd zephyrproject
     west update

   Make sure zephyrproject/zephyr has a branch checked out that matches the
   default branch used by zephyr itself.

6. Do the following Zephyr specific testing in the Zephyr workspace on all of
   the platforms from 1. Skip QEMU tests on non-Linux platforms, and make sure
   ZEPHYR_BASE is unset in the calling environment. ::

     west build -b qemu_x86 -s zephyr/samples/hello_world -d build-qemu-x86
     west build -d build-qemu-x86 -t run

     west build -b qemu_cortex_m3 -s zephyr/samples/hello_world -d build-qemu-m3
     west build -d build-qemu-m3 -t run

     # This example uses a Nordic board. Do this for as many boards
     # as you have access to / volunteers for.
     west build -b nrf52dk_nrf52832 -s zephyr/samples/hello_world -d build-nrf52
     west flash -d build-nrf52
     west debug -d build-nrf52
     west debugserver -d build-nrf52
     west attach -d build-nrf52

   (It's still a pass if ``west build`` requires ``--pristine``.)

7. Assuming that all went well (if it didn't, go fix it and repeat):

   - update __version__ to 'X.Y.Z' (i.e. drop the 'aN' suffix that denotes
     alpha N)

   - tag the release on GitHub (see "Tagging the release" for a procedure)

   - create a release on GitHub from the new tag by going to
     https://github.com/zephyrproject-rtos/west/releases
     clicking "Draft a new release", and following instructions

   - upload the release artifacts to PyPI (see "Building and uploading the
     release wheels" for a procedure)

8. Send email to the Zephyr lists, announce@ and users@, notifying them of the
   new release. Include 'git shortlog' data of the new commits since the last
   release to give credit to all contributors.

Building and uploading the release wheels
-----------------------------------------

You need the zephyr-project PyPI credentials for the 'twine upload' command. ::

  git clean -ffdx
  python3 setup.py sdist bdist_wheel
  twine upload -u zephyr-project dist/*

The 'git clean' step is important. We've anecdotally observed broken wheels
being generated from dirty repositories.

Tagging the release
-------------------

Create and push a GPG signed tag. ::

  git tag -a -s vX.Y.Z -m 'West vX.Y.Z

  Signed-off-by: Your Name <your.name@example.com>'

  git push origin vX.Y.Z

Cutting a release branch
------------------------

This is how to cut a new release branch for minor version vX.Y.

Summary of what happens:

- before:

  - vX.Y-branch does not exist
  - main branch is at version X.(Y-1).99

- after:

  - vX.Y-branch exists and is at version X.Y.0a1
  - main is at version X.Y.99
  - west.manifest.SCHEMA_VERSION may be updated

1. Check the git logs since the last release::

     git log vX.(Y-1).99..origin/main

   Decide if west.manifest.SCHEMA_VERSION needs an update:

   - SCHEMA_VERSION should be updated to X.Y if release vX.Y will have manifest
     syntax changes that earlier versions of west cannot parse.

   - SCHEMA_VERSION should *not* be changed for west vX.Y if the manifest
     syntax is fully compatible with what west vX.(Y-1) can handle.

   If you want to change SCHEMA_VERSION, send this as a pull request to the
   main branch and get it reviewed and merged. (This requires a PR and review
   even though the rest of the steps don't.)

   **Don't** introduce incompatible manifest changes in patch versions.
   That violates semantic versioning. Example: if v0.7.3 can parse a manifest,
   v0.7.2 should be able to parse it, too, and with the same results.

2. Create and push the release branch for minor version vX.Y.0, which is named
   "vX.Y-branch"::

      git checkout -b vX.Y-branch origin/main
      git push origin vX.Y-branch

   This should already contain the SCHEMA_VERSION change if one is needed.

   Subsequent fixes for patch versions vX.Y.Z should go to vX.Y-branch after
   being backported from main (or the other way around in case of an urgent
   hotfix).

3. In vX.Y-branch, in src/west/version.py, set __version__ to X.Y.0a1.
   Push this to origin/vX.Y-branch. You don't need a PR for this.

4. In the main branch, set __version__ to X.Y.99.
   Push this to origin/main. You don't need a PR for this.

5. Create an annotated tag vX.Y.99 which points to the main branch commit you
   just created in the previous step. Push it to origin/main. You don't need a
   PR for this. See refs/tags/v0.12.99 for an example. (This makes 'git
   describe' output easy to read during development.)

From this point forward, the main branch is moving independently from the
release branch. Do the release prep work in the release branch.
