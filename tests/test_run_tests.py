"""Exercise the check runner's exit status with isolated tool fixtures."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from loader import ROOT


class CheckRunnerTest(unittest.TestCase):
    def run_checks(self, shell_source="true\n", lint_status=0):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(ROOT / "run-tests.sh", root / "run-tests.sh")
            (root / "fixture.sh").write_text(shell_source)
            tool_dir = root / "tools"
            tool_dir.mkdir()
            for name, source in {
                "python3": "exit 0\n",
                "git": "printf '%s\\n' fixture.sh\n",
                "shellcheck": f"exit {lint_status}\n",
            }.items():
                tool = tool_dir / name
                tool.write_text("#!/bin/bash\n" + source)
                tool.chmod(0o755)
            return subprocess.run(
                ["bash", str(root / "run-tests.sh")],
                env={**os.environ, "PATH": f"{tool_dir}:{os.environ['PATH']}"},
                capture_output=True, text=True, check=False,
            )

    def test_successful_checks_succeed(self):
        result = self.run_checks()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_shell_syntax_failure_fails_runner(self):
        result = self.run_checks(shell_source="if then\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("shellcheck clean", result.stdout)

    def test_shellcheck_failure_fails_runner(self):
        result = self.run_checks(lint_status=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("shellcheck clean", result.stdout)
