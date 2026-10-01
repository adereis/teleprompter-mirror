#!/usr/bin/env python3
"""WebRTC signaling server for teleprompter mirroring to a tablet."""

import argparse
import json
import re
import subprocess
import sys
import threading
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# Shared config lives in ../lib; make it importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import teleprompter_config  # noqa: E402
import window_focus  # noqa: E402

STATIC_DIR = Path(__file__).parent
LAN_IPS = []

# Binding to 127.0.0.1 keeps other machines out; it is not a trust boundary
# against other *pages*. Any site the user has open can POST here, and a
# rebound DNS name would even make our own pages same-origin with an attacker.
LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}


def _is_loopback_host(host):
    """True when a Host header names this machine's loopback interface.

    A missing Host is accepted: only HTTP/1.0 clients omit it, and a browser
    never does, so there is no rebinding to protect against.
    """
    if not host:
        return True
    try:
        return urllib.parse.urlsplit(f"//{host}").hostname in LOOPBACK_NAMES
    except ValueError:
        return False


def _is_own_origin(origin, port):
    """True when an Origin header is one of this server's own origins."""
    parts = urllib.parse.urlsplit(origin)
    if parts.scheme not in ("http", "https") or parts.path or parts.query:
        return False
    try:
        hostname, declared = parts.hostname, parts.port
    except ValueError:
        return False  # unparseable port
    if hostname not in LOOPBACK_NAMES:
        return False
    return (declared or (443 if parts.scheme == "https" else 80)) == port

# Chrome replaces local IPs with mDNS UUIDs for privacy — tablets can't resolve them.
_MDNS_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.local"
)


def _fix_mdns(sdp_json, lan_ips):
    """Replace mDNS hostnames in SDP candidates with real LAN IPs.

    Each mDNS candidate line is duplicated for every LAN IP so that ICE
    can find a working path regardless of which network the peer is on.
    """
    data = json.loads(sdp_json)
    sdp = data.get("sdp", "")
    lines = sdp.split("\r\n")
    out = []
    rewrites = 0
    for line in lines:
        m = _MDNS_RE.search(line)
        if m:
            for i, ip in enumerate(lan_ips):
                newline = _MDNS_RE.sub(ip, line)
                # Vary the foundation so ICE treats each as a distinct candidate
                if i > 0:
                    parts = newline.split(" ", 2)
                    parts[0] = parts[0] + str(i)
                    newline = " ".join(parts)
                out.append(newline)
            rewrites += 1
        else:
            out.append(line)
    if rewrites:
        print(f"  [signal] Rewrote {rewrites} mDNS candidate(s) → {lan_ips}")
    data["sdp"] = "\r\n".join(out)
    return json.dumps(data)


class Handler(BaseHTTPRequestHandler):
    _lock = threading.Lock()
    _offer = None
    _answer = None

    def _authorized(self, guarded):
        """Reject requests another origin's page could have made.

        Two checks. The Host header must name the loopback interface, which
        stops DNS rebinding — without it, a name resolving to 127.0.0.1 would
        serve our own pages under the attacker's origin, making every endpoint
        readable to them. And on anything that acts (window focus, signaling),
        an Origin must be ours: a cross-origin `fetch(..., {mode: 'no-cors'})`
        cannot read the reply but its side effect still happens. Requests
        without an Origin are allowed — browsers always send one on POST, so
        those are local tools, which already have the session bus anyway.
        """
        if not _is_loopback_host(self.headers.get("Host")):
            return False
        origin = self.headers.get("Origin")
        if guarded and origin:
            return _is_own_origin(origin, self.server.server_address[1])
        return True

    def _forbidden(self):
        self._respond(403, "application/json", json.dumps({"error": "forbidden"}))

    def do_GET(self):
        # /windows and /focus/target expose the desktop, so they are guarded
        # like the acting routes even though they only read.
        if not self._authorized(self.path.startswith(("/windows", "/focus"))):
            return self._forbidden()
        routes = {
            "/": ("redirect", "/cast"),
            "/cast": ("file", "cast.html", "text/html"),
            "/latency": ("file", "latency-test.html", "text/html"),
            "/view": ("file", "view.html", "text/html"),
            "/icon.svg": ("file", "icon.svg", "image/svg+xml"),
            "/manifest.json": ("file", "manifest.json", "application/manifest+json"),
            "/offer": ("signal", "_offer"),
            "/answer": ("signal", "_answer"),
            "/windows": ("focus", window_focus.list_windows),
            "/focus/target": ("focus", lambda: window_focus.load_target() or {}),
        }
        route = routes.get(self.path)
        if not route:
            return self._respond(404)

        kind = route[0]
        if kind == "redirect":
            self.send_response(302)
            self.send_header("Location", route[1])
            self.end_headers()
        elif kind == "file":
            path = STATIC_DIR / route[1]
            if path.exists():
                self._respond(200, route[2], path.read_bytes())
            else:
                self._respond(404)
        elif kind == "signal":
            with Handler._lock:
                data = getattr(Handler, route[1])
            if data:
                self._respond(200, "application/json", data.encode())
            else:
                self._respond(204)
        elif kind == "focus":
            self._focus_response(route[1])

    def do_POST(self):
        # Read first even when the request will be refused: leaving the body
        # in the socket would desync the next request on a kept-alive
        # connection.
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        if not self._authorized(guarded=True):
            return self._forbidden()

        # Handled before the signaling lock: these shell out to the session bus
        # and must not stall an SDP exchange while they wait on the shell.
        if self.path == "/focus":
            return self._focus_response(window_focus.focus_shared)
        if self.path == "/focus/target":
            request = self._json_body(body)
            if request is None:
                return
            return self._focus_response(
                lambda: window_focus.remember(request.get("id")))

        with Handler._lock:
            if self.path == "/offer":
                Handler._offer = _fix_mdns(body, LAN_IPS)
                Handler._answer = None
                return self._respond(200)
            elif self.path == "/answer":
                Handler._answer = _fix_mdns(body, LAN_IPS)
                return self._respond(200)
            elif self.path == "/reset":
                Handler._offer = None
                Handler._answer = None
                return self._respond(200)
        self._respond(404)

    def _json_body(self, body):
        """Parse a JSON request body, answering 400 and returning None if bad."""
        if not body.strip():
            return {}
        try:
            request = json.loads(body)
        except json.JSONDecodeError as err:
            self._respond(400, "application/json", json.dumps({"error": str(err)}))
            return None
        if not isinstance(request, dict):
            self._respond(400, "application/json",
                          json.dumps({"error": "expected a JSON object"}))
            return None
        return request

    def _focus_response(self, operation):
        """Run a window_focus operation, reporting failures as JSON errors.

        409 rather than 500: the usual failure is "no window matches", which
        is about the desktop's state, not a broken server.
        """
        try:
            result = operation()
        except window_focus.FocusError as err:
            return self._respond(409, "application/json", json.dumps({"error": str(err)}))
        self._respond(200, "application/json", json.dumps(result))

    def _respond(self, code, content_type=None, body=None):
        self.send_response(code)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.end_headers()
        if body:
            self.wfile.write(body if isinstance(body, bytes) else body.encode())

    def log_message(self, fmt, *args):
        msg = str(args[0]) if args else ""
        if "/offer" not in msg and "/answer" not in msg:
            super().log_message(fmt, *args)


if __name__ == "__main__":
    cfg = teleprompter_config.load()
    parser = argparse.ArgumentParser(description="Teleprompter mirror signaling server")
    parser.add_argument("-p", "--port", type=int, default=int(cfg["TELEPROMPTER_PORT"]),
                        help="Listen port (default: %(default)s)")
    parser.add_argument("--bind", default=cfg["TELEPROMPTER_BIND"],
                        help="Bind address (default: %(default)s — localhost only)")
    parser.add_argument("--ip", help="Override LAN IP (default: auto-detect all)")
    args = parser.parse_args()

    all_ips = subprocess.check_output(["hostname", "-I"]).decode().split()
    LAN_IPS = [args.ip] if args.ip else [ip for ip in all_ips if ":" not in ip]

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"Teleprompter mirror on {args.bind}:{args.port}")
    print(f"  Cast:  http://localhost:{args.port}/cast")
    print(f"  View:  http://localhost:{args.port}/view  (tablet via ADB reverse)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
