#!/bin/bash
# Open the Teleprompter Mirror cast page as a standalone Chrome app window.
# Requires mirror-server.py to be running (start it with ./start-mirror.sh).

PORT=${1:-8047}
URL="http://localhost:$PORT/cast"
WM_CLASS=teleprompter-mirror

if ! curl -s -o /dev/null --connect-timeout 1 "$URL"; then
    echo "Mirror server not running on port $PORT."
    echo "Start it first:  ./start-mirror.sh usb"
    exit 1
fi

exec google-chrome --app="$URL" --class="$WM_CLASS" 2>/dev/null
