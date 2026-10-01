"""Tests for the HTTP trust boundary around the window-focus routes.

Binding to 127.0.0.1 keeps other machines out, but any page the user has open
can still send requests to it, and a rebound DNS name would serve our own
pages under an attacker's origin. These tests drive a real server over a real
socket with the desktop operations replaced, and pin down who gets refused.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from loader import load_module

server = load_module("app/mirror-server.py", "mirror_server")


class FocusRoutesTest(unittest.TestCase):
    def setUp(self):
        self.remembered = []
        self.focused = []
        focus = server.window_focus
        for name, replacement in {
            "list_windows": lambda: [{"id": "7", "title": "Call", "wm_class": "zoom"}],
            "remember": lambda window_id: self.remembered.append(window_id),
            "focus_shared": lambda: self.focused.append(True),
            "load_target": lambda: {"id": "7", "wm_class": "zoom", "title": "Call"},
        }.items():
            self.addCleanup(setattr, focus, name, getattr(focus, name))
            setattr(focus, name, replacement)

        class QuietHandler(server.Handler):
            def log_message(self, fmt, *args):
                pass  # keep the request log out of the test output

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.port = self.httpd.server_address[1]
        # A short poll keeps shutdown() from costing half a second per test.
        threading.Thread(target=self.httpd.serve_forever,
                         kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def request(self, path, method="GET", body=None, headers=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method,
            data=None if body is None else body.encode(), headers=headers or {})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status
        except urllib.error.HTTPError as err:
            err.close()
            return err.code

    # ─── Allowed ─────────────────────────────────────────────────────────

    def test_own_page_may_change_the_target(self):
        origin = f"http://127.0.0.1:{self.port}"
        status = self.request("/focus/target", "POST", '{"id": "7"}', {"Origin": origin})
        self.assertEqual(status, 200)
        self.assertEqual(self.remembered, ["7"])

    def test_localhost_origin_is_also_ours(self):
        # The tablet reaches the server as localhost through ADB reverse.
        status = self.request("/focus", "POST", "{}",
                              {"Origin": f"http://localhost:{self.port}"})
        self.assertEqual(status, 200)
        self.assertEqual(self.focused, [True])

    def test_a_request_without_an_origin_is_allowed(self):
        # Local tools (the CLI, curl, these tests) send none; a browser always
        # sends one on POST, so this does not weaken the browser boundary.
        self.assertEqual(self.request("/focus", "POST", "{}"), 200)

    def test_reading_the_target_without_an_origin_is_allowed(self):
        self.assertEqual(self.request("/focus/target"), 200)

    # ─── Refused ─────────────────────────────────────────────────────────

    def test_another_local_origin_cannot_focus(self):
        other = f"http://127.0.0.1:{self.port + 1}"
        self.assertEqual(self.request("/focus", "POST", "{}", {"Origin": other}), 403)
        self.assertEqual(self.focused, [])

    def test_another_local_origin_cannot_retarget(self):
        other = "http://localhost:3000"
        status = self.request("/focus/target", "POST", '{"id": "7"}', {"Origin": other})
        self.assertEqual(status, 403)
        self.assertEqual(self.remembered, [])

    def test_a_remote_origin_cannot_focus(self):
        status = self.request("/focus", "POST", "{}", {"Origin": "https://example.invalid"})
        self.assertEqual(status, 403)

    def test_a_null_origin_cannot_focus(self):
        # Sandboxed iframes and some redirects send Origin: null.
        self.assertEqual(self.request("/focus", "POST", "{}", {"Origin": "null"}), 403)

    def test_another_origin_cannot_read_the_window_list(self):
        status = self.request("/windows", headers={"Origin": "http://evil.invalid"})
        self.assertEqual(status, 403)

    def test_a_rebound_host_is_refused(self):
        # DNS rebinding: a name resolving to 127.0.0.1 would otherwise serve
        # our pages under the attacker's origin, making everything readable.
        self.assertEqual(self.request("/cast", headers={"Host": "evil.invalid"}), 403)
        self.assertEqual(self.request("/focus", "POST", "{}",
                                      {"Host": "evil.invalid"}), 403)
        self.assertEqual(self.focused, [])

    def test_signaling_is_guarded_too(self):
        # /reset is as reachable as /focus; an unrelated page could otherwise
        # tear down an in-progress connection.
        status = self.request("/reset", "POST", "", {"Origin": "http://evil.invalid"})
        self.assertEqual(status, 403)

    def test_a_refused_post_still_answers_json(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/focus", method="POST", data=b"{}",
            headers={"Origin": "http://evil.invalid"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertIn("error", json.loads(caught.exception.read()))


class OriginParsingTest(unittest.TestCase):
    def test_matching_origins(self):
        for origin in ("http://localhost:8047", "http://127.0.0.1:8047",
                       "http://[::1]:8047"):
            self.assertTrue(server._is_own_origin(origin, 8047), origin)

    def test_non_matching_origins(self):
        for origin in ("http://localhost:9000", "http://example.invalid:8047",
                       "null", "", "file://", "http://localhost:8047/path",
                       "http://localhost.evil.invalid:8047"):
            self.assertFalse(server._is_own_origin(origin, 8047), origin)

    def test_default_port_is_understood(self):
        self.assertTrue(server._is_own_origin("http://localhost", 80))
        self.assertFalse(server._is_own_origin("http://localhost", 8047))

    def test_loopback_hosts(self):
        for host in ("localhost", "localhost:8047", "127.0.0.1:8047", "[::1]:8047", ""):
            self.assertTrue(server._is_loopback_host(host), host)
        for host in ("evil.invalid", "evil.invalid:8047", "192.168.1.5:8047"):
            self.assertFalse(server._is_loopback_host(host), host)


if __name__ == "__main__":
    unittest.main()
