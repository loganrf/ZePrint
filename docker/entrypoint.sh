#!/bin/sh
# Runs as root just long enough to:
#   - match the zeprint user to PUID/PGID (default 1000) and own the data dir
#   - grant that user the group of any USB printer device passed in with --device
# then drops privileges. Started as a non-root user, it just execs the command.
set -e

DATA="${ZEPRINT_DATA_DIR:-/data}"

if [ "$(id -u)" = "0" ]; then
    PUID="${PUID:-1000}"
    PGID="${PGID:-1000}"
    [ "$(id -g zeprint)" = "$PGID" ] || groupmod -o -g "$PGID" zeprint
    [ "$(id -u zeprint)" = "$PUID" ] || usermod -o -u "$PUID" zeprint

    mkdir -p "$DATA"
    if [ "$(stat -c %u "$DATA")" != "$PUID" ]; then
        chown -R zeprint:zeprint "$DATA"
    fi

    for dev in /dev/usb/lp* /dev/lp*; do
        [ -e "$dev" ] || continue
        gid="$(stat -c %g "$dev")"
        group="$(getent group "$gid" | cut -d: -f1)"
        if [ -z "$group" ]; then
            group="printdev$gid"
            groupadd -o -g "$gid" "$group"
        fi
        usermod -a -G "$group" zeprint
        echo "entrypoint: $dev (group $group) is available to zeprint"
    done

    exec setpriv --reuid=zeprint --regid=zeprint --init-groups "$@"
fi

exec "$@"
