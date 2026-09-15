#!/bin/bash
# Run both jobstitch processes in one container:
#
#   job-watcher       background — picks up job folders and generates the CVs
#   clipboard-import  foreground — it prompts on the terminal, so it needs one
#                                  (run the container with -it)
#
# Any arguments are passed to job-watcher:
#
#     docker run ... jobstitch start-all.sh --temperature=0.4
#
# Quitting the intake loop ('q') leaves the watcher running; the container
# stops when the watcher does. Start another intake session at any time with:
#
#     docker exec -it <container> clipboard-import
#
# Set JOBSTITCH_CLIPBOARD=off to run the watcher alone — on a machine with no
# clipboard, feed it job folders from your own JD source instead.
set -u

# Set by the watchdog below, read by shutdown(), so that a container that dies
# because the watcher died reports a failure rather than a clean exit.
died="${TMPDIR:-/tmp}/jobstitch-watcher-died"
rm -f "$died"

job-watcher "$@" &
watcher=$!

shutdown() {
    kill -TERM "$watcher" 2>/dev/null
    wait "$watcher" 2>/dev/null
    [ -e "$died" ] && exit 1
    exit 0
}
trap shutdown INT TERM

if [ "${JOBSTITCH_CLIPBOARD:-on}" = "off" ]; then
    echo "📋 clipboard intake off (JOBSTITCH_CLIPBOARD=off) — watcher only."
    wait "$watcher"
    exit $?
fi

# Take the container down if the watcher dies while the intake loop holds the
# foreground: a container that has quietly stopped generating CVs is worse
# than one that exits.
(
    while kill -0 "$watcher" 2>/dev/null; do sleep 5; done
    : > "$died"
    echo "✗ job-watcher exited — stopping the container." >&2
    kill -TERM "$$" 2>/dev/null
) &
guard=$!

clipboard-import || echo "⚠ clipboard-import exited with status $?." >&2

kill -TERM "$guard" 2>/dev/null
echo "ℹ job-watcher is still running. For another intake session:" >&2
echo "    docker exec -it <container> clipboard-import" >&2
wait "$watcher"
