# Copyright (c) 2018, 2019, Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

'''West configuration file handling.

West follows Git-like conventions for configuration file locations.
There are three types of configuration file: system-wide files apply
to all users on the current machine, global files apply to the current
user, and local files apply to the current west workspace.

You can override these files' locations with the ``WEST_CONFIG_SYSTEM``,
``WEST_CONFIG_GLOBAL``, and ``WEST_CONFIG_LOCAL`` environment variables.

Configuration values from later configuration files override configuration
from earlier ones. Local values have highest precedence, and system values
lowest.

`Configuration`, `ConfigFile`, and `MalformedConfig` are the rust-backed
implementations re-exported from the `_west_native` PyO3 binding. The
underlying TOML walking + write logic lives in `west_core::config`; the
system/global/conf.d/local layer resolution lives in
`west_core::config_paths`.
'''

from west._west_native import Configuration, ConfigFile, MalformedConfig

__all__ = ['Configuration', 'ConfigFile', 'MalformedConfig']
