#!/bin/bash
# Open the Teleprompter Mirror cast page as a standalone browser app window.
# Requires the mirror server to be running (start it with bin/start-mirror.sh).

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
# shellcheck source=../lib/config.sh
. "$PROJECT_DIR/lib/config.sh"

PORT=${1:-$TELEPROMPTER_PORT}
URL="http://localhost:$PORT/cast"

if ! curl -s -o /dev/null --connect-timeout 1 "$URL"; then
    MSG="Mirror server is not running on port $PORT.

Start it with:
  systemctl --user start teleprompter-mirror

Check status with:
  systemctl --user status teleprompter-mirror"
    echo "$MSG"
    zenity --error --title="Teleprompter Mirror" --text="$MSG" --no-wrap 2>/dev/null
    exit 1
fi

exec "$TELEPROMPTER_BROWSER" --app="$URL" 2>/dev/null
