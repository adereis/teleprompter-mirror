#!/usr/bin/env python3
"""Enable or disable a GNOME Shell extension by editing gsettings directly.

`gnome-extensions enable` goes through the running shell, which refuses a UUID
it has not loaded — exactly the case right after installing, since the shell
does not rescan its extensions directory and `ReloadExtension` is gone. This
writes the same settings the shell would, so the extension comes up at the
next login.

Enabling means two edits, not one: GNOME's own schema says
`disabled-extensions` "takes precedence over the enabled-extensions setting",
and the shell's EnableExtension clears it there as well. Adding to the enabled
list alone silently does nothing for an extension the user once switched off.

Standard library only; shells out to `gsettings`.

    extension-state.py enable|disable <uuid>
"""

import ast
import subprocess
import sys

SCHEMA = "org.gnome.shell"
ENABLED = "enabled-extensions"
DISABLED = "disabled-extensions"


def read_list(key, run=subprocess.check_output):
    """Return a gsettings string-array value as a Python list."""
    raw = run(("gsettings", "get", SCHEMA, key), text=True).strip()
    if raw.startswith("@as "):          # how gsettings prints an empty array
        raw = raw[len("@as "):].strip()
    if raw in ("", "[]"):
        return []
    value = ast.literal_eval(raw)
    return [str(item) for item in value]


def format_list(items):
    """Render a Python list as a gsettings string-array literal."""
    return "[" + ", ".join("'" + item.replace("'", r"\'") + "'" for item in items) + "]"


def plan(uuid, enable, enabled, disabled):
    """Return the {key: new value} edits needed, skipping no-op writes."""
    edits = {}
    if enable and uuid not in enabled:
        edits[ENABLED] = enabled + [uuid]
    if not enable and uuid in enabled:
        edits[ENABLED] = [item for item in enabled if item != uuid]
    # Always clear the override: it wins over the enabled list, and leaving a
    # stale entry behind would make the next install look successful and do
    # nothing.
    if uuid in disabled:
        edits[DISABLED] = [item for item in disabled if item != uuid]
    return edits


def main(argv):
    if len(argv) != 2 or argv[0] not in ("enable", "disable"):
        print(__doc__.strip().splitlines()[-1].strip(), file=sys.stderr)
        return 2
    action, uuid = argv
    edits = plan(uuid, action == "enable", read_list(ENABLED), read_list(DISABLED))
    for key, items in edits.items():
        subprocess.run(("gsettings", "set", SCHEMA, key, format_list(items)), check=True)
    print(f"{action}d {uuid}" if edits else f"{uuid} already {action}d")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
