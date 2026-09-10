#!/bin/bash
# Run the project's checks: Python unit tests, byte-compilation, and shell
# syntax/lint. Standard library only — no test framework to install.
#
#   ./run-tests.sh
#
# Install the shell linter on Fedora with: sudo dnf install ShellCheck
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

export TMPDIR="$HOME/tmp"
mkdir -p "$TMPDIR"

echo "== Python unit tests =="
python3 -m unittest discover -s tests "$@"

echo
echo "== Browser lifecycle tests (Node.js) =="
node tests/browser.test.js

echo
echo "== Python byte-compile =="
python3 -m py_compile app/mirror-server.py camera/camera-control.py lib/teleprompter_config.py
echo "ok"

echo
echo "== Shell syntax (bash -n) =="
mapfile -t scripts < <(git ls-files '*.sh' '.githooks/pre-commit' \
    'system/networkmanager/99-teleprompter' 'system/networkmanager/99-teleprompter-camera')
for s in "${scripts[@]}"; do
    bash -n "$s"
    echo "  ok: $s"
done

echo
echo "== shellcheck =="
if command -v shellcheck >/dev/null 2>&1; then
    # SCRIPTDIR lets `source=../lib/config.sh` directives resolve relative to
    # each script's own location now that the scripts live in subdirectories.
    shellcheck -x --source-path=SCRIPTDIR "${scripts[@]}"
    echo "  shellcheck clean"
else
    echo "  shellcheck is required; install it with: sudo dnf install ShellCheck" >&2
    exit 1
fi
