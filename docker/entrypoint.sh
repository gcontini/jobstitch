#!/bin/sh
# Container entrypoint: prepare the mounted folder, then run the command
# (by default docker/start-all.sh, which launches both processes).
#
#     $JOBSTITCH_DATA/        the folder you mount, /data by default
#         applications_cv/    JOBSTITCH_HOME — incoming, working, error, resume, xlsx
#         resources/          JOBSTITCH_RESOURCES — templates, prompts, models.toml, CV data
#
# resources/ is seeded from the pristine copy baked into the image at every
# launch: a file you do not have yet is copied in, a file you already have is
# left exactly as it is. So editing a prompt or putting your real
# candidate_profile.json in there survives every restart and every rebuild.
set -eu

DATA="${JOBSTITCH_DATA:-/data}"
SEED="${JOBSTITCH_SEED_RESOURCES:-/opt/jobstitch/resources}"

# The layout inside the container is fixed rather than inherited: a
# JOBSTITCH_HOME left over in the .env you pass at launch points at a path on
# your host, and must not send the watcher outside the mounted folder.
JOBSTITCH_HOME="$DATA/applications_cv"
JOBSTITCH_RESOURCES="$DATA/resources"
export JOBSTITCH_HOME JOBSTITCH_RESOURCES

if [ ! -d "$DATA" ]; then
    echo "✗ $DATA does not exist — mount your jobstitch folder there:" >&2
    echo "    docker run -it --rm --env-file .env -v /path/to/jobstitch-data:/data jobstitch" >&2
    exit 1
fi

if ! mkdir -p "$JOBSTITCH_HOME" "$JOBSTITCH_RESOURCES" 2>/dev/null; then
    echo "✗ cannot write to $DATA as uid $(id -u):$(id -g)." >&2
    echo "  Run the container as yourself: --user \"\$(id -u):\$(id -g)\"" >&2
    exit 1
fi

copied=0
kept=0
for src in "$SEED"/*; do
    [ -e "$src" ] || continue          # empty seed folder: nothing to copy
    dst="$JOBSTITCH_RESOURCES/${src##*/}"
    if [ -e "$dst" ]; then
        kept=$((kept + 1))
    else
        cp -a "$src" "$dst"
        copied=$((copied + 1))
    fi
done

echo "📦 resources: $copied seeded from the image, $kept already yours -> $JOBSTITCH_RESOURCES"
echo "📂 workspace: $JOBSTITCH_HOME"

exec "$@"
