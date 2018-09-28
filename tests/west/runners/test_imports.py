from west.runners.core import ZephyrBinaryRunner


def test_runner_imports():
    # Ensure that all runner modules are imported and returned by
    # get_runners().
    #
    # This is just a basic sanity check against errors introduced by
    # tree-wide refactorings for runners that don't have their own
    # test suites.
    runner_names = set(r.name() for r in ZephyrBinaryRunner.get_runners())
    expected = set(('arc-nsim', 'bossac', 'dfu-util', 'em-starterkit', 'esp32',
                    'jlink', 'nios2', 'nrfjprog', 'openocd', 'pyocd',
                    'qemu', 'xtensa', 'intel_s1000'))
    assert runner_names == expected
