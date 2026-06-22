"""Helpers to load the project's scripts as importable modules in tests.

`app/mirror-server.py` and `camera/camera-control.py` have hyphens in their
names, so they can't be imported with a normal `import` statement. Load them by
file path instead. The shared `lib/` directory is added to sys.path so both the
scripts' own `import teleprompter_config` and the tests' direct imports resolve.
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "lib"

# Make the shared config module importable for both the modules under test and
# the tests that import it directly.
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))


def load_module(relpath, modname):
    """Load <repo-root>/<relpath> as a module object named <modname>."""
    spec = importlib.util.spec_from_file_location(modname, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
