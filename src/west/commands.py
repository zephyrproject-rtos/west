# Copyright 2018 Open Source Foundries Limited.
# Copyright 2019 Foundries.io Limited.
#
# SPDX-License-Identifier: Apache-2.0

from abc import ABC, abstractmethod
from collections import OrderedDict
import importlib
import itertools
import os
import sys

import pykwalify
import yaml

from west import log
from west.configuration import config as _config
from west.manifest import Manifest
from west.util import escapes_directory

'''\
This package provides WestCommand, which is the common abstraction all
west commands subclass.

This package also provides support for extension commands.'''

__all__ = ['CommandContextError', 'CommandError', 'WestCommand']

class CommandError(RuntimeError):
    '''Indicates that a command failed.'''

    def __init__(self, returncode=1):
        super().__init__()
        self.returncode = returncode

class CommandContextError(CommandError):
    '''Indicates that a context-dependent command could not be run.'''

class ExtensionCommandError(CommandError):
    '''Exception class indicating an extension command was badly
    defined and could not be created.'''

    def __init__(self, **kwargs):
        self.hint = kwargs.pop('hint', None)
        super(ExtensionCommandError, self).__init__(**kwargs)

def _no_topdir_msg(cwd, name):
    return f'''\
no west workspace found from "{cwd}"; "west {name}" requires one.
Things to try:
  - Change directory to somewhere inside a west workspace and retry.
  - Set ZEPHYR_BASE to a zephyr repository path in a west workspace.
  - Run "west init" to set up a workspace here.
  - Run "west init -h" for additional information.
'''

class WestCommand(ABC):
    '''Abstract superclass for a west command.'''

    def __init__(self, name, help, description, accepts_unknown_args=False,
                 requires_workspace=True, requires_installation=None):
        '''Abstract superclass for a west command.

        Some fields, such as *name*, *help*, and *description*,
        overlap with kwargs that should be passed to the
        ``argparse.ArgumentParser`` added by `WestCommand.add_parser`.
        This wart is by design: ``argparse`` doesn't make many API stability
        guarantees, so this information must be duplicated here for
        future-proofing.

        :param name: the command's name, as entered by the user
        :param help: one-line command help text
        :param description: multi-line command description
        :param accepts_unknown_args: if true, the command can handle
            arbitrary unknown command line arguments in `WestCommand.run`.
            Otherwise, it's a fatal to pass unknown arguments.
        :param requires_workspace: if true, the command requires a
            west workspace to run, and running it outside of one is
            a fatal error.
        :param requires_installation: deprecated equivalent for
            "requires_workspace"; this may go away eventually.
        '''
        self.name = name
        self.help = help
        self.description = description
        self.accepts_unknown_args = accepts_unknown_args
        if requires_installation is not None:
            self.requires_workspace = requires_installation
        else:
            self.requires_workspace = requires_workspace
        self.requires_installation = self.requires_workspace
        self.topdir = None
        self.manifest = None

    def run(self, args, unknown, topdir, manifest=None):
        '''Run the command.

        This raises `west.commands.CommandContextError` if the command
        cannot be run due to a context mismatch. Other exceptions may
        be raised as well.

        :param args: known arguments parsed via `WestCommand.add_parser`
        :param unknown: unknown arguments present on the command line;
            must be empty unless ``accepts_unknown_args`` is true
        :param topdir: west workspace topdir, accessible as
            ``self.topdir`` from `WestCommand.do_run`
        :param manifest: `west.manifest.Manifest` or ``None``,
            accessible as ``self.manifest`` from `WestCommand.do_run`
        '''
        if unknown and not self.accepts_unknown_args:
            self.parser.error(f'unexpected arguments: {unknown}')
        if not topdir and self.requires_workspace:
            log.die(_no_topdir_msg(os.getcwd(), self.name))
        self.topdir = topdir
        self.manifest = manifest
        self.do_run(args, unknown)

    def add_parser(self, parser_adder):
        '''Registers a parser for this command, and returns it.

        The parser object is stored in a ``parser`` attribute of.

        :param parser_adder: The return value of a call to
            ``argparse.ArgumentParser.add_subparsers()``
        '''
        self.parser = self.do_add_parser(parser_adder)

        if self.parser is None:
            raise ValueError('do_add_parser did not return a value')

        return self.parser

    #
    # Mandatory subclass hooks
    #

    @abstractmethod
    def do_add_parser(self, parser_adder):
        '''Subclass method for registering command line arguments.

        This is called by `WestCommand.add_parser` to register the
        command's options and arguments.

        Subclasses should ``parser_adder.add_parser()`` to add an
        ``ArgumentParser`` for that subcommand, then add any
        arguments. The final parser must be returned.

        :param parser_adder: The return value of a call to
            ``argparse.ArgumentParser.add_subparsers()``
        '''

    @abstractmethod
    def do_run(self, args, unknown):
        '''Subclasses must implement; called to run the command.

        :param args: ``argparse.Namespace`` of parsed arguments
        :param unknown: If ``accepts_unknown_args`` is true, a
            sequence of un-parsed argument strings.
        '''

    #
    # Public API, mostly for subclasses.
    #
    # These are meant to be useful to subclasses during their do_run()
    # calls. Using this functionality outside of a WestCommand
    # subclass leads to undefined results.
    #

    @property
    def has_manifest(self):
        '''Property which is True if self.manifest is safe to access.
        '''
        return self._manifest is not None

    @property
    def manifest(self):
        '''Property for the manifest which was passed to run().

        If `do_run` was given a *manifest* kwarg, it is returned.
        Otherwise, a fatal error occurs.
        '''
        if self._manifest is None:
            log.die(f"can't run west {self.name};",
                    "it requires the manifest, which was not available.",
                    'Try "west manifest --validate" to debug.')
        return self._manifest

    @manifest.setter
    def manifest(self, manifest):
        self._manifest = manifest

#
# Private extension API
#
# This is used internally by main.py but should be considered an
# implementation detail.
#

class WestExtCommandSpec:
    # An object which allows instantiating a west extension.

    def __init__(self, name, project, help, factory):
        self.name = name
        '''Command name, as known to the user.'''

        self.project = project
        '''west.manifest.Project instance which defined the command.'''

        self.help = help
        '''Help string in west-commands.yml, or a default value.'''

        self.factory = factory
        '''"Factory" callable for the command.

        This returns a WestCommand instance when called.
        It may do some additional steps (like importing the definition of
        the command) before constructing it, however.'''

def extension_commands(manifest=None):
    # Get descriptions of available extension commands.
    #
    # The return value is an ordered map from project paths to lists of
    # WestExtCommandSpec objects, for projects which define extension
    # commands. The map's iteration order matches the manifest.projects
    # order.
    #
    # The return value is empty if configuration option
    # ``commands.allow_extensions`` is false.
    #
    # :param manifest: a parsed ``west.manifest.Manifest`` object, or None
    #                  to reload a new one.

    allow_extensions = _config.getboolean('commands', 'allow_extensions',
                                          fallback=True)
    if not allow_extensions:
        return {}

    if manifest is None:
        manifest = Manifest.from_file()

    specs = OrderedDict()
    for project in manifest.projects:
        if project.west_commands:
            specs[project.path] = _ext_specs(project)
    return specs

def _ext_specs(project):
    # Get a list of WestExtCommandSpec objects for the given
    # west.manifest.Project.

    ret = []

    # As an internal implementation detail, we allow the internal
    # representation of a project to have sequences as their
    # west_commands attribute value. This is required for manifest
    # imports, where e.g. the final ManifestProject.west_commands
    # might contain the results of importing multiple repositories.
    if isinstance(project.west_commands, str):
        commands = [project.west_commands]
    else:
        commands = list(project.west_commands)
    for cmd in commands:
        spec_file = os.path.join(project.abspath, cmd)

        # Verify project.west_commands isn't trying a directory traversal
        # outside of the project.
        if escapes_directory(spec_file, project.abspath):
            raise ExtensionCommandError(
                f'west-commands file {project.west_commands} '
                f'escapes project path {project.path}')

        # The project may not be cloned yet, or this might be coming
        # from a manifest that was copy/pasted into a self import
        # location.
        if not os.path.exists(spec_file):
            continue

        # Load the spec file and check the schema.
        with open(spec_file, 'r') as f:
            try:
                commands_spec = yaml.safe_load(f.read())
            except yaml.YAMLError as e:
                raise ExtensionCommandError from e
        try:
            pykwalify.core.Core(
                source_data=commands_spec,
                schema_files=[_EXT_SCHEMA_PATH]).validate()
        except pykwalify.errors.SchemaError as e:
            raise ExtensionCommandError from e

        for commands_desc in commands_spec['west-commands']:
            ret.extend(_ext_specs_from_desc(project, commands_desc))
    return ret

def _ext_specs_from_desc(project, commands_desc):
    py_file = os.path.join(project.abspath, commands_desc['file'])

    # Verify the YAML's python file doesn't escape the project directory.
    if escapes_directory(py_file, project.abspath):
        raise ExtensionCommandError(
            f'extension command python file "{commands_desc["file"]}" '
            f'escapes project path {project.path}')

    # Create the command thunks.
    thunks = []
    for command_desc in commands_desc['commands']:
        name = command_desc['name']
        attr = command_desc.get('class', name)
        help = command_desc.get('help',
                                f'(no help provided; try "west {name} -h")')
        factory = _ExtFactory(py_file, name, attr)
        thunks.append(WestExtCommandSpec(name, project, help, factory))

    # Return the thunks for this project.
    return thunks

def _commands_module_from_file(file):
    # Python magic for importing a module containing west extension
    # commands. To avoid polluting the sys.modules key space, we put
    # these modules in an (otherwise unpopulated) west.commands.ext
    # package.
    #
    # The file is imported as a module named
    # west.commands.ext.A_FRESH_IDENTIFIER. This module object is
    # returned from a cache if the same file is ever imported again,
    # to avoid a double import in case the file maintains module-level
    # state or defines multiple commands.
    global _EXT_MODULES_CACHE
    global _EXT_MODULES_NAME_IT

    file = os.path.normcase(os.path.realpath(file))
    if file in _EXT_MODULES_CACHE:
        return _EXT_MODULES_CACHE[file]

    mod_name = next(_EXT_MODULES_NAME_IT)
    spec = importlib.util.spec_from_file_location(mod_name, file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _EXT_MODULES_CACHE[file] = mod

    return mod

_EXT_SCHEMA_PATH = os.path.join(os.path.dirname(__file__),
                                'west-commands-schema.yml')

# Cache which maps files implementing extension commands to their
# imported modules.
_EXT_MODULES_CACHE = {}
# Infinite iterator of "fresh" extension command module names.
_EXT_MODULES_NAME_IT = (f'west.commands.ext.cmd_{i}'
                        for i in itertools.count(1))

class _ExtFactory:

    def __init__(self, py_file, name, attr):
        self.py_file = py_file
        self.name = name
        self.attr = attr

    def __call__(self):
        # Append the python file's directory to sys.path. This lets
        # its code import helper modules in a natural way.
        py_dir = os.path.dirname(self.py_file)
        sys.path.append(py_dir)

        # Load the module containing the command. Convert only
        # expected exceptions to ExtensionCommandError.
        try:
            mod = _commands_module_from_file(self.py_file)
        except ImportError as ie:
            raise ExtensionCommandError(
                hint=f'could not import {self.py_file}') from ie

        # Get the attribute which provides the WestCommand subclass.
        try:
            cls = getattr(mod, self.attr)
        except AttributeError as ae:
            raise ExtensionCommandError(
                hint=f'no attribute {self.attr} in {self.py_file}') from ae

        # Create the command instance and return it.
        try:
            return cls()
        except Exception as e:
            raise ExtensionCommandError(
                hint='command constructor threw an exception') from e
