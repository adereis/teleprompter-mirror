#!/usr/bin/env python3
"""WebRTC signaling server for teleprompter mirroring to a tablet."""

import argparse
import json
import re
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# Shared config lives in ../lib; make it importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import teleprompter_config  # noqa: E402

STATIC_DIR = Path(__file__).parent
LAN_IPS = []

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

    def do_GET(self):
        routes = {
            "/": ("redirect", "/cast"),
            "/cast": ("file", "cast.html", "text/html"),
            "/latency": ("file", "latency-test.html", "text/html"),
            "/view": ("file", "view.html", "text/html"),
            "/icon.svg": ("file", "icon.svg", "image/svg+xml"),
            "/manifest.json": ("file", "manifest.json", "application/manifest+json"),
            "/offer": ("signal", "_offer"),
            "/answer": ("signal", "_answer"),
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

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()

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
