Notes for maintainers cutting a release.

Pre-release test plan
---------------------

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

3. Create and update a default (Zephyr) workspace on all of the platforms from
   1. ::

     $ west init zephyrproject
     $ cd zephyrproject; west update

4. Do Zephyr specific testing in the Zephyr workspace on all of the platforms
   from 1. ::

     $ west build -b qemu_x86 -s zephyr/samples/hello_world -d build-qemu-x86
     $ west build -d build-qemu-x86 -t run

     $ west build -b qemu_cortex_m3 -s zephyr/samples/hello_world -d build-qemu-m3
     $ west build -d build-qemu-m3 -t run

     # This example uses a Nordic board. Do this for as many boards
     # as you have access to / volunteers for.
     $ west build -b nrf52_pca10040 -s zephyr/samples/hello_world -d build-nrf52
     $ west flash -d build-nrf52
     $ west debug -d build-nrf52
     $ west debugserver -d build-nrf52
     $ west attach -d build-nrf52

5. Bump src/west/version.py to tag and upload an RC version, e.g. vX.Y.Zrc1
   using below steps.

   Make sure to check if west.manifest.SCHEMA_VERSION also needs an update.

6. Upload the rc to PyPI using below steps.

7. For each of the platforms in 1., upgrade to the RC::

     pip install --U --pre west

   Now repeat steps 3. -- 5., and repeat step 4. in an existing workspace.
   (It's still a pass if ``west build`` requires ``--pristine``.)

Tagging the release
-------------------

Create and push a GPG signed tag.

  $ git tag -a -s vX.Y.Z -m 'West vX.Y.Z

  Signed-off-by: Your Name <your.name@example.com>'
  $ git push origin vX.Y.Z

Building and uploading the release wheels
-----------------------------------------

You need the zephyr-project PyPI credentials for the 'twine upload' command. ::

  $ git clean -ffdx
  $ python3 setup.py sdist bdist_wheel
  $ twine upload -u zephyr-project dist/*

The 'git clean' step is important. We've anecdotally observed broken wheels
being generated from dirty repositories.

Cut a release branch
--------------------

If you've cut a new minor version (vX.Y.0), cut a release branch, vX.Y-branch.
Fixes for versions vX.Y.Z should go to that branch.
