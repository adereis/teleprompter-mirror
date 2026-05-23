#!/usr/bin/env python3
"""Capture a screen region and stream it to the tablet via WebRTC.

STATUS: NON-FUNCTIONAL PROTOTYPE (May 2026)

This script does NOT work on GNOME 49 + PipeWire 1.4.x due to a
compatibility issue between GStreamer's pipewiresrc element and GNOME
Shell's screencast portal implementation.

The root cause: GNOME Shell creates its screencast PipeWire node with
object.register=false, which means the node exists in the PipeWire
graph but is NOT published to the PipeWire registry. GStreamer's
pipewiresrc uses registry-based node discovery to find its target
before connecting — since the node is unregistered, pipewiresrc
reports "target not found" and fails. This happens regardless of
whether the node is specified via path, target-object, or serial,
and regardless of whether the portal fd or the default PipeWire
connection is used.

The working alternative is cast-crop.html, which uses Chrome's
getDisplayMedia() for capture and a canvas drawImage() crop before
sending via WebRTC. See /cast-crop in the mirror server.

If a future PipeWire or GStreamer version fixes pipewiresrc to handle
unregistered portal nodes, this script should work — the portal
interaction and WebRTC signaling code are correct.

Intended usage (when/if pipewiresrc is fixed):
    ./cast-region.py                          # full screen
    ./cast-region.py --crop 100,200,800,600   # region at (100,200) size 800x600
    ./cast-region.py --server http://IP:8080  # custom signaling server
    ./cast-region.py -p 8080                  # shorthand for localhost:PORT
"""

import argparse
import json
import os
import random
import sys
import threading
import time
import urllib.request

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstSdp", "1.0")
gi.require_version("GstWebRTC", "1.0")
from gi.repository import Gio, GLib, Gst, GstSdp, GstWebRTC


gi.require_version("Xdp", "1.0")
from gi.repository import Xdp

# Keep portal session alive for the lifetime of the process
_portal_session = None


def portal_screencast():
    """Request screen capture via libportal. Returns (fd, node_id)."""
    global _portal_session
    portal = Xdp.Portal()
    result = {}
    error = [None]
    loop = GLib.MainLoop()

    def on_session_created(portal_obj, res, _data):
        try:
            session = portal_obj.create_screencast_session_finish(res)
            result["session"] = session
            session.start(None, None, on_started, None)
        except Exception as e:
            error[0] = e
            loop.quit()

    def on_started(session, res, _data):
        try:
            session.start_finish(res)
            result["fd"] = session.open_pipewire_remote()
            streams_var = session.get_streams()
            if isinstance(streams_var, GLib.Variant):
                result["streams"] = streams_var.unpack()
            else:
                result["streams"] = streams_var
            loop.quit()
        except Exception as e:
            error[0] = e
            loop.quit()

    portal.create_screencast_session(
        Xdp.OutputType.MONITOR | Xdp.OutputType.WINDOW,
        Xdp.ScreencastFlags.NONE,
        Xdp.CursorMode.EMBEDDED,
        Xdp.PersistMode.TRANSIENT,
        None,  # restore_token
        None,  # cancellable
        on_session_created,
        None,
    )
    loop.run()

    if error[0]:
        raise error[0]

    _portal_session = result["session"]  # prevent GC
    fd = result["fd"]
    streams = result["streams"]
    node_id = streams[0][0]
    print(f"Portal streams: {streams}")
    print(f"PipeWire node: {node_id}, fd: {fd}")
    return fd, node_id


class Caster:
    def __init__(self, pw_fd, pw_node, crop, server_url):
        self.server = server_url.rstrip("/")
        self.loop = GLib.MainLoop()
        self.crop = crop

        crop_el = "videocrop name=crop ! " if crop else ""
        self.pipeline = Gst.parse_launch(
            f"pipewiresrc name=pwsrc "
            f"  do-timestamp=true keepalive-time=1000 ! "
            f"videoconvert ! {crop_el}"
            f"vp8enc deadline=1 target-bitrate=8000000 cpu-used=4 "
            f"  keyframe-max-dist=60 ! "
            f"rtpvp8pay picture-id-mode=1 ! "
            f"application/x-rtp,media=video,encoding-name=VP8,payload=96 ! "
            f"webrtcbin name=webrtc bundle-policy=max-bundle "
            f"  stun-server=stun://stun.l.google.com:19302"
        )
        pwsrc = self.pipeline.get_by_name("pwsrc")
        pwsrc.set_property("fd", pw_fd)
        print(f"pipewiresrc: fd={pw_fd} (no path, auto-detect from portal fd)")

        self.webrtc = self.pipeline.get_by_name("webrtc")
        self.webrtc.connect("on-negotiation-needed", self._on_negotiation_needed)
        self.webrtc.connect("notify::ice-gathering-state", self._on_ice_state)
        self.webrtc.connect("notify::ice-connection-state", self._on_conn_state)

        if crop:
            pad = self.pipeline.get_by_name("crop").get_static_pad("sink")
            pad.add_probe(Gst.PadProbeType.BUFFER, self._set_crop)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error)

    def _set_crop(self, pad, _info):
        caps = pad.get_current_caps()
        if not caps:
            return Gst.PadProbeReturn.OK
        s = caps.get_structure(0)
        _, sw = s.get_int("width")
        _, sh = s.get_int("height")
        x, y, w, h = self.crop
        c = self.pipeline.get_by_name("crop")
        c.set_property("left", x)
        c.set_property("top", y)
        c.set_property("right", max(0, sw - x - w))
        c.set_property("bottom", max(0, sh - y - h))
        print(f"Crop: ({x},{y}) {w}x{h} from {sw}x{sh}")
        return Gst.PadProbeReturn.REMOVE

    def _on_negotiation_needed(self, _webrtc):
        print("Creating offer...")
        promise = Gst.Promise.new_with_change_func(self._on_offer_created)
        self.webrtc.emit("create-offer", None, promise)

    def _on_offer_created(self, promise):
        reply = promise.get_reply()
        offer = reply.get_value("offer")
        self.webrtc.emit("set-local-description", offer, Gst.Promise.new())

    def _on_ice_state(self, webrtc, _pspec):
        state = webrtc.get_property("ice-gathering-state")
        if state == GstWebRTC.WebRTCICEGatheringState.COMPLETE:
            GLib.idle_add(self._send_offer)

    def _send_offer(self):
        desc = self.webrtc.get_property("local-description")
        sdp = desc.sdp.as_text()
        body = json.dumps({"type": "offer", "sdp": sdp}).encode()
        req = urllib.request.Request(
            f"{self.server}/offer",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req)
        print("Offer posted — waiting for tablet...")
        threading.Thread(target=self._poll_answer, daemon=True).start()
        return False

    def _poll_answer(self):
        while True:
            try:
                r = urllib.request.urlopen(f"{self.server}/answer")
                if r.status == 200:
                    answer = json.loads(r.read())
                    GLib.idle_add(self._set_answer, answer)
                    return
            except Exception:
                pass
            time.sleep(0.5)

    def _set_answer(self, answer):
        _, msg = GstSdp.SDPMessage.new_from_text(answer["sdp"])
        desc = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.ANSWER, msg
        )
        self.webrtc.emit("set-remote-description", desc, Gst.Promise.new())
        print("Connected — streaming!")
        return False

    def _on_conn_state(self, webrtc, _pspec):
        state = webrtc.get_property("ice-connection-state")
        if state in (
            GstWebRTC.WebRTCICEConnectionState.DISCONNECTED,
            GstWebRTC.WebRTCICEConnectionState.FAILED,
        ):
            print(f"ICE {state.value_nick} — stopping")
            self.loop.quit()

    def _on_error(self, _bus, msg):
        err, debug = msg.parse_error()
        print(f"Pipeline error: {err.message}", file=sys.stderr)
        if debug:
            print(f"  Debug: {debug}", file=sys.stderr)
        self.loop.quit()

    def run(self):
        try:
            urllib.request.urlopen(
                urllib.request.Request(f"{self.server}/reset", method="POST")
            )
        except Exception:
            pass

        self.pipeline.set_state(Gst.State.PLAYING)
        print("Pipeline running...")
        try:
            self.loop.run()
        except KeyboardInterrupt:
            print("\nStopping...")
        self.pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Stream a screen region to the tablet via WebRTC"
    )
    parser.add_argument("--crop", metavar="X,Y,W,H", help="Crop region (pixels)")
    parser.add_argument(
        "--server", default="http://localhost:8080", help="Signaling server URL"
    )
    parser.add_argument("-p", "--port", type=int, help="Server port (localhost)")
    args = parser.parse_args()

    crop = None
    if args.crop:
        parts = args.crop.split(",")
        if len(parts) != 4:
            sys.exit("--crop requires X,Y,W,H")
        crop = tuple(int(p) for p in parts)

    server = args.server
    if args.port:
        server = f"http://localhost:{args.port}"

    Gst.init(None)
    print("Requesting screen capture (portal dialog will appear)...")
    pw_fd, pw_node = portal_screencast()
    print(f"Got PipeWire stream (node {pw_node})")

    # Check if the node actually exists in PipeWire right now
    import subprocess

    check = subprocess.run(
        ["pw-cli", "info", str(pw_node)], capture_output=True, text=True
    )
    print(f"pw-cli info {pw_node}:")
    for line in check.stdout.strip().split("\n"):
        print(f"  {line}")

    # Also get the node serial
    dump = subprocess.run(
        ["pw-dump", str(pw_node)], capture_output=True, text=True
    )
    # Extract serial from JSON
    import re

    serial_match = re.search(r'"object.serial"\s*:\s*"?(\d+)"?', dump.stdout)
    if serial_match:
        serial = serial_match.group(1)
        print(f"Node serial: {serial}")
    else:
        serial = None
        print("Could not find serial")

    # Also check if session is alive
    print(f"Portal session alive: {_portal_session is not None}")

    # Try multiple pipewiresrc configurations to find one that works
    # Build stream-properties with node.target to bypass registry lookup
    stream_props = Gst.Structure.new_empty("stream-properties")

    attempts = [
        ("portal fd + path", {"fd": pw_fd, "path": str(pw_node)}),
        ("portal fd only (autoconnect)", {"fd": pw_fd}),
        ("default connection + path", {"path": str(pw_node)}),
        ("default connection + target-object (id)", {"target-object": str(pw_node)}),
    ]
    if serial:
        attempts.append(
            ("default connection + target-object (serial)", {"target-object": serial})
        )
        attempts.append(
            ("portal fd + target-object (serial)", {"fd": pw_fd, "target-object": serial})
        )
    # Add stream-properties based attempts
    attempts.append(
        ("portal fd + stream-props node.target", {"fd": pw_fd, "_stream_target": str(pw_node)})
    )
    if serial:
        attempts.append(
            ("portal fd + stream-props node.target (serial)", {"fd": pw_fd, "_stream_target": serial})
        )

    for desc, props in attempts:
        fd_to_use = os.dup(pw_fd) if "fd" in props else -1
        print(f"  Trying: {desc}...")
        test = Gst.parse_launch("pipewiresrc name=src ! fakesink")
        src = test.get_by_name("src")
        for k, v in props.items():
            if k == "fd" and fd_to_use >= 0:
                src.set_property("fd", fd_to_use)
            elif k == "_stream_target":
                sp = Gst.Structure.new_empty("stream-properties")
                sp.set_value("node.target", v)
                src.set_property("stream-properties", sp)
            else:
                src.set_property(k, v)

        bus = test.get_bus()
        test.set_state(Gst.State.PLAYING)
        msg = bus.timed_pop_filtered(
            5 * Gst.SECOND,
            Gst.MessageType.ASYNC_DONE | Gst.MessageType.ERROR,
        )
        if msg and msg.type == Gst.MessageType.ERROR:
            err, _ = msg.parse_error()
            print(f"    FAILED: {err.message}")
        else:
            print(f"    OK!")
        test.set_state(Gst.State.NULL)

    sys.exit(0)  # just testing for now
