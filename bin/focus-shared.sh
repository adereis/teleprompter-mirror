#!/bin/bash
# Raise the window that is being mirrored to the tablet.
#
# Backs the "Focus Shared Window" action on the desktop entry and is the
# script to bind to a custom keyboard shortcut. It resolves the same target
# the cast page's button uses (remembered in
# ~/.config/teleprompter-mirror/focus-target.json, or matched against
# TELEPROMPTER_FOCUS_MATCH), so every entry point raises the same window.
#
# Does not need the mirror server — it talks to the GNOME Shell extension
# directly.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
# shellcheck source=../lib/config.sh
. "$PROJECT_DIR/lib/config.sh"

if OUTPUT=$(python3 "$PROJECT_DIR/lib/window_focus.py" focus 2>&1); then
    echo "$OUTPUT"
    exit 0
fi

# Launched from a desktop action or a keybinding there is nowhere to read
# stderr, so say it on screen too.
echo "$OUTPUT" >&2
zenity --error --title="Teleprompter Mirror" --no-wrap \
    --text="Could not focus the shared window.

$OUTPUT" 2>/dev/null
exit 1
