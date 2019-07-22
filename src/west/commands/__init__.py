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

All built-in west commands are implemented as modules in this package.
This package also provides support for extension commands.'''

__all__ = ['CommandContextError', 'CommandError', 'WestCommand',
           'extension_commands', 'WestExtCommandSpec', 'ExtensionCommandError']

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


_NO_TOPDIR_MSG_FMT = '''\
no west installation found from "{}"; "west {}" requires one.
Things to try:
 - Set ZEPHYR_BASE to a zephyr repository path in a west installation.
 - Run "west init" to set up an installation here.
 - Run "west init -h" for additional information.
'''

class WestCommand(ABC):
    '''Abstract superclass for a west command.'''

    def __init__(self, name, help, description, accepts_unknown_args=False,
                 requires_installation=True):
        '''Abstract superclass for a west command.

        Some of the fields in a WestCommand (such as *name*, *help*, and
        *description*) overlap with kwargs that should be passed to the
        argparse.ArgumentParser which should be added in the
        `WestCommand.add_parser()` method. This wart is by design: argparse
        doesn't make many API stability guarantees, so some of this
        information must be duplicated to work reliably in the future.

        :param name: the command's name, as entered by the user
        :param help: one-line command help text
        :param description: multi-line command description
        :param accepts_unknown_args: if true, the command can handle
                                     arbitrary unknown command line arguments
                                     in its run() method. Otherwise, passing
                                     unknown arguments will cause
                                     UnknownArgumentsError to be raised.
        :param requires_installation: if true, the command requires a
                                      west installation to run. Running such a
                                      command outside of any installation
                                      (i.e. without a valid topdir) will exit
                                      with an error.
        '''
        self.name = name
        self.help = help
        self.description = description
        self.accepts_unknown_args = accepts_unknown_args
        self.requires_installation = requires_installation

    def run(self, args, unknown, topdir):
        '''Run the command.

        This raises `CommandContextError` if the command cannot be run
        due to a context mismatch. Other exceptions may be raised as
        well.

        :param args: known arguments parsed via `WestCommand.add_parser()`.
        :param unknown: unknown arguments present on the command line;
                        this must be empty if the constructor
                        was passed accepts_unknown_args=False.
        :param topdir: top level directory of west installation, or None;
                       this is stored as an attribute before do_run() is
                       called.
        '''
        if unknown and not self.accepts_unknown_args:
            self.parser.error('unexpected arguments: {}'.format(unknown))
        if not topdir and self.requires_installation:
            log.die(_NO_TOPDIR_MSG_FMT.format(os.getcwd(), self.name))
        self.topdir = topdir
        self.do_run(args, unknown)

    def add_parser(self, parser_adder):
        '''Registers a parser for this command, and returns it.

        The parser object is stored in the ``parser`` attribute of this
        WestCommand.

        :param parser_adder: The return value of a call to
                             argparse.ArgumentParser.add_subparsers()
        '''
        self.parser = self.do_add_parser(parser_adder)

        if self.parser is None:
            raise ValueError('no parser was returned')

        return self.parser

    #
    # Mandatory subclass hooks
    #

    @abstractmethod
    def do_add_parser(self, parser_adder):
        '''Subclass method for registering command line arguments.

        This is called by WestCommand.add_parser() to do the work of
        adding the parser itself.

        The subclass should call parser_adder.add_parser() to add an
        ArgumentParser for that subcommand, then add any
        arguments. The final parser must be returned.

        :param parser_adder: The return value of a call to
                             argparse.ArgumentParser.add_subparsers()

        '''

    @abstractmethod
    def do_run(self, args, unknown):
        '''Subclasses must implement; called when the command is run.

        :param args: is the namespace of parsed known arguments.
        :param unknown: If ``accepts_unknown_args`` was False when constructing
                        this object, this parameter is an empty sequence.
                        Otherwise, it is an iterable containing all unknown
                        arguments present on the command line.'''

class WestExtCommandSpec:
    '''An object which allows instantiating an extension west command.'''

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
    '''Get descriptions of available extension commands.

    The return value is an ordered map from project paths to lists of
    WestExtCommandSpec objects, for projects which define extension
    commands. The map's iteration order matches the manifest.projects
    order.

    The return value is empty if configuration option
    ``commands.allow_extensions`` is false.

    :param manifest: a parsed ``west.manifest.Manifest`` object, or None
                     to reload a new one.
    '''
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

    spec_file = os.path.join(project.abspath, project.west_commands)

    # Verify project.west_commands isn't trying a directory traversal
    # outside of the project.
    if escapes_directory(spec_file, project.abspath):
        raise ExtensionCommandError(
            'west-commands file {} escapes project path {}'.
            format(project.west_commands, project.path))

    # Project may not be cloned yet.
    if not os.path.exists(spec_file):
        return []

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

    ret = []
    for commands_desc in commands_spec['west-commands']:
        ret.extend(_ext_specs_from_desc(project, commands_desc))
    return ret

def _ext_specs_from_desc(project, commands_desc):
    py_file = os.path.join(project.abspath, commands_desc['file'])

    # Verify the YAML's python file doesn't escape the project directory.
    if escapes_directory(py_file, project.abspath):
        raise ExtensionCommandError(
            'extension command python file "{}" escapes project path {}'.
            format(commands_desc['file'], project.path))

    # Create the command thunks.
    thunks = []
    for command_desc in commands_desc['commands']:
        name = command_desc['name']
        attr = command_desc.get('class', name)
        help = command_desc.get(
            'help',
            '(no help provided; try "west {} -h")'.
            format(name))
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
    # The Python 3.4 way to import a module given its file got deprecated
    # later on, but we still need to support 3.4. If that requirement ever
    # gets dropped, this code can be simplified.
    if (3, 4) <= sys.version_info < (3, 5):
        def _import_mod_from(mod_name, file):
            from importlib.machinery import SourceFileLoader
            return SourceFileLoader(mod_name, file).load_module()
    else:
        def _import_mod_from(mod_name, file):
            spec = importlib.util.spec_from_file_location(mod_name, file)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

    mod = _import_mod_from(mod_name, file)
    _EXT_MODULES_CACHE[file] = mod

    return mod


_EXT_SCHEMA_PATH = os.path.join(os.path.dirname(__file__),
                                'west-commands-schema.yml')

# Cache which maps files implementing extension commands their
# imported modules.
_EXT_MODULES_CACHE = {}
# Infinite iterator of "fresh" extension command module names.
_EXT_MODULES_NAME_IT = ('west.commands.ext.cmd_{}'.format(i)
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
                hint='could not import {}'.format(self.py_file)) from ie

        # Get the attribute which provides the WestCommand subclass.
        try:
            cls = getattr(mod, self.attr)
        except AttributeError as ae:
            hint = 'no attribute {} in {}'.format(self.attr,
                                                  self.py_file)
            raise ExtensionCommandError(hint=hint) from ae

        # Create the command instance and return it.
        try:
            return cls()
        except Exception as e:
            raise ExtensionCommandError(
                hint='command constructor threw an exception') from e
