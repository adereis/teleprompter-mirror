"""Find and raise the window being cast to the tablet.

GNOME denies ``org.gnome.Shell.Introspect.GetWindows`` to everything except the
desktop portals, and Wayland has no wmctrl equivalent, so window listing and
activation go through this project's own GNOME Shell extension
(``system/gnome-extension/teleprompter-focus@teleprompter-mirror.local``),
which exports ``List``/``Activate`` on the session bus.

Which window is being shared cannot be discovered: the portal knows, but tells
neither the shell's clients nor the capturing page. So the target is either
remembered (the user picks it once, in the cast page) or matched against the
``TELEPROMPTER_FOCUS_MATCH`` pattern. The remembered target is stored outside
the browser so the cast page button, the desktop entry action, and a keyboard
shortcut all raise the same window.

The selection rules are pure (:func:`select_window`) so they can be tested
without a running shell; everything touching D-Bus sits in :func:`_call`.

Standard library only — no external dependencies.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import teleprompter_config

BUS_NAME = "org.gnome.Shell"
OBJECT_PATH = "/org/gnome/Shell/Extensions/TeleprompterFocus"
INTERFACE = "org.gnome.Shell.Extensions.TeleprompterFocus"
EXTENSION_UUID = "teleprompter-focus@teleprompter-mirror.local"
CALL_TIMEOUT = 5  # seconds to wait on the shell before giving up

_MISSING_EXTENSION_HINT = (
    f"The {EXTENSION_UUID} GNOME Shell extension is not answering. Install it "
    f"with system/install.sh, then log out and back in (Wayland cannot reload "
    f"the shell in place) and check: gnome-extensions info {EXTENSION_UUID}"
)

# What busctl says when gnome-shell is running but the extension is not loaded
# ("Object does not exist at path …") or is an incompatible version.
_MISSING_EXTENSION_ERRORS = (
    "does not exist at path",
    "unknown object",
    "unknown interface",
    "unknown method",
    "not activatable",
)


class FocusError(Exception):
    """A window could not be listed, matched, or raised."""


# ─── Remembered target ───────────────────────────────────────────────────────

def state_path():
    """Return the path of the remembered-target file (may not exist).

    This is runtime state, not configuration, but it lives beside config.env so
    there is only one Teleprompter Mirror directory to clean up.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "teleprompter-mirror" / "focus-target.json"


def load_target(path=None):
    """Return the remembered target dict, or None when nothing is remembered."""
    path = state_path() if path is None else Path(path)
    if not path.is_file():
        return None
    try:
        target = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as err:
        raise FocusError(f"Could not read {path}: {err}") from err
    if not isinstance(target, dict):
        raise FocusError(f"{path} does not contain a window target")
    return target


def save_target(target, path=None):
    """Persist the remembered target, replacing any previous one.

    Written to a sibling temporary file and renamed into place. Writing in
    place would truncate first, and this file has several writers — the
    threaded server and the CLI — so a reader could catch a half-written
    document, or two writers could interleave into one that parses as neither.
    """
    path = state_path() if path is None else Path(path)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False,
                                         prefix=f".{path.name}.") as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(target, indent=2) + "\n")
        os.replace(temporary, path)  # atomic within one filesystem
    except OSError as err:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise FocusError(f"Could not write {path}: {err}") from err


def forget_target(path=None):
    """Drop the remembered target. Returns True if a target was removed."""
    path = state_path() if path is None else Path(path)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as err:
        raise FocusError(f"Could not remove {path}: {err}") from err
    return True


# ─── Matching (pure) ─────────────────────────────────────────────────────────

def _norm(value):
    return (value or "").strip().lower()


def _target_score(window, target):
    """Rank a window against a remembered target; 0 means "not this one".

    Three tiers, strongest first:

    3. the exact window — matched by shell id, which is what makes picking one
       of several browser windows stick. Ids are per-session, so a stale one
       could in principle name a different window after a relogin; requiring
       the class to agree too makes that harmless.
    2. same application, same title.
    1. same application.

    Titles are volatile — a call window renames itself as the meeting changes,
    and a browser window is titled after whichever tab is active — so an exact
    title is a bonus, never a requirement. The application class is the stable
    part and does the real filtering.
    """
    wanted_class = _norm(target.get("wm_class"))
    if wanted_class:
        if _norm(window.get("wm_class")) != wanted_class:
            return 0
    elif _norm(window.get("title")) != _norm(target.get("title")):
        # Nothing stable to match on; fall back to requiring the exact title.
        return 0
    wanted_id = target.get("id")
    if wanted_id is not None and str(window.get("id")) == str(wanted_id):
        return 3
    return 2 if _norm(window.get("title")) == _norm(target.get("title")) else 1


def _pattern_score(window, regex):
    if regex.search(window.get("title") or "") or regex.search(window.get("wm_class") or ""):
        return 1
    return 0


def select_window(windows, target=None, pattern=None):
    """Return the best window to raise, or None when nothing matches.

    `target` (a remembered pick) wins over `pattern` (the configured
    `TELEPROMPTER_FOCUS_MATCH` fallback). Ties are broken by preferring a
    window that is not already focused — otherwise clicking the button in the
    cast window would just re-focus the cast window, since it shares an
    application class with the browser window being shared — and then by the
    shell's most-recently-used order.
    """
    if target:
        scored = [(window, _target_score(window, target)) for window in windows]
    elif pattern:
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as err:
            raise FocusError(f"TELEPROMPTER_FOCUS_MATCH is not a valid regex: {err}") from err
        scored = [(window, _pattern_score(window, regex)) for window in windows]
    else:
        return None

    ranked = sorted(
        ((window, score) for window, score in scored if score),
        key=lambda item: (-item[1], bool(item[0].get("focused")), item[0].get("mru", 0)),
    )
    return ranked[0][0] if ranked else None


# ─── Shell extension (D-Bus) ─────────────────────────────────────────────────

def _call(method, signature=None, arguments=()):
    """Call a method on the shell extension and return its single result."""
    command = ["busctl", "--user", "--json=short", "call",
               BUS_NAME, OBJECT_PATH, INTERFACE, method]
    if signature:
        command.append(signature)
        command.extend(str(argument) for argument in arguments)
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                check=False, timeout=CALL_TIMEOUT)
    except FileNotFoundError as err:
        raise FocusError("busctl is not installed; it ships with systemd") from err
    except subprocess.TimeoutExpired as err:
        # A wedged shell must not pin a server thread (or the cast page) open.
        raise FocusError(
            f"{INTERFACE}.{method} did not answer within {CALL_TIMEOUT}s; "
            f"gnome-shell may be busy or stuck") from err
    if result.returncode != 0:
        stderr = result.stderr.strip()
        lowered = stderr.lower()
        if any(marker in lowered for marker in _MISSING_EXTENSION_ERRORS):
            raise FocusError(_MISSING_EXTENSION_HINT)
        raise FocusError(f"{INTERFACE}.{method} failed: {stderr or result.returncode}")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as err:
        raise FocusError(f"Unreadable reply from {method}: {err}") from err
    data = payload.get("data") or []
    return data[0] if data else None


def list_windows():
    """Return the open windows, most recently used first."""
    raw = _call("List")
    try:
        windows = json.loads(raw or "[]")
    except json.JSONDecodeError as err:
        raise FocusError(f"Unreadable window list: {err}") from err
    if not isinstance(windows, list):
        raise FocusError("Window list is not a list")
    return windows


def activate_window(window_id):
    """Raise a window by id. Returns False if the shell no longer has it."""
    try:
        wanted = int(window_id)
    except (TypeError, ValueError) as err:
        raise FocusError(f"Not a window id: {window_id!r}") from err
    return bool(_call("Activate", "t", [wanted]))


# ─── Operations ──────────────────────────────────────────────────────────────

def focus(target=None, pattern=None, windows=None):
    """Raise the matching window and return it.

    Raises FocusError when nothing matches or the shell refuses, so a failed
    focus can never look like a successful one.
    """
    windows = list_windows() if windows is None else windows
    window = select_window(windows, target=target, pattern=pattern)
    if window is None:
        if target:
            described = target.get("wm_class") or target.get("title") or "the remembered window"
            raise FocusError(f"No open window matches {described}; pick the shared window again")
        if pattern:
            raise FocusError(f"No open window matches TELEPROMPTER_FOCUS_MATCH ({pattern})")
        raise FocusError("No shared window has been picked yet")
    if window.get("focused"):
        return window  # already in front; activating again would be a no-op
    if not activate_window(window.get("id")):
        raise FocusError(f"The shell could not raise {window.get('title') or window.get('id')}")
    return window


def focus_shared(cfg=None):
    """Raise the remembered window, falling back to the configured pattern."""
    cfg = teleprompter_config.load() if cfg is None else cfg
    return focus(target=load_target(), pattern=cfg.get("TELEPROMPTER_FOCUS_MATCH") or None)


def remember(window_id, windows=None):
    """Remember a listed window as the shared one and return it."""
    if window_id is None:
        raise FocusError("No window id given")
    windows = list_windows() if windows is None else windows
    wanted = str(window_id)
    window = next((w for w in windows if str(w.get("id")) == wanted), None)
    if window is None:
        raise FocusError(f"Window {wanted} is no longer open")
    # The id pins this exact window for as long as it lives; class and title
    # are the fallback once it (or the session) has been restarted.
    save_target({"id": wanted,
                 "wm_class": window.get("wm_class", ""),
                 "title": window.get("title", "")})
    return window


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main(argv):
    command = argv[0] if argv else "focus"
    try:
        if command == "list":
            print(json.dumps(list_windows(), indent=2))
        elif command == "focus":
            window = focus_shared()
            print(f"Focused: {window.get('title') or window.get('wm_class')}")
        elif command == "remember":
            if len(argv) != 2:
                raise FocusError("Usage: window_focus.py remember <window-id>")
            window = remember(argv[1])
            print(f"Remembered: {window.get('title') or window.get('wm_class')}")
        elif command == "forget":
            print("Forgotten." if forget_target() else "Nothing was remembered.")
        else:
            raise FocusError(f"Unknown command: {command} (list|focus|remember|forget)")
    except FocusError as err:
        print(err, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
