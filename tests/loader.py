"""Helpers to load the project's scripts as importable modules in tests.

`mirror-server.py` and `camera-control.py` have hyphens in their names, so they
can't be imported with a normal `import` statement. Load them by file path
instead. The repo root is added to sys.path so their own
`import teleprompter_config` resolves.
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_module(filename, modname):
    """Load <repo-root>/<filename> as a module object named <modname>."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(modname, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
