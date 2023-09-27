#!/bin/bash
set -e

# This is the test script that runs in the containers themselves.

WEST=/west
# Replace semicolon with dash
WEST_TARGET=${WEST_TARGET//:/-}

WEST_TOX_OUT=$WEST_TOX_OUT/$WEST_TARGET
WEST_TOX_OUT_IN_HOST=$WEST_TOX_OUT_IN_HOST/$WEST_TARGET

die() {
    if [ $# -eq 0 ]; then
        echo "error: $*" >&2
    else
        echo "error: unknown error in $0" >&2
    fi
    exit 1
}

main()
{
    # Verify the container environment set up meets this script's requirements.
    [ -n "$WEST_TOX_OUT" ] || die "missing $WEST_TOX_OUT"
    [ -n "$WEST_TOX_OUT_IN_HOST" ] || die "missing $WEST_TOX_OUT_IN_HOST"
    [ -d "$WEST" ] || die "missing $WEST in the container"

    TOX_LOG="$WEST_TOX_OUT/tox.log"
    TOX_LOG_IN_HOST="$WEST_TOX_OUT_IN_HOST/tox.log"
    WEST_TESTDIR="/tmp/west"

    mkdir "$WEST_TOX_OUT"

    git clone -q "$WEST" "$WEST_TESTDIR" || die "failed to clone west to $WEST_TESTDIR in container"
    cd "$WEST_TESTDIR"

    echo "running tox, output in $TOX_LOG_IN_HOST in host"
    tox run >"$TOX_LOG" 2>&1 || die "tox failed, see $TOX_LOG"

    cp -R htmlcov "$WEST_TOX_OUT" || die "failed to copy coverage to $WEST_TOX_OUT_IN_HOST/htmlcov in host"
}

main "$@"
