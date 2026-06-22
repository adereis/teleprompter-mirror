"""Configuration loader for the Teleprompter Mirror Python tools.

Reads the same ``KEY=value`` file the bash scripts and the systemd service use,
so there is a single source of truth across the whole project.

Precedence (highest first):
    1. the process environment       (one-off overrides)
    2. ~/.config/teleprompter-mirror/config.env
    3. the built-in defaults in DEFAULTS

Standard library only — no external dependencies.
"""

import os
from pathlib import Path

DEFAULTS = {
    "TELEPROMPTER_PORT": "8047",
    "TELEPROMPTER_BIND": "127.0.0.1",
    "TELEPROMPTER_BROWSER": "google-chrome",
    "TELEPROMPTER_CAMERA_CONNECTION": "Camera-A6300",
    "TELEPROMPTER_CAMERA_ENDPOINT": "http://192.168.122.1:8080/sony",
}


def config_path():
    """Return the path to the user's config file (may not exist)."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "teleprompter-mirror" / "config.env"


def parse_env_file(text):
    """Parse shell-style ``KEY=value`` lines into a dict.

    Blank lines and ``#`` comments are ignored. A leading ``export`` and one
    layer of matching single or double quotes around the value are stripped, so
    the same file can be sourced by bash and parsed here.
    """
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        values[key] = val
    return values


def load(environ=None, path=None):
    """Return the merged configuration dict (defaults < file < environment)."""
    environ = os.environ if environ is None else environ
    path = config_path() if path is None else Path(path)

    cfg = dict(DEFAULTS)
    if path.is_file():
        cfg.update(parse_env_file(path.read_text()))
    for key in DEFAULTS:
        if key in environ:
            cfg[key] = environ[key]
    return cfg


def get(key, environ=None, path=None):
    """Return a single configuration value."""
    return load(environ=environ, path=path).get(key)
