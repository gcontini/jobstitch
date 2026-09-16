#!/bin/sh
# Container entrypoint for the jobstitch API server.
#
# The server is stateless: it mounts nothing, seeds nothing and writes nothing
# outside its scratch directory. This script only makes sure that directory
# exists and is writable before handing over to the command.
#
# To run with edited prompts or a different template, mount them and set
# JOBSTITCH_RESOURCES; anything missing there falls back to the copy baked
# into the image.
set -eu

WORK="${JOBSTITCH_WORK_DIR:-/tmp/jobstitch}"

if ! mkdir -p "$WORK" 2>/dev/null; then
    echo "✗ cannot create the scratch directory $WORK as uid $(id -u):$(id -g)." >&2
    echo "  Set JOBSTITCH_WORK_DIR to a writable path." >&2
    exit 1
fi

if [ -n "${JOBSTITCH_RESOURCES:-}" ] && [ ! -d "${JOBSTITCH_RESOURCES}" ]; then
    echo "✗ JOBSTITCH_RESOURCES=$JOBSTITCH_RESOURCES is not a directory." >&2
    exit 1
fi

echo "📦 resources: ${JOBSTITCH_RESOURCES:-baked into the image}"
echo "📂 scratch  : $WORK"

exec "$@"
