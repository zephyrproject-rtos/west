#!/bin/bash

# This is the test script that runs in the containers themselves.

WEST=/west

die() {
    if [ ! -z "$@" ]; then
        echo "error: $@" >&2
    else
        echo "error: unknown error in $0"
    fi
    exit 1
}

main()
{
    # Verify the container environment set up meets this script's requirements.
    [ ! -z "$WEST_TOX_OUT" ] || die "missing $WEST_TOX_OUT"
    [ ! -z "$WEST_TOX_OUT_IN_HOST" ] || die "missing $WEST_TOX_OUT_IN_HOST"
    [ -d "$WEST" ] || die "missing $WEST in the container"

    TOX_LOG="$WEST_TOX_OUT/tox.log"
    TOX_LOG_IN_HOST="$WEST_TOX_OUT_IN_HOST/tox.log"
    WEST_TESTDIR="/tmp/west"

    mkdir "$WEST_TOX_OUT" || die "failed to make $WEST_TOX_OUT in container ($WEST_TOX_OUT_IN_HOST in host)"

    git clone -q "$WEST" "$WEST_TESTDIR" || die "failed to clone west to $WEST_TESTDIR in container"
    cd "$WEST_TESTDIR"

    echo "running tox, output in $TOX_LOG_IN_HOST in host"
    tox run >"$TOX_LOG" 2>&1 || die "tox failed"

    cp -R htmlcov "$WEST_TOX_OUT" || die "failed to copy coverage to $WEST_TOX_OUT_IN_HOST/htmlcov in host"
}

main "$@"
