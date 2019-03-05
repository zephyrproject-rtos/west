# Copyright 2019 Foundries.io Limited.
#
# SPDX-License-Identifier: Apache-2.0

'''Backports for older Python versions.'''

# NOTE: these should be periodically cleaned up and removed from the
# code base when support for older Python versions is dropped.

import sys

# Ensure CompletedProcess is available (it was added in 3.5).
if sys.version_info < (3, 5):
    from subprocess import CalledProcessError

    class CompletedProcess:
        '''subprocess.CompletedProcess-alike provided for Python 3.4.'''

        def __init__(self, args, returncode, stdout, stderr):
            self.args = args
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

        def check_returncode(self):
            if self.returncode:
                # The stdout and stderr attributes weren't added until
                # Python 3.5; we manually include them here for code
                # targeting more recent versions which expects them.
                ret = CalledProcessError(self.returncode, self.args,
                                         output=self.stdout)
                ret.stdout = self.stdout
                ret.stderr = None
                raise ret
else:
    from subprocess import CompletedProcess

__all__ = ['CompletedProcess']
