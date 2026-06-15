#!/bin/bash
# Open the Teleprompter Mirror cast page as a standalone Chrome app window.
# Requires mirror-server.py to be running (start it with ./start-mirror.sh).

PORT=${1:-8047}
URL="http://localhost:$PORT/cast"
WM_CLASS=teleprompter-mirror

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

exec google-chrome --app="$URL" --class="$WM_CLASS" 2>/dev/null
