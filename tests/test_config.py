"""Tests for teleprompter_config — the shared config loader."""

import unittest

import loader  # noqa: F401  (importing puts lib/ on sys.path)
import teleprompter_config as cfg


class ParseEnvFileTest(unittest.TestCase):
    def test_basic_key_value(self):
        self.assertEqual(
            cfg.parse_env_file("TELEPROMPTER_PORT=9000"),
            {"TELEPROMPTER_PORT": "9000"},
        )

    def test_ignores_comments_and_blanks(self):
        text = "# a comment\n\nTELEPROMPTER_BIND=0.0.0.0\n   \n# trailing\n"
        self.assertEqual(cfg.parse_env_file(text), {"TELEPROMPTER_BIND": "0.0.0.0"})

    def test_strips_export_prefix(self):
        self.assertEqual(
            cfg.parse_env_file("export TELEPROMPTER_BROWSER=chromium"),
            {"TELEPROMPTER_BROWSER": "chromium"},
        )

    def test_strips_matching_quotes(self):
        self.assertEqual(
            cfg.parse_env_file('TELEPROMPTER_CAMERA_CONNECTION="My Camera"'),
            {"TELEPROMPTER_CAMERA_CONNECTION": "My Camera"},
        )
        self.assertEqual(
            cfg.parse_env_file("TELEPROMPTER_CAMERA_CONNECTION='My Camera'"),
            {"TELEPROMPTER_CAMERA_CONNECTION": "My Camera"},
        )

    def test_value_with_equals_sign(self):
        # URLs and the like contain '=' in query strings; keep everything.
        self.assertEqual(
            cfg.parse_env_file("TELEPROMPTER_CAMERA_ENDPOINT=http://h/sony?a=b"),
            {"TELEPROMPTER_CAMERA_ENDPOINT": "http://h/sony?a=b"},
        )

    def test_line_without_equals_is_skipped(self):
        self.assertEqual(cfg.parse_env_file("not a setting"), {})


class LoadPrecedenceTest(unittest.TestCase):
    def test_defaults_when_no_file_no_env(self):
        loaded = cfg.load(environ={}, path="/nonexistent/config.env")
        self.assertEqual(loaded, cfg.DEFAULTS)
        self.assertIsNot(loaded, cfg.DEFAULTS)  # must be a copy

    def test_file_overrides_default(self):
        path = self._write("TELEPROMPTER_PORT=9000\n")
        loaded = cfg.load(environ={}, path=path)
        self.assertEqual(loaded["TELEPROMPTER_PORT"], "9000")
        self.assertEqual(loaded["TELEPROMPTER_BIND"], cfg.DEFAULTS["TELEPROMPTER_BIND"])

    def test_env_overrides_file(self):
        path = self._write("TELEPROMPTER_PORT=9000\n")
        loaded = cfg.load(environ={"TELEPROMPTER_PORT": "7777"}, path=path)
        self.assertEqual(loaded["TELEPROMPTER_PORT"], "7777")

    def test_unrelated_env_vars_ignored(self):
        loaded = cfg.load(environ={"PATH": "/usr/bin", "HOME": "/root"},
                          path="/nonexistent")
        self.assertEqual(loaded, cfg.DEFAULTS)

    def test_get_single_value(self):
        path = self._write("TELEPROMPTER_BROWSER=brave-browser\n")
        self.assertEqual(
            cfg.get("TELEPROMPTER_BROWSER", environ={}, path=path),
            "brave-browser",
        )

    def _write(self, text):
        import tempfile
        fd = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        fd.write(text)
        fd.close()
        self.addCleanup(lambda: __import__("os").unlink(fd.name))
        return fd.name


if __name__ == "__main__":
    unittest.main()
