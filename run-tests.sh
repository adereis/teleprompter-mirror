#!/bin/bash
# Run the project's checks: Python unit tests, byte-compilation, and shell
# syntax/lint. Standard library only — no test framework to install.
#
#   ./run-tests.sh
#
# shellcheck is used when available but is not required to pass locally.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

status=0

echo "== Python unit tests =="
python3 -m unittest discover -s tests "$@"

echo
echo "== Python byte-compile =="
python3 -m py_compile mirror-server.py camera-control.py teleprompter_config.py
echo "ok"

echo
echo "== Shell syntax (bash -n) =="
mapfile -t scripts < <(git ls-files '*.sh' '99-teleprompter' '99-teleprompter-camera')
for s in "${scripts[@]}"; do
    bash -n "$s" && echo "  ok: $s"
done

echo
echo "== shellcheck =="
if command -v shellcheck >/dev/null 2>&1; then
    shellcheck -x "${scripts[@]}" && echo "  shellcheck clean"
else
    echo "  shellcheck not installed — skipping (install it for full linting)"
fi

exit "$status"
