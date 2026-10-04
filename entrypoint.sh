#!/bin/sh
# Fix the volume's ownership, then drop privileges.
#
# A mounted volume arrives owned by root regardless of what the image did at
# build time: the mount replaces the directory the Dockerfile created and
# chowned. A container that has already dropped to a non-root user therefore
# cannot write to its own data directory, and the first thing that fails is
# storage initialisation at startup -- which on Railway looks like
#
#   [Errno 13] Permission denied: '/app/data/media'
#
# So the container starts as root, makes the directories and hands them to
# the application user, and only then drops privileges for everything that
# follows. The application itself never runs as root.
set -e

DATA_DIR="${DATA_DIR:-/app/data}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR/media" "$DATA_DIR/work" "$DATA_DIR/cache"
    # Only the data directory. Chowning all of /app on every boot would walk
    # the whole image for no reason.
    chown -R echoes:echoes "$DATA_DIR" || {
        echo "entrypoint: could not take ownership of $DATA_DIR" >&2
        echo "entrypoint: if this is a mounted volume, check its mount path" >&2
        exit 1
    }
    exec gosu echoes "$@"
fi

# Already unprivileged (someone set USER, or the platform runs as non-root).
# Nothing to fix and nothing to drop.
exec "$@"
