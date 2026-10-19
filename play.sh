#!/bin/sh
# Ball Balance launcher: downloads (or updates) the game into ~/.ball-balance and runs it.
#
#   curl -fsSL https://raw.githubusercontent.com/Narek-versatile/ball-balance/main/play.sh | sh
#
# Options for the game go after `sh -s --`, e.g.   ... | sh -s -- --keys
set -e

REPO="Narek-versatile/ball-balance"
DIR="${BALL_BALANCE_HOME:-$HOME/.ball-balance}"

PY=""
for cmd in python3 python; do
    if command -v "$cmd" >/dev/null 2>&1 && "$cmd" -c 'import sys; sys.exit(sys.version_info < (3, 8))' 2>/dev/null; then
        PY="$cmd"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "Ball Balance needs Python 3.8 or newer: https://www.python.org/downloads/" >&2
    exit 1
fi

echo "Downloading Ball Balance into $DIR ..."
mkdir -p "$DIR"
curl -fsSL "https://github.com/$REPO/archive/refs/heads/main.tar.gz" | tar -xzf - -C "$DIR" --strip-components=1

# This script usually arrives through a pipe, so hand the game the real keyboard.
if [ -t 0 ]; then
    exec "$PY" "$DIR/ball_balance.py" "$@"
elif (: < /dev/tty) 2>/dev/null; then
    exec "$PY" "$DIR/ball_balance.py" "$@" < /dev/tty
else
    exec "$PY" "$DIR/ball_balance.py" "$@"
fi
