# Copyright (c) 2018, Nordic Semiconductor ASA
# Copyright 2018, Foundries.io Ltd
#
# SPDX-License-Identifier: Apache-2.0

import collections
import os

import pykwalify.core
import yaml

from west import util

Project = collections.namedtuple(
    'Project',
    'name url revision path abspath clone_depth')
'''Holds information about a project, taken from the manifest file.'''


class MalformedManifest(RuntimeError):
    pass


def manifest_projects(manifest_path):
    '''Return a list of Project instances for the given manifest path.'''
    def _malformed_manifest(msg):
        _malformed(manifest_path, msg)

    manifest = _validated_manifest(manifest_path)

    projects = []

    # Get the defaults object out of the manifest, or provide one.
    defaults = manifest.get('defaults', {'revision': 'master'})
    default_rev = defaults.get('revision')
    default_remote = defaults.get('remote')

    # Map from each remote's name onto that remote's data in the manifest.
    remote_dict = {r['name']: r for r in manifest['remotes']}

    # The default remote, if specified, must be defined.
    if default_remote is not None and default_remote not in remote_dict:
        _malformed_manifest('default remote {} is not defined'.
                            format(default_remote))

    # mp = manifest project (dictionary with values parsed from the manifest)
    for mp in manifest['projects']:
        # Validate the project remote.
        mpr = mp.get('remote') or defaults.get('remote')
        if mpr is None:
            _malformed_manifest('project {} does not specify a remote'.
                                format(mp['name']))
        if mpr not in remote_dict:
            _malformed_manifest('project {} remote {} is not defined'.
                                format(mp['name'], mp['remote']))

        # If no clone path is specified, the project's name is used
        clone_path = mp.get('path', mp['name'])

        # Use named tuples to store project information. That gives nicer
        # syntax compared to a dict (project.name instead of project['name'],
        # etc.)
        projects.append(Project(
            mp['name'],
            # The project repository URL is formed by concatenating the
            # remote URL with the project name.
            remote_dict[mpr]['url'] + '/' + mp['name'],
            # The project revision is defined in its entry or given by a
            # default value.
            mp.get('revision', default_rev),
            clone_path,
            # Absolute clone path
            os.path.join(util.west_topdir(), clone_path),
            # If no clone depth is specified, we fetch the entire history
            mp.get('clone-depth', None)))

    return projects


def _validated_manifest(manifest_path):
    # Validates the manifest with pykwalify.
    with open(manifest_path, 'r') as f:
        manifest_data = yaml.safe_load(f.read())

    if not manifest_data:
        _malformed(manifest_path, 'No YAML content was found.')

    try:
        pykwalify.core.Core(
            source_data=manifest_data,
            schema_files=[_SCHEMA_PATH]
        ).validate()
    except pykwalify.errors.SchemaError as e:
        _malformed(manifest_path, e)

    return manifest_data['manifest']


def _malformed(manifest_path, error_info):
    raise MalformedManifest('{} malformed (schema: {}):\n{}'
                            .format(manifest_path, _SCHEMA_PATH, error_info))


_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "manifest-schema.yml")
