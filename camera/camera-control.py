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
    camera-control.py zoom set [SEC]    # Zoom out fully, then in for SEC (default: 0.9)
    camera-control.py refocus           # Nudge zoom in/out to trigger AF-C refocus
    camera-control.py status            # Show camera status (zoom pos, focus, etc.)
    camera-control.py apis              # List all available API methods
    camera-control.py reconnect         # Wait for camera after WiFi drop, restore zoom
    camera-control.py start             # Just startRecMode (no zoom change)
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
DEFAULT_ZOOM_DURATION = 0.8

log = logging.getLogger("camera-control")


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


def zoom_timed(endpoint, direction, duration):
    """Zoom in/out for a duration. Returns final position. Only 2 API calls."""
    api_call(endpoint, "actZoom", [direction, "start"])
    time.sleep(duration)
    api_call(endpoint, "actZoom", [direction, "stop"])
    time.sleep(0.3)
    return get_zoom_position(endpoint)


def cmd_zoom_set(endpoint, duration=None):
    """Zoom out fully, then zoom in for DEFAULT_ZOOM_DURATION."""
    if duration is None:
        duration = DEFAULT_ZOOM_DURATION
    zoom_timed(endpoint, "out", 10)
    pos = zoom_timed(endpoint, "in", duration)
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


def cmd_reconnect(endpoint):
    """Wait for camera after WiFi reconnect, start rec mode, restore zoom."""
    max_wait = 30
    for attempt in range(max_wait // 2):
        result = api_call(endpoint, "startRecMode", exit_on_error=False)
        if result is not None:
            print("Camera connected, rec mode started.")
            for retry in range(3):
                time.sleep(3)
                try:
                    pos = zoom_timed(endpoint, "in", DEFAULT_ZOOM_DURATION)
                    print(f"Zoom set to {pos}/100")
                    return
                except SystemExit:
                    if retry < 2:
                        print(f"Zoom not ready, retrying ({retry + 2}/3)...")
            print("Zoom restore failed after 3 attempts")
            return
        time.sleep(2)
    print(f"Camera not reachable after {max_wait}s")
    sys.exit(1)



def cmd_start(endpoint):
    """Check camera state; start rec mode only if needed. No zoom change."""
    result = api_call(endpoint, "getEvent", [False], exit_on_error=False)
    if result is None:
        print("Camera not reachable.")
        sys.exit(1)
    status = result[1].get("cameraStatus") if isinstance(result[1], dict) else None
    if status == "NotReady":
        print(f"Camera status: {status} — recovering")
        api_call(endpoint, "startRecMode", exit_on_error=False)
        print("Rec mode started.")
    else:
        print(f"Camera status: {status} — no recovery needed")


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
    elif cmd == "apis":
        cmd_apis(endpoint)
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
