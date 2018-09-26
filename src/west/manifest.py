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
    _validate_manifest(manifest_path)

    with open(manifest_path) as f:
        manifest = yaml.safe_load(f)['manifest']

    projects = []
    # Manifest "defaults" keys whose values get copied to each project
    # that doesn't specify its own value.
    project_defaults = ('remote', 'revision')

    # mp = manifest project (dictionary with values parsed from the manifest)
    for mp in manifest['projects']:
        # Fill in any missing fields in 'mp' with values from the 'defaults'
        # dictionary
        if 'defaults' in manifest:
            for key, val in manifest['defaults'].items():
                if key in project_defaults:
                    mp.setdefault(key, val)

        # Add the repository URL to 'mp'
        for remote in manifest['remotes']:
            if remote['name'] == mp['remote']:
                mp['url'] = remote['url'] + '/' + mp['name']
                break
        else:
            log.die('Remote {} not defined in {}'
                    .format(mp['remote'], manifest_path))

        # If no clone path is specified, the project's name is used
        clone_path = mp.get('path', mp['name'])

        # Use named tuples to store project information. That gives nicer
        # syntax compared to a dict (project.name instead of project['name'],
        # etc.)
        projects.append(Project(
            mp['name'],
            mp['url'],
            # If no revision is specified, 'master' is used
            mp.get('revision', 'master'),
            clone_path,
            # Absolute clone path
            os.path.join(util.west_topdir(), clone_path),
            # If no clone depth is specified, we fetch the entire history
            mp.get('clone-depth', None)))

    return projects


def _validate_manifest(manifest_path):
    # Validates the manifest with pykwalify.

    try:
        pykwalify.core.Core(
            source_file=manifest_path,
            schema_files=[_SCHEMA_PATH]
        ).validate()
    except pykwalify.errors.SchemaError as e:
        _malformed(manifest_path, e)

    return manifest_data['manifest']


def _malformed(manifest_path, error_info):
    raise MalformedManifest('{} malformed (schema: {}):\n{}'
                            .format(manifest_path, _SCHEMA_PATH, error_info))


_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "manifest-schema.yml")
