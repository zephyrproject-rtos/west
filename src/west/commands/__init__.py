# Copyright 2018 Open Source Foundries Limited.
# Copyright 2019 Foundries.io Limited.
#
# SPDX-License-Identifier: Apache-2.0

'''west.commands

This provides WestCommand, which is the common abstraction all west
command-line commands implement.

All built-in west commands should be implemented in modules in this
package. To use them, import them from the modules which contain them.

west.commands also includes support for discovering external commands
based on configuration in the manifest.

'''

from west.commands.command import WestCommand, \
    CommandContextError, CommandError, \
    external_commands, WestExtCommandSpec

__all__ = ['CommandContextError', 'CommandError', 'WestCommand',
           'external_commands', 'WestExtCommandSpec']
