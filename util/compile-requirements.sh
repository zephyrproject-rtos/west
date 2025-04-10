#!/bin/bash

if ! command -v uv 2>&1 >/dev/null
then
    echo "uv command could not be found"
    echo "See https://docs.astral.sh/uv/ for installation instructions"
    exit 1
fi

common_args="--quiet --upgrade --universal --python-version 3.9 --generate-hashes"

uv python install 3.9 3.10 3.11 3.12 3.13
uv pip compile $common_args -o requirements.txt pyproject.toml
uv pip compile $common_args --group tox -o requirements-tox.txt pyproject.toml
echo -e "build\nsetuptools" | uv pip compile $common_args -o requirements-install-build.txt -
echo "tox" | uv pip compile $common_args -o requirements-install-tox.txt -
