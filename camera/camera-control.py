#!/usr/bin/env python3
"""Control Sony A6300 via Camera Remote API (WiFi JSON-RPC).

Launch Smart Remote Embedded on the camera, connect your computer to the
camera's WiFi AP (DIRECT-xxxx:ILCE-6300), then run this script.

Usage:
    camera-control.py discover          # Find camera and show available APIs
    camera-control.py zoom              # Show current zoom position
    camera-control.py zoom in           # Zoom in one step
    camera-control.py zoom out          # Zoom out one step
    camera-control.py zoom in start     # Continuous zoom in (send 'stop' to end)
    camera-control.py zoom stop         # Stop continuous zoom
    camera-control.py zoom in 3s        # Smooth zoom in for 3 seconds
    camera-control.py zoom set [SEC]    # Zoom out fully, then in for SEC (default: 1.2s)
    camera-control.py refocus           # Nudge zoom in/out to trigger AF-C refocus
    camera-control.py status            # Show camera status (zoom pos, focus, etc.)
    camera-control.py apis              # List all available API methods
    camera-control.py reconnect         # Wait for camera after WiFi drop, recover if reset
    camera-control.py start             # startRecMode + zoom restore, only if camera reset
    camera-control.py keepalive         # Poll camera every 10s to prevent WiFi idle kick
"""

import json
import logging
import socket
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# Shared config lives in ../lib; make it importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import teleprompter_config  # noqa: E402

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_ST = "urn:schemas-sony-com:service:ScalarWebAPI:1"
SSDP_TIMEOUT = 3

# Endpoint comes from config (env or ~/.config/teleprompter-mirror/config.env),
# falling back to the A6300's fixed soft-AP address.
DEFAULT_ENDPOINT = teleprompter_config.get("TELEPROMPTER_CAMERA_ENDPOINT")
DEFAULT_ZOOM_DURATION = 1.2
KEEPALIVE_INTERVAL = 10  # seconds between read-only getEvent polls

# Timed zoom control assumes actZoom start/stop commands round-trip quickly: the
# motor runs for the wall-clock gap between them. Over a degraded camera link
# those calls can block for seconds (2-7s observed during a weak-signal
# reconnect), so the motor runs uncontrolled and overshoots. Treat any actZoom
# slower than this as "timing unreliable" and abort the move rather than trust it.
ZOOM_LATENCY_LIMIT = 2.0  # seconds
# A restore zooms fully out then in for DEFAULT_ZOOM_DURATION (~38/100 on the
# E PZ 16-50mm). A final position far above that means the 'stop' command was
# delayed and the lens over-ran (stranded at 100/100 in the wild). Reject such a
# result and retry instead of reporting it as a successful restore.
ZOOM_RESTORE_CEILING = 75

log = logging.getLogger("camera-control")


class ZoomTimingError(RuntimeError):
    """Timed zoom control was unreliable — link too slow, or result implausible.

    Raised by the timed-zoom helpers so callers (restore_zoom's backoff loop,
    interactive `zoom set`) can retry or fail cleanly instead of trusting a
    duration that no longer maps to actual motor runtime.
    """


def _init_logging():
    logging.basicConfig(
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
        level=logging.INFO,
        stream=sys.stdout,
    )
    sys.stdout.reconfigure(line_buffering=True)


def discover():
    """Find camera via SSDP and return the API endpoint base URL."""
    msg = "\r\n".join([
        "M-SEARCH * HTTP/1.1",
        f"HOST: {SSDP_ADDR}:{SSDP_PORT}",
        'MAN: "ssdp:discover"',
        "MX: 1",
        f"ST: {SSDP_ST}",
        "", "",
    ])
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(SSDP_TIMEOUT)
    try:
        sock.sendto(msg.encode(), (SSDP_ADDR, SSDP_PORT))
        data, addr = sock.recvfrom(4096)
    except socket.timeout:
        return None, None
    finally:
        sock.close()

    response = data.decode()
    location = None
    for line in response.splitlines():
        if line.upper().startswith("LOCATION:"):
            location = line.split(":", 1)[1].strip()
            break

    if not location:
        return None, None

    with urllib.request.urlopen(location, timeout=5) as resp:
        dd_xml = resp.read().decode()

    return parse_device_descriptor(dd_xml)


def parse_device_descriptor(dd_xml):
    """Extract (model, camera API endpoint) from a UPnP device descriptor.

    Returns the model name (or "Unknown") and the camera service's
    ActionList URL, or None for the endpoint if no camera service is present.
    """
    ns = {
        "av": "urn:schemas-sony-com:av",
        "upnp": "urn:schemas-upnp-org:device-1-0",
    }
    root = ET.fromstring(dd_xml)

    model = root.findtext(".//upnp:modelName", default="Unknown", namespaces=ns)

    endpoint = None
    for svc in root.findall(".//av:X_ScalarWebAPI_Service", namespaces=ns):
        svc_type = svc.findtext("av:X_ScalarWebAPI_ServiceType", namespaces=ns)
        if svc_type == "camera":
            endpoint = svc.findtext("av:X_ScalarWebAPI_ActionList_URL", namespaces=ns)
            break

    return model, endpoint


def api_call(endpoint, method, params=None, version="1.0", exit_on_error=True):
    """Make a JSON-RPC call to the camera."""
    payload = {
        "method": method,
        "params": params or [],
        "id": 1,
        "version": version,
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{endpoint}/camera",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        result = json.loads(e.read())
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        elapsed = (time.monotonic() - t0) * 1000
        log.info("api %s -> ERR %.0fms %s", method, elapsed, e)
        if not exit_on_error:
            return None
        print(f"Connection failed: {e}")
        print("Is the camera's WiFi connected and Smart Remote running?")
        sys.exit(1)

    elapsed = (time.monotonic() - t0) * 1000
    if "error" in result:
        code, msg = result["error"]
        log.info("api %s -> ERR %.0fms [%s] %s", method, elapsed, code, msg)
        if not exit_on_error:
            return None
        print(f"Error {code}: {msg}")
        sys.exit(1)

    log.info("api %s -> OK %.0fms", method, elapsed)
    return result.get("result", [])


def cmd_discover(endpoint):
    """Discover camera and print info."""
    model, discovered = discover()
    if discovered:
        print(f"Found: {model}")
        print(f"Endpoint: {discovered}")
    else:
        print(f"SSDP discovery failed, using default: {DEFAULT_ENDPOINT}")
    ep = discovered or endpoint
    print("\nInitializing rec mode...")
    api_call(ep, "startRecMode")
    print("Rec mode started.")
    result = api_call(ep, "getAvailableApiList")
    apis = result[0] if result else []
    print(f"\n{len(apis)} available methods:")
    for name in sorted(apis):
        print(f"  {name}")


def get_zoom_position(endpoint):
    """Return the current zoom position (0-100)."""
    result = api_call(endpoint, "getEvent", [False])
    for item in result:
        if isinstance(item, dict) and item.get("type") == "zoomInformation":
            return item["zoomPosition"]
    return 0


def cmd_zoom(endpoint, direction, movement="1shot"):
    """Control power zoom."""
    if direction == "stop":
        direction, movement = "in", "stop"
    if direction == "set":
        duration = float(movement) if movement != "1shot" else DEFAULT_ZOOM_DURATION
        cmd_zoom_set(endpoint, duration)
        return
    if movement.endswith("s"):
        duration = float(movement[:-1])
        api_call(endpoint, "actZoom", [direction, "start"])
        time.sleep(duration)
        api_call(endpoint, "actZoom", [direction, "stop"])
        pos = get_zoom_position(endpoint)
        print(f"Zoom {direction} {duration}s: stopped at {pos}/100")
        return
    api_call(endpoint, "actZoom", [direction, movement])
    print(f"Zoom {direction} {movement}: OK")


def _actzoom(endpoint, direction, phase):
    """Send one actZoom command; return its round-trip latency in seconds."""
    t0 = time.monotonic()
    api_call(endpoint, "actZoom", [direction, phase])
    return time.monotonic() - t0


def zoom_timed(endpoint, direction, duration):
    """Zoom for `duration` seconds and return the final position.

    Timed control only works if the actZoom start/stop commands round-trip
    quickly — the motor runs for the wall-clock gap between them. Measure each
    command and raise ZoomTimingError if it exceeds ZOOM_LATENCY_LIMIT so the
    caller can back off instead of trusting a duration that no longer reflects
    motor runtime. On a slow start the motor is already running uncontrolled, so
    stop it immediately (skip the hold) to limit the over-run before raising.
    """
    start_rtt = _actzoom(endpoint, direction, "start")
    if start_rtt <= ZOOM_LATENCY_LIMIT:
        time.sleep(duration)
    stop_rtt = _actzoom(endpoint, direction, "stop")
    worst = max(start_rtt, stop_rtt)
    if worst > ZOOM_LATENCY_LIMIT:
        raise ZoomTimingError(
            f"actZoom {direction} RTT {worst:.1f}s > {ZOOM_LATENCY_LIMIT}s "
            "— timed control unreliable")
    time.sleep(0.3)
    return get_zoom_position(endpoint)


def cmd_zoom_set(endpoint, duration=None):
    """Zoom out fully, then zoom in for DEFAULT_ZOOM_DURATION."""
    if duration is None:
        duration = DEFAULT_ZOOM_DURATION
    try:
        zoom_timed(endpoint, "out", 10)
        pos = zoom_timed(endpoint, "in", duration)
    except ZoomTimingError as e:
        print(f"Camera link too slow to set zoom precisely: {e}")
        print("Try again once the WiFi link settles (check 'iw dev wlan0 link').")
        sys.exit(1)
    print(f"Zoom set to {pos}/100")


def cmd_refocus(endpoint):
    """Nudge zoom to trigger AF-C refocus, then return."""
    api_call(endpoint, "actZoom", ["in", "1shot"])
    time.sleep(0.5)
    api_call(endpoint, "actZoom", ["out", "1shot"])
    time.sleep(0.3)
    pos = get_zoom_position(endpoint)
    print(f"Refocus done, zoom at {pos}/100")


def cmd_status(endpoint):
    """Poll camera event for current state."""
    result = api_call(endpoint, "getEvent", [False])
    for i, item in enumerate(result):
        if item is None:
            continue
        if isinstance(item, dict):
            t = item.get("type", "")
            if t in ("zoomInformation", "focusStatus", "focusMode",
                      "cameraStatus", "shootMode"):
                print(f"{t}: {json.dumps(item, indent=2)}")
        elif isinstance(item, list):
            for sub in item:
                if isinstance(sub, dict):
                    t = sub.get("type", "")
                    if t in ("zoomInformation", "focusStatus", "focusMode",
                              "cameraStatus", "shootMode"):
                        print(f"{t}: {json.dumps(sub, indent=2)}")


ZOOM_RETRY_DELAYS = [3, 5, 8, 13, 21]


def zoom_to_zero(endpoint):
    """Zoom out to 0. Returns True on success."""
    zoom_timed(endpoint, "out", 2)
    pos = get_zoom_position(endpoint)
    if pos > 0:
        print(f"Zoom still at {pos}/100 after 2s out, extending")
        zoom_timed(endpoint, "out", 3)
        pos = get_zoom_position(endpoint)
    if pos > 0:
        print(f"Zoom still at {pos}/100 — could not reach 0")
        return False
    return True


def restore_zoom(endpoint):
    """Reset zoom to default with fibonacci-style backoff. Returns True on success.

    Each attempt zooms fully out then in for DEFAULT_ZOOM_DURATION. The backoff
    gives a still-settling link time to recover: a slow actZoom raises
    ZoomTimingError, an unreachable camera raises SystemExit, and an implausibly
    high final position (the 'stop' arrived late and the lens over-ran) is
    rejected too — all three just trigger the next, longer retry rather than
    leaving the lens parked at the wrong focal length.
    """
    elapsed = 0
    for i, delay in enumerate(ZOOM_RETRY_DELAYS):
        time.sleep(delay)
        elapsed += delay
        attempt = f"attempt {i + 1}/{len(ZOOM_RETRY_DELAYS)}, {elapsed}s"
        try:
            if not zoom_to_zero(endpoint):
                raise ZoomTimingError("could not reach zoom 0")
            pos = zoom_timed(endpoint, "in", DEFAULT_ZOOM_DURATION)
            if pos > ZOOM_RESTORE_CEILING:
                raise ZoomTimingError(f"overshot to {pos}/100 (stop delayed)")
            print(f"Zoom restored to {pos}/100 ({attempt})")
            return True
        except ZoomTimingError as e:
            print(f"Zoom not ready ({attempt}): {e}")
        except SystemExit:
            print(f"Zoom not ready ({attempt})")
    print(f"Zoom restore failed after {len(ZOOM_RETRY_DELAYS)} attempts ({elapsed}s)")
    return False


def cmd_reconnect(endpoint):
    """Wait for the camera after a WiFi reconnect, then recover iff it reset.

    The `up` dispatcher calls this the moment camera WiFi comes up, when the
    camera may not answer yet — so poll (probing with a read-only getEvent) up
    to max_wait seconds. Once reachable, delegate to the same self-gating logic
    as `start`: only a camera that actually reset to NotReady gets startRecMode +
    zoom restore. Previously this restored zoom unconditionally, which corrupted
    a perfectly good zoom on every brief RF flap — and, because a reconnect rides
    in on a still-weak link, the timed restore could strand the lens at 100/100.
    """
    max_wait = 30
    for _ in range(max_wait // 2):
        result = api_call(endpoint, "getEvent", [False], exit_on_error=False)
        if result is not None:
            _recover_if_reset(endpoint, result)
            return
        time.sleep(2)
    print(f"Camera not reachable after {max_wait}s")
    sys.exit(1)



def parse_camera_state(result):
    """Extract (cameraStatus, zoomPosition) from a getEvent result list.

    Pure helper: takes the already-decoded getEvent array and returns the
    camera status string (or None) and zoom position (or None), locating each
    by item type rather than fixed indices so it tolerates missing/reordered
    slots. This is the decision the re-association recovery hinges on, so it's
    kept free of I/O and unit-tested.
    """
    status = None
    zoom = None
    for item in result or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "cameraStatus":
            status = item.get("cameraStatus")
        elif item.get("type") == "zoomInformation":
            zoom = item.get("zoomPosition")
    return status, zoom


def _recover_if_reset(endpoint, result):
    """Run startRecMode + zoom restore only if the camera reports NotReady.

    Shared by `start` and `reconnect`. A brief RF drop leaves Smart Remote
    running — the camera stays IDLE with its zoom intact — so recovery must be
    gated on the camera having actually reset (e.g. a power-loss drop). Restoring
    unconditionally clobbers a good zoom; over the still-weak link a reconnect
    rides in on, the timed restore can even drive the lens to 100/100.
    """
    status, zoom = parse_camera_state(result)
    if status == "NotReady":
        api_call(endpoint, "startRecMode", exit_on_error=False)
        print("Camera was NotReady — recovering")
        restore_zoom(endpoint)
    else:
        print(f"Camera status: {status} (zoom: {zoom}) — no recovery needed")


def cmd_start(endpoint):
    """Check camera state; start rec mode only if the camera reset.

    Self-gating, so it's safe to call on any re-association or DHCP change:
    runs startRecMode + zoom restore only when the camera reports NotReady
    (e.g. a power-loss drop reset Smart Remote); otherwise just logs state.
    """
    result = api_call(endpoint, "getEvent", [False], exit_on_error=False)
    if result is None:
        print("Camera not reachable.")
        sys.exit(1)
    _recover_if_reset(endpoint, result)


def cmd_keepalive(endpoint):
    """Poll camera periodically to prevent WiFi inactivity disconnect."""
    print(f"Keepalive started (interval {KEEPALIVE_INTERVAL}s)")
    while True:
        time.sleep(KEEPALIVE_INTERVAL)
        result = api_call(endpoint, "getEvent", [False], exit_on_error=False)
        if result is None:
            print("keepalive: camera unreachable")
        else:
            print("keepalive: OK")


def cmd_apis(endpoint):
    """List all available API methods."""
    result = api_call(endpoint, "getAvailableApiList")
    apis = result[0] if result else []
    for name in sorted(apis):
        print(name)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    _init_logging()
    cmd = sys.argv[1]
    endpoint = DEFAULT_ENDPOINT

    if cmd == "discover":
        cmd_discover(endpoint)
    elif cmd == "zoom":
        if len(sys.argv) < 3:
            pos = get_zoom_position(endpoint)
            print(f"{pos}/100")
            sys.exit(0)
        direction = sys.argv[2]
        movement = sys.argv[3] if len(sys.argv) > 3 else "1shot"
        cmd_zoom(endpoint, direction, movement)
    elif cmd == "refocus":
        cmd_refocus(endpoint)
    elif cmd == "status":
        cmd_status(endpoint)
    elif cmd == "reconnect":
        cmd_reconnect(endpoint)
    elif cmd == "start":
        cmd_start(endpoint)
    elif cmd == "keepalive":
        cmd_keepalive(endpoint)
    elif cmd == "apis":
        cmd_apis(endpoint)
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
