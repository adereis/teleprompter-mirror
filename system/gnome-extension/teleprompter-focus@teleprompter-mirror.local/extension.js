// D-Bus bridge that lets the Teleprompter Mirror tools list windows and raise
// the one being cast to the tablet.
//
// Why an extension at all: GNOME restricts
// org.gnome.Shell.Introspect.GetWindows to the portals, and Wayland has no
// wmctrl/xdotool equivalent, so nothing outside the shell process can
// enumerate or activate windows. Activating from inside the shell also
// sidesteps focus-stealing prevention — the compositor itself is the one
// raising the window, so the request is authoritative.
//
// Exported on gnome-shell's own bus connection, so the destination name is
// org.gnome.Shell (the same pattern third-party window extensions use).

import Gio from 'gi://Gio';
import Meta from 'gi://Meta';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const OBJECT_PATH = '/org/gnome/Shell/Extensions/TeleprompterFocus';

// List returns JSON rather than a{sv} so the Python side can parse it with the
// standard library alone, which is the project-wide constraint.
const IFACE = `
<node>
  <interface name="org.gnome.Shell.Extensions.TeleprompterFocus">
    <method name="List">
      <arg type="s" direction="out" name="windows"/>
    </method>
    <method name="Activate">
      <arg type="t" direction="in" name="id"/>
      <arg type="b" direction="out" name="activated"/>
    </method>
  </interface>
</node>`;

/** Windows a user could plausibly be sharing, most recently used first. */
function listWindows() {
    return global.display
        .get_tab_list(Meta.TabList.NORMAL, null)
        .filter(win => !win.is_skip_taskbar());
}

function describe(win, index) {
    const frame = win.get_frame_rect();
    return {
        // Stringified because window ids are 64-bit and JSON numbers are not.
        id: String(win.get_id()),
        title: win.get_title() ?? '',
        wm_class: win.get_wm_class() ?? '',
        width: frame.width,
        height: frame.height,
        focused: win.has_focus(),
        // Position in the shell's most-recently-used order; the callers use it
        // to break ties between equally good matches.
        mru: index,
    };
}

export default class TeleprompterFocusExtension extends Extension {
    enable() {
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);
    }

    disable() {
        this._dbus?.flush();
        this._dbus?.unexport();
        this._dbus = null;
    }

    List() {
        return JSON.stringify(listWindows().map(describe));
    }

    Activate(id) {
        const wanted = Number(id);
        const win = listWindows().find(w => Number(w.get_id()) === wanted);
        if (!win)
            return false;
        // Moves to the window's workspace and focuses it with a valid
        // timestamp, so it is raised rather than merely marked urgent.
        Main.activateWindow(win);
        return true;
    }
}
