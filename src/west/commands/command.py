# Copyright 2018 Open Source Foundries Limited.
# Copyright 2019 Foundries.io Limited.
#
# SPDX-License-Identifier: Apache-2.0

from abc import ABC, abstractmethod
from collections import OrderedDict
import importlib
from keyword import iskeyword
import os
import sys

import pykwalify
import yaml

from west.manifest import Manifest
from west.util import escapes_directory


class CommandError(RuntimeError):
    '''Indicates that a command failed. The return code attribute
    specifies the error code to return to the system'''

    def __init__(self, returncode=1):
        super().__init__()
        self.returncode = returncode


class CommandContextError(CommandError):
    '''Indicates that a context-dependent command could not be run.'''


class BadExternalCommand(Exception):
    '''Exception class indicating an external command was badly
    defined and could not be created.'''


class WestCommand(ABC):
    '''Abstract superclass for a west command.

    All top-level commands supported by west implement this interface.'''

    def __init__(self, name, help, description, accepts_unknown_args=False):
        '''Create a command instance.

        Some of the fields in a WestCommand (such as `name`, `help`, and
        `description`) overlap with kwargs that should be passed to the
        argparse.ArgumentParser which should be added in the
        `add_parser()` method. This wart is by design: argparse
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
        '''
        self.name = name
        self.help = help
        self.description = description
        self._accept_unknown = accepts_unknown_args

    def run(self, args, unknown):
        '''Run the command.

        This raises CommandContextError if the command cannot be run
        due to a context mismatch. Other exceptions may be raised as
        well.

        :param args: known arguments parsed via `add_parser()`.
        :param unknown: unknown arguments present on the command line;
                        this must be empty if the constructor
                        was passed accepts_unknown_args=False.
        '''
        if unknown and not self._accept_unknown:
            self.parser.error('unexpected arguments: {}'.format(unknown))
        self.do_run(args, unknown)

    def add_parser(self, parser_adder):
        '''Registers a parser for this command, and returns it.

        The parser object is stored in the `parser` attribute of this
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
        :param unknown: If `accepts_unknown_args` was False when constructing
                        this object, this parameter is an empty sequence.
                        Otherwise, it is an iterable containing all unknown
                        arguments present on the command line.'''


class WestExtCommandSpec:
    '''An object which allows instantiating an external west command.'''

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


def external_commands(manifest=None):
    '''Get descriptions of available external commands.

    The return value is an ordered map from project paths to lists of
    WestExtCommandSpec objects, for projects which define external
    commands. The map's iteration order matches the manifest.projects
    order.

    :param manifest: a parsed `west.manifest.Manifest` object, or None
                     to reload a new one.
    '''
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
        raise BadExternalCommand(
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
            raise BadExternalCommand from e
    try:
        pykwalify.core.Core(
            source_data=commands_spec,
            schema_files=[_EXT_SCHEMA_PATH]).validate()
    except pykwalify.errors.SchemaError as e:
        raise BadExternalCommand from e

    ret = []
    for commands_desc in commands_spec['west-commands']:
        ret.extend(_ext_specs_from_desc(project, commands_desc))
    return ret


def _ext_specs_from_desc(project, commands_desc):
    py_file = os.path.join(project.abspath, commands_desc['file'])

    # Verify the YAML's python file doesn't escape the project directory.
    if escapes_directory(py_file, project.abspath):
        raise BadExternalCommand(
            'external command python file "{}" escapes project path {}'.
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


def _commands_module_from_file(command_name, file):
    # Python magic for importing a module containing west extension
    # commands. To avoid polluting the sys.modules key space, we put
    # these modules in an (otherwise unpopulated) west.commands.ext
    # package.
    #
    # The file is imported as a module named
    # west.commands.ext.<command_name>, so the name must be a
    # non-keyword identifier. This module object is returned from a
    # cache if the same file is ever imported again, to avoid a double
    # import in case the file maintains module-level state or defines
    # multiple commands.
    #
    # (This shouldn't be a problem for west itself since main.py
    # executes at most one WestCommand -- and creates at most one
    # external command -- per process, but better safe than sorry in
    # case this code gets called from elsewhere.)
    #
    # If we ever try to re-use a command_name, a ValueError() is
    # raised; this prevents the import machinery from returning a
    # previously cached module from sys.modules for an unrelated file,
    # and is a decent sanity check against two providers of the same
    # command.
    global _EXT_MODULES_CACHE
    mod_name = 'west.commands.ext.{}'.format(command_name)

    if not command_name.isidentifier() or iskeyword(command_name):
        raise ValueError('bad command name "{}"'.format(command_name))
    elif mod_name in sys.modules:
        raise ValueError('command {} is already defined'.format(
            mod_name.rsplit('.', 1)[-1]))

    file = os.path.normcase(os.path.realpath(file))

    if file in _EXT_MODULES_CACHE:
        return _EXT_MODULES_CACHE[file]

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


_EXT_MODULES_CACHE = {}


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
        # expected exceptions to BadExternalCommand.
        try:
            mod = _commands_module_from_file(self.name, self.py_file)
        except ValueError as ve:
            raise BadExternalCommand from ve
        except ImportError as ie:
            raise BadExternalCommand from ie

        # Get the attribute which provides the WestCommand subclass.
        cls = getattr(mod, self.attr)

        # Create the command instance and return it.
        return cls()
