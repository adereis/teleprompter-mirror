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
    camera-control.py zoom set          # Park the lens at TELEPROMPTER_CAMERA_ZOOM_TARGET
    camera-control.py zoom set 1.2      # Zoom out fully, then in for 1.2s (open loop)
    camera-control.py refocus           # Nudge zoom in/out to trigger AF-C refocus
    camera-control.py status            # Show camera status (zoom pos, focus, etc.)
    camera-control.py apis              # List all available API methods
    camera-control.py reconnect         # Wait for camera after WiFi drop, recover if reset
    camera-control.py start             # startRecMode + zoom restore, only if camera reset
    camera-control.py keepalive         # Poll every 10s; recover if the camera stays reset

Commands that move the lens or restart Smart Remote (zoom with a direction,
refocus, reconnect, start) take an exclusive lock first, so two of them can
never drive the motor at once. Read-only commands never take it.
"""

import contextlib
import fcntl
import json
import logging
import math
import os
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
KEEPALIVE_INTERVAL = 10  # seconds between read-only getEvent polls

# Sony error code returned by every actuating method while the camera is not in
# rec mode. It means Smart Remote reset underneath us — a different failure from
# a slow or unreachable link, and one that backing off cannot fix.
ERR_NOT_AVAILABLE_NOW = 1

# Timed zoom control assumes actZoom start/stop commands round-trip quickly: the
# motor runs for the wall-clock gap between them. Over a degraded camera link
# those calls can block for seconds (2-7s observed during a weak-signal
# reconnect), so the motor runs uncontrolled and overshoots. Treat any actZoom
# slower than this as "timing unreliable" and abort the move rather than trust it.
ZOOM_LATENCY_LIMIT = 2.0  # seconds
# A restore drives the lens to ZOOM_TARGET. A position far above that means the
# 'stop' command was delayed and the lens over-ran (stranded at 100/100 in the
# wild). Reject such a result and retry instead of reporting it as a successful
# restore.
ZOOM_RESTORE_CEILING = 75

# Where a recovery parks the lens, and how precisely. Open-loop timed restores
# did not repeat: the same 1.2s 'in' from zero landed at 54, 48 and 45 on three
# consecutive recoveries, so every recovery quietly re-framed the shot. Drive to
# a measured position instead.
#
# The E PZ 16-50mm has a minimum travel rather than a minimum pulse width:
# measured commands of 0.05s, 0.08s, 0.10s and 0.15s each moved the lens about
# 10 of 100 positions, so once the motor starts it completes a fixed increment
# however briefly it was told to run. The lens therefore cannot be parked more
# finely than that, and no amount of code will change it.
ZOOM_MIN_INCREMENT = 10
# Correcting an error smaller than half the minimum increment would overshoot
# by more than it fixes, so stop there.
ZOOM_TOLERANCE = ZOOM_MIN_INCREMENT // 2
# Above this pulse width travel is proportional to time (0.3s moved 12, 1.2s
# moves about 45). Only moves at least this long say anything about the motor's
# speed: calibrating from a 0.05s pulse that moved 10 would read 200 units/s
# and wreck every step after it.
ZOOM_LINEAR_MIN_STEP = 0.4
# Starting guess for the lens speed, in zoom units per motor-second. Only the
# first step uses it; the loop re-measures as it goes, which matters more than
# the constant's accuracy — a fixed figure that underestimates the lens by 2x
# makes a proportional controller overshoot, reverse, and oscillate forever.
ZOOM_RATE_GUESS = 45 / 1.2
ZOOM_MIN_STEP = 0.05   # shortest pulse worth sending; anything less is the same
ZOOM_MAX_STEP = 1.5    # cap a single open-loop leg
ZOOM_MAX_STEPS = 12

# A recovery re-checks the camera before releasing the lens lock, and repeats if
# it reset again mid-recovery. Bounded, because a camera whose AP has wedged
# needs a power cycle and no number of passes will reach it.
RECOVERY_PASSES = 3
STATUS_POLL_ATTEMPTS = 5
STATUS_POLL_DELAY = 2  # seconds

# The keepalive is the only process guaranteed to be running whenever camera
# WiFi is up, so it doubles as the supervisor of last resort. It escalates only
# after the camera has looked NotReady for this many consecutive polls — long
# enough for a dispatcher-spawned recovery to take the lock and do the work —
# and then no more often than the cooldown, so a camera that needs a physical
# power cycle is not hammered.
KEEPALIVE_NOTREADY_THRESHOLD = 3
KEEPALIVE_RECOVERY_COOLDOWN = 120  # seconds

log = logging.getLogger("camera-control")


class ZoomTimingError(RuntimeError):
    """Timed zoom control was unreliable — link too slow, or result implausible.

    Raised by the timed-zoom helpers so callers (restore_zoom's backoff loop,
    interactive `zoom set`) can retry or fail cleanly instead of trusting a
    duration that no longer maps to actual motor runtime.
    """


class CameraReset(RuntimeError):
    """The camera left rec mode underneath us — Smart Remote reset.

    Distinct from ZoomTimingError on purpose. A slow link is answered by waiting
    longer; a reset is answered by calling startRecMode again, and waiting does
    nothing at all. Collapsing the two is what left the lens being zoomed at a
    camera that had already reset: 'Not Available Now' looked like one more
    transient failure, so the backoff loop spent its remaining attempts on a
    remedy that could not work.
    """


# Two lens commands running at once is not a race over a variable — it is two
# processes driving one motor. Timed zoom is the exposed part: zoom_timed
# assumes the gap between its own start and stop is the motor's runtime, which
# stops being true the moment another process interleaves its own start/stop
# pair. A WiFi flap produces exactly that, because every `up` and
# `dhcp4-change` spawns a recovery and a slow link makes each one outlive the
# next event. Three concurrent restores were observed in the wild during a
# five-flap minute, each independently reading a position the others were
# already correcting, leaving the lens four steps off target. ZOOM_LATENCY_LIMIT
# cannot catch this: it measures one caller's own round trips and sees nothing
# of a second actuator. Serialize instead.


class LensBusy(RuntimeError):
    """Another process holds the lens lock; this invocation must not proceed."""


def lock_path():
    """Path to the lens lock file.

    Under the same ~/.config directory as the project's other state rather
    than XDG_RUNTIME_DIR, which is where a lock would normally belong: the NM
    dispatcher runs this script through `runuser`, which sets HOME but leaves
    XDG_RUNTIME_DIR unset, so a runtime-dir path would resolve one way for a
    dispatcher-spawned recovery and another for a command typed in a terminal
    — two lock files and no mutual exclusion between the two callers that most
    need it. The file outliving a reboot costs nothing, because flock state
    lives in the kernel rather than in the file's contents.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "teleprompter-mirror" / "camera.lock"


def actuates_lens(argv):
    """True if this invocation must hold the lens lock for its whole run.

    Pure, so the lock's scope is pinned by tests rather than by reading main().
    `zoom` with no direction only reports the current position and must stay
    answerable while a recovery holds the lock; every form that can send
    actZoom or startRecMode has to wait its turn.

    `keepalive` is false despite being able to recover, and deliberately so: it
    runs for as long as camera WiFi is up, so holding the lock process-wide
    would lock out every dispatcher recovery and every command typed in a
    terminal for hours. It takes the lock around its escalation only.
    """
    if not argv:
        return False
    if argv[0] == "zoom":
        return len(argv) > 1
    return argv[0] in ("refocus", "reconnect", "start")


def _lock_holder(fd):
    """Best-effort description of the current holder, for the declined message."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        pid = os.read(fd, 32).decode(errors="replace").strip()
    except OSError:
        pid = ""
    return f"pid {pid}" if pid.isdigit() else "another process"


@contextlib.contextmanager
def lens_lock(path=None):
    """Hold the exclusive lens lock for the body, or raise LensBusy.

    Non-blocking on purpose. A second recovery that queued behind the first
    would act on a camera state it read minutes earlier, and would begin its
    own timed zoom just as the holder finished one; declining immediately
    leaves the outcome to the process already doing the work. The holder's pid
    is written into the file so a declined run can name it.
    """
    path = lock_path() if path is None else Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise LensBusy(_lock_holder(fd)) from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        yield
    finally:
        os.close(fd)  # releases the flock, however the body exited


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


def api_call(endpoint, method, params=None, version="1.0", exit_on_error=True,
             detect_reset=False):
    """Make a JSON-RPC call to the camera.

    With detect_reset, a "Not Available Now" answer raises CameraReset instead
    of being reported as a generic error. Callers that actuate the lens want
    that distinction: it is the camera telling them Smart Remote is no longer
    in rec mode, which needs startRecMode rather than another retry.
    """
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
        if detect_reset and code == ERR_NOT_AVAILABLE_NOW:
            raise CameraReset(f"{method}: {msg}")
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
    _, position = parse_camera_state(result)
    if (isinstance(position, bool) or not isinstance(position, (int, float))
            or not math.isfinite(position) or not 0 <= position <= 100):
        print("Camera did not report a valid zoom position.")
        sys.exit(1)
    return position


def _zoom_duration(value):
    """Validate before starting the lens, including the homing leg of set."""
    duration = float(value)
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("Zoom duration must be a finite, nonnegative number.")
    return duration


def cmd_zoom(endpoint, direction, movement="1shot"):
    """Control power zoom."""
    if direction == "stop":
        direction, movement = "in", "stop"
    try:
        if direction == "set":
            cmd_zoom_set(endpoint,
                         None if movement == "1shot" else _zoom_duration(movement))
            return
        if movement.endswith("s"):
            duration = _zoom_duration(movement[:-1])
            pos = zoom_timed(endpoint, direction, duration)
            print(f"Zoom {direction} {duration}s: stopped at {pos}/100")
            return
    except (ValueError, ZoomTimingError) as error:
        print(f"Zoom failed: {error}")
        sys.exit(1)
    except CameraReset as error:
        print(f"Zoom failed: {error}")
        print("The camera left rec mode. Run 'camera-control.py start' to recover.")
        sys.exit(1)
    api_call(endpoint, "actZoom", [direction, movement])
    print(f"Zoom {direction} {movement}: OK")


def _actzoom(endpoint, direction, phase):
    """Send one actZoom command; return its round-trip latency in seconds.

    Raises CameraReset if the camera reports it is no longer in rec mode.
    """
    t0 = time.monotonic()
    api_call(endpoint, "actZoom", [direction, phase], detect_reset=True)
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
    duration = _zoom_duration(duration)
    try:
        start_rtt = _actzoom(endpoint, direction, "start")
        if start_rtt <= ZOOM_LATENCY_LIMIT:
            time.sleep(duration)
    finally:
        # A timed-out start may still have reached the camera. Attempt stop
        # even if start failed or the hold was interrupted with Ctrl-C.
        stop_rtt = _actzoom(endpoint, direction, "stop")
    worst = max(start_rtt, stop_rtt)
    if worst > ZOOM_LATENCY_LIMIT:
        raise ZoomTimingError(
            f"actZoom {direction} RTT {worst:.1f}s > {ZOOM_LATENCY_LIMIT}s "
            "— timed control unreliable")
    time.sleep(0.3)
    return get_zoom_position(endpoint)


def cmd_zoom_set(endpoint, duration=None):
    """Park the lens at the configured target, or by timed move if given seconds.

    With no duration this is the same closed-loop move a recovery makes, so
    `zoom set` and an automatic recovery land in the same place — which is the
    point of having a configured target at all. An explicit duration keeps the
    old open-loop behavior for exploring where a given motor time ends up.
    """
    # Validate before the homing leg: an invalid duration must not move the
    # lens at all, not strand it at 0 on the way to a move that cannot happen.
    duration = None if duration is None else _zoom_duration(duration)
    try:
        if duration is None:
            pos = zoom_to(endpoint, zoom_target())
        else:
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


def zoom_target():
    """The configured parking position for a recovery, validated.

    Read on demand rather than at import so a mistyped config value only fails
    the commands that move the lens; `status` and `keepalive` stay usable while
    the user fixes it.
    """
    target = teleprompter_config.get_int("TELEPROMPTER_CAMERA_ZOOM_TARGET")
    if not 0 <= target <= 100:
        raise ValueError(
            f"TELEPROMPTER_CAMERA_ZOOM_TARGET must be 0-100, got {target}")
    return target


def _step_duration(error, rate):
    """Motor seconds to cover `error` units of zoom at `rate`, clamped."""
    return min(ZOOM_MAX_STEP, max(ZOOM_MIN_STEP, abs(error) / rate))


def _close_enough(error):
    """True when the remaining error is not worth another pulse.

    The motor's minimum travel is about ZOOM_MIN_INCREMENT positions, so an
    error below half of that cannot be improved: the correction would land
    further from the target than staying put. Chasing it just alternates
    between overshooting in each direction until the step budget runs out.
    """
    return abs(error) <= ZOOM_TOLERANCE


def zoom_to(endpoint, target):
    """Drive the lens to `target`/100 and return the position actually reached.

    Closed loop on purpose. The old restore ran the motor for a fixed 1.2s from
    zero and accepted wherever it stopped, which is why three consecutive
    recoveries parked the same lens at 54, 48 and 45 — each one re-framing the
    shot a little more. Here every move is followed by a real position read, so
    the error shrinks instead of accumulating.

    The loop also calibrates itself: each move reports how far the lens really
    travelled for the time commanded, which is a direct measurement of the
    motor's speed, so the next step is sized from the lens's own behavior
    rather than from a constant that may be wrong for this lens or this
    firmware. Without that, an underestimated speed overshoots the target,
    reverses, overshoots again, and never settles.

    Raises ZoomTimingError if the lens will not settle, and propagates
    CameraReset untouched so the caller can restart rec mode.
    """
    if not zoom_to_zero(endpoint):
        raise ZoomTimingError("could not home the lens to 0")
    pos = 0
    rate = ZOOM_RATE_GUESS
    stalled = 0
    for _ in range(ZOOM_MAX_STEPS):
        error = target - pos
        if _close_enough(error):
            return pos
        direction = "in" if error > 0 else "out"
        duration = _step_duration(error, rate)
        moved = zoom_timed(endpoint, direction, duration)
        travelled = abs(moved - pos)
        if travelled:
            # Only long moves are proportional, so only they measure the motor.
            if duration >= ZOOM_LINEAR_MIN_STEP:
                rate = travelled / duration
            stalled = 0
        else:
            # Two commanded moves that change nothing means the motor is not
            # running — a mechanical limit, or a pulse too short to start it.
            # Say so instead of spending the budget on the same no-op.
            stalled += 1
            if stalled >= 2:
                raise ZoomTimingError(
                    f"lens stopped responding at {pos}/100 (target {target}/100)")
        pos = moved
    raise ZoomTimingError(
        f"lens settled at {pos}/100, {ZOOM_MAX_STEPS} steps short of {target}/100")


def restore_zoom(endpoint, target=None):
    """Park the lens at `target` with fibonacci-style backoff. True on success.

    The backoff gives a still-settling link time to recover: a slow actZoom
    raises ZoomTimingError, an unreachable camera raises SystemExit, and a
    position above ZOOM_RESTORE_CEILING (the 'stop' arrived late and the lens
    over-ran) is rejected too — all of which just trigger the next, longer retry
    rather than leaving the lens at the wrong focal length.

    A CameraReset is the one failure waiting cannot fix, so it is answered
    rather than merely retried: the camera dropped out of rec mode underneath
    us, so call startRecMode before the next attempt. Without this the loop
    spent all five attempts pushing actZoom at a camera that rejected every one
    of them with "Not Available Now", and reported a failed restore 50 seconds
    later while the real remedy was one call away.
    """
    target = zoom_target() if target is None else target
    elapsed = 0
    for i, delay in enumerate(ZOOM_RETRY_DELAYS):
        time.sleep(delay)
        elapsed += delay
        attempt = f"attempt {i + 1}/{len(ZOOM_RETRY_DELAYS)}, {elapsed}s"
        try:
            pos = zoom_to(endpoint, target)
            if pos > ZOOM_RESTORE_CEILING:
                raise ZoomTimingError(f"overshot to {pos}/100 (stop delayed)")
            print(f"Zoom restored to {pos}/100 ({attempt})")
            return True
        except CameraReset as e:
            print(f"Camera left rec mode during restore ({attempt}): {e}")
            if api_call(endpoint, "startRecMode", exit_on_error=False) is None:
                print("startRecMode failed; retrying after backoff")
            else:
                print("Rec mode restarted — retrying zoom")
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


def read_status(endpoint, attempts=STATUS_POLL_ATTEMPTS, delay=STATUS_POLL_DELAY):
    """Poll getEvent until the camera reports a status, or give up and return None.

    Read-only and cheap, so it is safe to call while holding the lens lock —
    which is exactly when it is needed, to find out whether the recovery that
    just ran actually took.
    """
    for i in range(attempts):
        if i:
            time.sleep(delay)
        result = api_call(endpoint, "getEvent", [False], exit_on_error=False)
        if result is not None:
            status, _ = parse_camera_state(result)
            if status is not None:
                return status
    return None


def _recover_if_reset(endpoint, result):
    """Run startRecMode + zoom restore only if the camera reports NotReady.

    Shared by `start` and `reconnect`. A brief RF drop leaves Smart Remote
    running — the camera stays IDLE with its zoom intact — so recovery must be
    gated on the camera having actually reset (e.g. a power-loss drop). Restoring
    unconditionally clobbers a good zoom; over the still-weak link a reconnect
    rides in on, the timed restore can even drive the lens to 100/100.

    When the camera has reset, recover in verified passes. The lens lock is
    non-blocking, so every recovery that fires while this one runs is declined
    and discarded; this process therefore has to be the one that finishes the
    job. Checking the camera again before releasing the lock is what makes that
    true. A WiFi flap storm broke the earlier single-pass version: it ran
    startRecMode, spent 50 seconds failing to restore zoom across a dying link,
    and turned away four later recoveries — including the one triggered by the
    good re-association that ended the storm — then exited leaving the camera
    NotReady with a healthy link and nothing scheduled to notice.
    """
    status, zoom = parse_camera_state(result)
    if status is None:
        print("Camera did not report its status; cannot decide whether to recover.")
        sys.exit(1)
    if status != "NotReady":
        print(f"Camera status: {status} (zoom: {zoom}) — no recovery needed")
        return
    if not recover(endpoint):
        sys.exit(1)


def recover(endpoint):
    """Bring a reset camera back, re-checking until it stays back. True if it did.

    Each pass restarts rec mode, parks the lens, and then asks the camera what
    state it is actually in. A pass that ends with the camera NotReady again
    means it reset a second time mid-recovery — common during a flap storm — so
    the work repeats rather than being reported as done.
    """
    for attempt in range(1, RECOVERY_PASSES + 1):
        pass_no = f"pass {attempt}/{RECOVERY_PASSES}"
        print(f"Camera was NotReady — recovering ({pass_no})")
        if api_call(endpoint, "startRecMode", exit_on_error=False) is None:
            print(f"startRecMode failed ({pass_no})")
            continue
        restored = restore_zoom(endpoint)
        status = read_status(endpoint)
        if status is None:
            print(f"Camera stopped answering after recovery ({pass_no})")
            continue
        if status == "NotReady":
            print(f"Camera reset again during recovery ({pass_no}) — repeating")
            continue
        if restored:
            print(f"Recovery complete — camera {status}")
            return True
        print(f"Camera is {status} but the lens was not restored ({pass_no})")
        return False
    print(f"Recovery failed after {RECOVERY_PASSES} passes")
    return False


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


def keepalive_should_escalate(notready_polls, since_last_recovery):
    """Decide whether the keepalive should attempt a recovery itself.

    Pure, so the escalation policy is pinned by tests rather than by reading
    the polling loop. True once the camera has looked NotReady for long enough
    that no dispatcher-spawned recovery is going to handle it, and not again
    until the cooldown has passed.
    """
    return (notready_polls >= KEEPALIVE_NOTREADY_THRESHOLD
            and since_last_recovery >= KEEPALIVE_RECOVERY_COOLDOWN)


def cmd_keepalive(endpoint):
    """Poll camera periodically to prevent WiFi inactivity disconnect.

    Polling is read-only, which is the property that made a 10s interval safe
    in the first place. The one exception is escalation: a recovery can fail
    and exit, and nothing else then looks at the camera until the next DHCP
    renewal roughly 27 minutes later — which is how a camera came to sit
    NotReady behind a perfectly healthy link. This loop already has the state
    needed to notice, so it recovers rather than only logging.

    It takes the lens lock for the recovery alone, never for the poll: holding
    it for the service's whole lifetime would block every dispatcher recovery
    and every command typed in a terminal. Being declined is a normal outcome
    here and means someone else is already on it.
    """
    print(f"Keepalive started (interval {KEEPALIVE_INTERVAL}s)")
    # A bad zoom target must not cost the user the keepalive itself: losing the
    # poll means the camera AP starts kicking the client for inactivity, which
    # is a worse failure than not being able to escalate. Report it loudly and
    # keep polling, but decline to escalate rather than crash mid-recovery.
    try:
        zoom_target()
        can_escalate = True
    except ValueError as error:
        print(f"Keepalive: escalation disabled, bad configuration: {error}")
        can_escalate = False
    notready_polls = 0
    last_recovery = -KEEPALIVE_RECOVERY_COOLDOWN
    while True:
        time.sleep(KEEPALIVE_INTERVAL)
        result = api_call(endpoint, "getEvent", [False], exit_on_error=False)
        if result is None:
            print("keepalive: camera unreachable")
            notready_polls = 0
            continue
        status, _ = parse_camera_state(result)
        if status != "NotReady":
            print("keepalive: OK")
            notready_polls = 0
            continue

        notready_polls += 1
        now = time.monotonic()
        print(f"keepalive: camera NotReady ({notready_polls} consecutive)")
        if not keepalive_should_escalate(notready_polls, now - last_recovery):
            continue
        if not can_escalate:
            print("keepalive: would recover, but the zoom target is unusable")
            continue
        last_recovery = now
        try:
            with lens_lock():
                print("keepalive: no one else is recovering — escalating")
                if recover(endpoint):
                    notready_polls = 0
        except LensBusy as holder:
            print(f"keepalive: recovery already running ({holder})")


def cmd_apis(endpoint):
    """List all available API methods."""
    result = api_call(endpoint, "getAvailableApiList")
    apis = result[0] if result else []
    for name in sorted(apis):
        print(name)


def dispatch(argv, endpoint):
    """Run one command. argv is sys.argv[1:], and the lens lock is already
    held if actuates_lens(argv) said this command needs it."""
    cmd = argv[0]

    if cmd == "discover":
        cmd_discover(endpoint)
    elif cmd == "zoom":
        if len(argv) < 2:
            pos = get_zoom_position(endpoint)
            print(f"{pos}/100")
            return
        direction = argv[1]
        movement = argv[2] if len(argv) > 2 else "1shot"
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


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    _init_logging()
    argv = sys.argv[1:]
    endpoint = DEFAULT_ENDPOINT

    if not actuates_lens(argv):
        dispatch(argv, endpoint)
        return

    # Every lens-moving command can end up parking the lens at the configured
    # target, so reject a malformed one here rather than part-way through a
    # recovery with the lock held.
    try:
        zoom_target()
    except ValueError as error:
        print(f"Bad configuration: {error}")
        sys.exit(1)

    try:
        with lens_lock():
            dispatch(argv, endpoint)
    except LensBusy as holder:
        # Not a failure of the camera — someone else is already on it — but
        # the requested move did not happen, so don't report success.
        print(f"Lens busy: {holder} is already moving it. "
              f"Skipping '{' '.join(argv)}'.")
        sys.exit(1)


if __name__ == "__main__":
    main()
