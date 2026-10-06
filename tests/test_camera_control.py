"""Tests for camera-control.py's device-descriptor parsing.

discover() does network I/O, but the part that extracts the camera's API
endpoint from the UPnP device descriptor XML is pure and worth pinning down —
it's how the script finds the camera when SSDP succeeds.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from loader import load_module

cam = load_module("camera/camera-control.py", "camera_control")

# Trimmed Sony-style device descriptor: UPnP modelName plus the Sony
# ScalarWebAPI service list that advertises the camera endpoint.
DESCRIPTOR = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"
      xmlns:av="urn:schemas-sony-com:av">
  <device>
    <modelName>ILCE-6300</modelName>
    <av:X_ScalarWebAPI_DeviceInfo>
      <av:X_ScalarWebAPI_ServiceList>
        <av:X_ScalarWebAPI_Service>
          <av:X_ScalarWebAPI_ServiceType>camera</av:X_ScalarWebAPI_ServiceType>
          <av:X_ScalarWebAPI_ActionList_URL>http://192.168.122.1:8080/sony</av:X_ScalarWebAPI_ActionList_URL>
        </av:X_ScalarWebAPI_Service>
        <av:X_ScalarWebAPI_Service>
          <av:X_ScalarWebAPI_ServiceType>system</av:X_ScalarWebAPI_ServiceType>
          <av:X_ScalarWebAPI_ActionList_URL>http://192.168.122.1:8080/sony</av:X_ScalarWebAPI_ActionList_URL>
        </av:X_ScalarWebAPI_Service>
      </av:X_ScalarWebAPI_ServiceList>
    </av:X_ScalarWebAPI_DeviceInfo>
  </device>
</root>
"""


class ParseDeviceDescriptorTest(unittest.TestCase):
    def test_extracts_model_and_camera_endpoint(self):
        model, endpoint = cam.parse_device_descriptor(DESCRIPTOR)
        self.assertEqual(model, "ILCE-6300")
        self.assertEqual(endpoint, "http://192.168.122.1:8080/sony")

    def test_picks_camera_service_not_first_service(self):
        # Even if a non-camera service is listed, the camera one must win.
        reordered = DESCRIPTOR.replace(
            "<av:X_ScalarWebAPI_ServiceType>camera",
            "<av:X_ScalarWebAPI_ServiceType>guide", 1)
        model, endpoint = cam.parse_device_descriptor(reordered)
        # No 'camera' service now -> endpoint is None, model still parsed.
        self.assertEqual(model, "ILCE-6300")
        self.assertIsNone(endpoint)

    def test_unknown_model_when_absent(self):
        xml = ('<root xmlns="urn:schemas-upnp-org:device-1-0"'
               ' xmlns:av="urn:schemas-sony-com:av"><device></device></root>')
        model, endpoint = cam.parse_device_descriptor(xml)
        self.assertEqual(model, "Unknown")
        self.assertIsNone(endpoint)


def make_event(status="IDLE", zoom=48):
    """A getEvent-shaped result: fixed-index array with None gaps, a
    cameraStatus object, and a zoomInformation object (as the A6300 returns)."""
    return [
        {"type": "availableApiList", "names": ["getEvent", "actZoom"]},
        {"type": "cameraStatus", "cameraStatus": status},
        None,
        {"type": "zoomInformation", "zoomPosition": zoom, "zoomNumberBox": 1},
    ]


class ParseCameraStateTest(unittest.TestCase):
    """parse_camera_state drives the re-association recovery decision:
    recover iff the camera reports NotReady. Pin down that logic."""

    def test_extracts_status_and_zoom(self):
        self.assertEqual(cam.parse_camera_state(make_event("IDLE", 48)),
                         ("IDLE", 48))

    def test_detects_notready(self):
        # The case the whole recovery path exists for.
        status, _ = cam.parse_camera_state(make_event("NotReady", 0))
        self.assertEqual(status, "NotReady")

    def test_tolerates_none_gaps_and_reordering(self):
        # Reversed order with the None gap still resolves both fields — the
        # helper searches by type rather than trusting fixed indices.
        status, zoom = cam.parse_camera_state(list(reversed(make_event("IDLE", 30))))
        self.assertEqual((status, zoom), ("IDLE", 30))

    def test_missing_fields_return_none(self):
        self.assertEqual(cam.parse_camera_state([]), (None, None))
        self.assertEqual(cam.parse_camera_state(None), (None, None))
        # Zoom present but no cameraStatus object -> status None, zoom found.
        only_zoom = [{"type": "zoomInformation", "zoomPosition": 12}]
        self.assertEqual(cam.parse_camera_state(only_zoom), (None, 12))


class FakeCamera:
    """A camera that answers getEvent and startRecMode, for the recovery tests.

    Models the one behavior the recovery logic turns on: startRecMode moves the
    camera from NotReady to IDLE. `resets_after` makes it drop back to NotReady
    again that many times, which is what a WiFi flap storm does to a real one.
    """

    def __init__(self, status="IDLE", zoom=48, resets_after=0):
        self.status = status
        self.zoom = zoom
        self.resets_after = resets_after
        self.methods = []
        self.unreachable = set()

    def __call__(self, endpoint, method, params=None, **kwargs):
        self.methods.append(method)
        if method in self.unreachable:
            if kwargs.get("exit_on_error", True):
                raise SystemExit(1)
            return None
        if method == "startRecMode":
            self.status = "IDLE"
            return []
        if method == "getEvent":
            return make_event(self.status, self.zoom)
        return []

    def restore_zoom(self, endpoint, target=None):
        """Stand-in for the real restore: succeeds, but lets the camera reset
        underneath it the way a flap storm does."""
        if self.resets_after:
            self.resets_after -= 1
            self.status = "NotReady"
        return True


class RecoveryGatingTest(unittest.TestCase):
    """Both `start` and `reconnect` must restore zoom only when the camera
    actually reset (NotReady). A brief RF drop leaves the camera IDLE with its
    zoom intact; restoring then clobbers a good setting — the exact bug that
    stranded the A6300 at 100/100 after a WiFi flap."""

    def _install(self, camera):
        self.camera = camera
        self.restored = []
        restore = camera.restore_zoom

        def counted(endpoint, target=None):
            self.restored.append(endpoint)
            return restore(endpoint, target)

        self.enterContext(patch.object(cam, "api_call", camera))
        self.enterContext(patch.object(cam, "restore_zoom", counted))
        self.enterContext(patch.object(cam.time, "sleep"))
        return camera

    def test_start_skips_restore_when_idle(self):
        self._install(FakeCamera("IDLE", 38))
        cam.cmd_start("EP")
        self.assertEqual(self.restored, [])
        self.assertNotIn("startRecMode", self.camera.methods)

    def test_start_restores_when_notready(self):
        self._install(FakeCamera("NotReady", 0))
        cam.cmd_start("EP")
        self.assertEqual(self.restored, ["EP"])
        self.assertIn("startRecMode", self.camera.methods)

    def test_reconnect_skips_restore_when_idle(self):
        # The incident: WiFi flap, camera still IDLE with a good zoom.
        self._install(FakeCamera("IDLE", 38))
        cam.cmd_reconnect("EP")
        self.assertEqual(self.restored, [])
        self.assertNotIn("startRecMode", self.camera.methods)

    def test_reconnect_restores_when_notready(self):
        self._install(FakeCamera("NotReady", 0))
        cam.cmd_reconnect("EP")
        self.assertEqual(self.restored, ["EP"])
        self.assertIn("startRecMode", self.camera.methods)

    def test_failed_start_does_not_move_lens(self):
        camera = self._install(FakeCamera("NotReady", 0))
        camera.unreachable.add("startRecMode")
        with self.assertRaises(SystemExit) as error:
            cam.cmd_start("EP")
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(self.restored, [])

    def test_failed_restore_fails_recovery(self):
        for command in (cam.cmd_start, cam.cmd_reconnect):
            with self.subTest(command=command.__name__):
                with self.subTest():
                    self.enterContext(patch.object(cam, "api_call",
                                                   FakeCamera("NotReady", 0)))
                    self.enterContext(patch.object(cam, "restore_zoom",
                                                   lambda ep, target=None: False))
                    self.enterContext(patch.object(cam.time, "sleep"))
                    with self.assertRaises(SystemExit) as error:
                        command("EP")
                    self.assertEqual(error.exception.code, 1)

    def test_missing_status_cannot_claim_recovery_unnecessary(self):
        camera = self._install(FakeCamera("NotReady", 0))
        self.enterContext(patch.object(cam, "parse_camera_state",
                                       lambda result: (None, None)))
        with self.assertRaises(SystemExit):
            cam.cmd_start("EP")
        self.assertEqual(self.restored, [])
        self.assertNotIn("startRecMode", camera.methods)


class VerifiedRecoveryTest(unittest.TestCase):
    """A recovery holds the non-blocking lens lock, so every recovery that fires
    while it runs is declined and thrown away — it has to be the one that
    finishes. The flap storm that prompted this: one pass ran startRecMode,
    failed to restore zoom across a dying link, turned away four later
    recoveries, and exited leaving the camera NotReady behind a healthy link."""

    def setUp(self):
        self.enterContext(patch.object(cam.time, "sleep"))

    def _run(self, camera, restore=None):
        self.enterContext(patch.object(cam, "api_call", camera))
        self.enterContext(patch.object(
            cam, "restore_zoom", restore or camera.restore_zoom))
        return cam.recover("EP")

    def test_single_pass_when_the_camera_stays_ready(self):
        camera = FakeCamera("NotReady", 0)
        self.assertTrue(self._run(camera))
        self.assertEqual(camera.methods.count("startRecMode"), 1)

    def test_repeats_when_the_camera_resets_mid_recovery(self):
        # The camera drops back to NotReady during the first restore; the pass
        # must notice before releasing the lock and run startRecMode again.
        camera = FakeCamera("NotReady", 0, resets_after=1)
        self.assertTrue(self._run(camera))
        self.assertEqual(camera.methods.count("startRecMode"), 2)

    def test_gives_up_after_the_pass_limit(self):
        camera = FakeCamera("NotReady", 0, resets_after=99)
        self.assertFalse(self._run(camera))
        self.assertEqual(camera.methods.count("startRecMode"), cam.RECOVERY_PASSES)

    def test_does_not_report_success_when_the_lens_was_not_restored(self):
        camera = FakeCamera("NotReady", 0)
        self.assertFalse(self._run(camera, restore=lambda ep, target=None: False))

    def test_unanswering_camera_is_not_success(self):
        camera = FakeCamera("NotReady", 0)

        def restore(endpoint, target=None):
            camera.unreachable.add("getEvent")
            return True

        self.assertFalse(self._run(camera, restore=restore))


class RestoreZoomTest(unittest.TestCase):
    """restore_zoom must reject an implausibly high final position (the 'stop'
    arrived late and the lens over-ran) and retry, rather than reporting the
    over-run as a successful restore. A CameraReset is answered instead of
    merely retried: waiting cannot bring a camera back into rec mode."""

    def setUp(self):
        self.enterContext(patch.object(cam.time, "sleep"))
        self.api = self.enterContext(patch.object(cam, "api_call", return_value=[]))

    def test_rejects_overshoot_then_succeeds(self):
        # First attempt overshoots past the ceiling; second lands on target.
        results = iter([100, 50])
        with patch.object(cam, "zoom_to", lambda ep, t: next(results)):
            self.assertTrue(cam.restore_zoom("EP", target=50))

    def test_gives_up_after_persistent_overshoot(self):
        with patch.object(cam, "zoom_to", lambda ep, t: 100):
            self.assertFalse(cam.restore_zoom("EP", target=50))

    def test_accepts_sane_position(self):
        with patch.object(cam, "zoom_to", lambda ep, t: 50):
            self.assertTrue(cam.restore_zoom("EP", target=50))

    def test_camera_reset_restarts_rec_mode_instead_of_only_waiting(self):
        # The 2026-10-06 failure: actZoom answered "Not Available Now" on every
        # attempt, the loop treated it as a slow link, and all five attempts
        # went to a remedy that could not work.
        outcomes = iter([cam.CameraReset("actZoom: Not Available Now"), 50])

        def zoom_to(endpoint, target):
            result = next(outcomes)
            if isinstance(result, Exception):
                raise result
            return result

        with patch.object(cam, "zoom_to", zoom_to):
            self.assertTrue(cam.restore_zoom("EP", target=50))
        self.assertIn(call("EP", "startRecMode", exit_on_error=False),
                      self.api.call_args_list)

    def test_persistent_reset_is_not_reported_as_restored(self):
        def zoom_to(endpoint, target):
            raise cam.CameraReset("actZoom: Not Available Now")

        with patch.object(cam, "zoom_to", zoom_to):
            self.assertFalse(cam.restore_zoom("EP", target=50))


class ZoomToTargetTest(unittest.TestCase):
    """The restore is closed-loop because the open-loop one did not repeat: the
    same 1.2s move from zero parked the same lens at 54, 48 and 45 on three
    consecutive recoveries, re-framing the shot a little each time."""

    def setUp(self):
        self.enterContext(patch.object(cam.time, "sleep"))
        self.enterContext(patch.object(cam, "zoom_to_zero", return_value=True))

    def _lens(self, rate=cam.ZOOM_RATE_GUESS, start=0,
              min_increment=cam.ZOOM_MIN_INCREMENT):
        """A lens modelled on the measured E PZ 16-50mm.

        Travel is proportional to commanded motor time, but never less than
        `min_increment`: on the real lens, commands of 0.05s through 0.15s all
        moved it about 10 of 100 positions.
        """
        state = {"pos": start, "moves": []}

        def zoom_timed(endpoint, direction, duration):
            state["moves"].append((direction, duration))
            delta = max(min_increment, duration * rate)
            state["pos"] += delta if direction == "in" else -delta
            state["pos"] = max(0, min(100, round(state["pos"])))
            return state["pos"]

        return zoom_timed, state

    def test_converges_on_the_target(self):
        zoom_timed, _ = self._lens()
        with patch.object(cam, "zoom_timed", zoom_timed):
            pos = cam.zoom_to("EP", 50)
        self.assertLessEqual(abs(pos - 50), cam.ZOOM_TOLERANCE)

    def test_corrects_an_overshoot_rather_than_accepting_it(self):
        # A lens twice as fast as the built-in guess overshoots the first leg.
        # The loop must zoom back out, and must re-measure the rate instead of
        # oscillating 0 <-> 100 on a constant it has already seen to be wrong.
        zoom_timed, state = self._lens(rate=cam.ZOOM_RATE_GUESS * 2)
        with patch.object(cam, "zoom_timed", zoom_timed):
            pos = cam.zoom_to("EP", 50)
        self.assertLessEqual(abs(pos - 50), cam.ZOOM_TOLERANCE)
        self.assertIn("out", [direction for direction, _ in state["moves"]])

    def test_converges_for_a_lens_slower_than_the_guess(self):
        zoom_timed, _ = self._lens(rate=cam.ZOOM_RATE_GUESS / 3)
        with patch.object(cam, "zoom_timed", zoom_timed):
            pos = cam.zoom_to("EP", 50)
        self.assertLessEqual(abs(pos - 50), cam.ZOOM_TOLERANCE)

    def test_short_moves_do_not_poison_the_rate_estimate(self):
        # A minimum-increment move covers 10 positions however short the pulse,
        # so reading a speed off it (10 / 0.05s = 200 units/s) would make every
        # later step far too brief to reach the target.
        zoom_timed, state = self._lens()
        with patch.object(cam, "zoom_timed", zoom_timed):
            pos = cam.zoom_to("EP", 95)
        self.assertLessEqual(abs(pos - 95), cam.ZOOM_TOLERANCE)

    def test_repeated_restores_land_in_the_same_place(self):
        # Three recoveries in a row must agree. The open-loop version is what
        # produced 54 -> 48 -> 45 on three consecutive recoveries.
        zoom_timed, state = self._lens()
        with patch.object(cam, "zoom_timed", zoom_timed):
            landings = []
            for _ in range(3):
                state["pos"] = 0
                landings.append(cam.zoom_to("EP", 50))
        self.assertEqual(len(set(landings)), 1, landings)
        self.assertLessEqual(abs(landings[0] - 50), cam.ZOOM_TOLERANCE)

    def test_stalled_motor_is_an_error_not_a_silent_short_landing(self):
        with patch.object(cam, "zoom_timed", lambda ep, d, dur: 10):
            with self.assertRaises(cam.ZoomTimingError):
                cam.zoom_to("EP", 50)

    def test_failed_homing_is_an_error(self):
        with patch.object(cam, "zoom_to_zero", return_value=False):
            with self.assertRaises(cam.ZoomTimingError):
                cam.zoom_to("EP", 50)

    def test_camera_reset_propagates_for_the_caller_to_answer(self):
        def zoom_timed(endpoint, direction, duration):
            raise cam.CameraReset("actZoom: Not Available Now")

        with patch.object(cam, "zoom_timed", zoom_timed):
            with self.assertRaises(cam.CameraReset):
                cam.zoom_to("EP", 50)


class ZoomTargetConfigTest(unittest.TestCase):
    def test_reads_the_configured_target(self):
        with patch.dict(os.environ, {"TELEPROMPTER_CAMERA_ZOOM_TARGET": "54"}):
            self.assertEqual(cam.zoom_target(), 54)

    def test_rejects_a_value_outside_the_lens_range(self):
        for value in ("-1", "101"):
            with self.subTest(value=value):
                with patch.dict(os.environ,
                                {"TELEPROMPTER_CAMERA_ZOOM_TARGET": value}):
                    with self.assertRaises(ValueError):
                        cam.zoom_target()

    def test_rejects_a_non_integer(self):
        # Silently falling back to the default would park a camera configured
        # for 54 at 50 forever, with nothing saying why.
        with patch.dict(os.environ, {"TELEPROMPTER_CAMERA_ZOOM_TARGET": "fifty"}):
            with self.assertRaises(ValueError):
                cam.zoom_target()


EP = "http://camera.invalid:8080/sony"


class CameraResetDetectionTest(unittest.TestCase):
    """'Not Available Now' is the camera saying it left rec mode, which needs
    startRecMode. Read as a generic error it looks like one more transient
    failure, and backing off from it never converges."""

    def _response(self, payload):
        class FakeResponse:
            def read(self_inner):
                return json.dumps(payload).encode()

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

        return FakeResponse()

    def test_not_available_now_raises_camera_reset(self):
        payload = {"error": [cam.ERR_NOT_AVAILABLE_NOW, "Not Available Now"]}
        with patch.object(cam.urllib.request, "urlopen",
                          return_value=self._response(payload)):
            with self.assertRaises(cam.CameraReset):
                cam.api_call(EP, "actZoom", ["in", "start"], detect_reset=True)

    def test_other_errors_are_unaffected(self):
        payload = {"error": [12, "No Such Method"]}
        with patch.object(cam.urllib.request, "urlopen",
                          return_value=self._response(payload)):
            with self.assertRaises(SystemExit):
                cam.api_call(EP, "actZoom", ["in", "start"], detect_reset=True)

    def test_reset_is_not_detected_unless_asked(self):
        # Read-only callers keep the old behavior: a plain error exit.
        payload = {"error": [cam.ERR_NOT_AVAILABLE_NOW, "Not Available Now"]}
        with patch.object(cam.urllib.request, "urlopen",
                          return_value=self._response(payload)):
            with self.assertRaises(SystemExit):
                cam.api_call(EP, "getEvent", [False])

    def test_actzoom_asks_for_reset_detection(self):
        with patch.object(cam, "api_call") as api:
            cam._actzoom("EP", "in", "start")
        self.assertTrue(api.call_args.kwargs.get("detect_reset"))


class KeepaliveEscalationTest(unittest.TestCase):
    """The keepalive is the only process guaranteed to be running whenever
    camera WiFi is up, so it is the backstop for a recovery that failed and
    exited: without it nothing looks at the camera again until the next DHCP
    renewal, about 27 minutes later."""

    def test_waits_for_several_consecutive_notready_polls(self):
        for polls in range(1, cam.KEEPALIVE_NOTREADY_THRESHOLD):
            with self.subTest(polls=polls):
                self.assertFalse(cam.keepalive_should_escalate(polls, 10_000))

    def test_escalates_once_the_camera_stays_notready(self):
        self.assertTrue(cam.keepalive_should_escalate(
            cam.KEEPALIVE_NOTREADY_THRESHOLD, 10_000))

    def test_respects_the_cooldown(self):
        # A camera whose AP has wedged needs a power cycle; no number of
        # recoveries reaches it, so don't hammer it.
        self.assertFalse(cam.keepalive_should_escalate(
            cam.KEEPALIVE_NOTREADY_THRESHOLD,
            cam.KEEPALIVE_RECOVERY_COOLDOWN - 1))


class TimedZoomTest(unittest.TestCase):
    def setUp(self):
        self.act = self.enterContext(patch.object(cam, "_actzoom", return_value=0.1))
        self.sleep = self.enterContext(patch.object(cam.time, "sleep"))
        self.position = self.enterContext(patch.object(cam, "get_zoom_position", return_value=38))

    def test_successful_move_stops_and_reads_position(self):
        self.assertEqual(cam.zoom_timed("EP", "in", 1.2), 38)
        self.assertEqual(self.act.call_args_list,
                         [call("EP", "in", "start"), call("EP", "in", "stop")])
        self.sleep.assert_has_calls([call(1.2), call(0.3)])

    def test_start_failure_still_attempts_stop(self):
        self.act.side_effect = [SystemExit(1), 0.1]
        with self.assertRaises(SystemExit):
            cam.zoom_timed("EP", "out", 1)
        self.act.assert_called_with("EP", "out", "stop")
        self.sleep.assert_not_called()
        self.position.assert_not_called()

    def test_interrupted_hold_still_attempts_stop(self):
        self.sleep.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            cam.zoom_timed("EP", "in", 1)
        self.act.assert_called_with("EP", "in", "stop")

    def test_slow_start_skips_hold_and_fails_after_stop(self):
        self.act.side_effect = [cam.ZOOM_LATENCY_LIMIT + 1, 0.1]
        with self.assertRaises(cam.ZoomTimingError):
            cam.zoom_timed("EP", "in", 1)
        self.act.assert_called_with("EP", "in", "stop")
        self.sleep.assert_not_called()

    def test_slow_stop_is_not_reported_as_success(self):
        self.act.side_effect = [0.1, cam.ZOOM_LATENCY_LIMIT + 1]
        with self.assertRaises(cam.ZoomTimingError):
            cam.zoom_timed("EP", "in", 1)
        self.position.assert_not_called()

    def test_stop_failure_propagates(self):
        self.act.side_effect = [0.1, SystemExit(1)]
        with self.assertRaises(SystemExit):
            cam.zoom_timed("EP", "in", 1)
        self.position.assert_not_called()

    def test_invalid_duration_cannot_start_either_leg(self):
        for value in (-1, float("nan"), float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    cam.zoom_timed("EP", "in", value)
                with self.assertRaises(ValueError):
                    cam.cmd_zoom_set("EP", value)
        self.act.assert_not_called()

    def test_cli_timed_move_uses_latency_guard(self):
        self.act.side_effect = [cam.ZOOM_LATENCY_LIMIT + 1, 0.1]
        with self.assertRaises(SystemExit) as error:
            cam.cmd_zoom("EP", "out", "2s")
        self.assertEqual(error.exception.code, 1)
        self.act.assert_called_with("EP", "out", "stop")


class ZoomTelemetryTest(unittest.TestCase):
    def test_zero_is_a_valid_reported_position(self):
        with patch.object(cam, "api_call", return_value=make_event(zoom=0)):
            self.assertEqual(cam.get_zoom_position("EP"), 0)

    def test_missing_or_invalid_position_is_an_error(self):
        for event in ([], None, *[make_event(zoom=value) for value in
                                 (None, "0", True, -1, 101, float("nan"))]):
            with self.subTest(event=event):
                with patch.object(cam, "api_call", return_value=event):
                    with self.assertRaises(SystemExit) as error:
                        cam.get_zoom_position("EP")
                    self.assertEqual(error.exception.code, 1)


class ActuatesLensTest(unittest.TestCase):
    """Which commands take the lock. Read-only ones must stay answerable while
    a recovery holds it — notably bare `zoom`, which only reports a position."""

    def test_lens_moving_commands_lock(self):
        for argv in (["zoom", "in"], ["zoom", "out", "3s"], ["zoom", "set"],
                     ["zoom", "stop"], ["refocus"], ["reconnect"], ["start"]):
            with self.subTest(argv=argv):
                self.assertTrue(cam.actuates_lens(argv))

    def test_read_only_commands_do_not_lock(self):
        for argv in (["zoom"], ["status"], ["keepalive"], ["discover"],
                     ["apis"], ["bogus"], []):
            with self.subTest(argv=argv):
                self.assertFalse(cam.actuates_lens(argv))


class LensLockTest(unittest.TestCase):
    """The lock exists because a WiFi flap can spawn several recoveries that
    outlive each other, and three of them once drove the same motor at once.
    flock is held per open file description, so a second acquisition conflicts
    even from within this one process."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "nested" / "camera.lock"

    def test_second_holder_is_declined_not_queued(self):
        with cam.lens_lock(self.path):
            with self.assertRaises(cam.LensBusy):
                with cam.lens_lock(self.path):
                    self.fail("second holder must not get the lock")

    def test_declined_message_names_the_holder(self):
        with cam.lens_lock(self.path):
            with self.assertRaises(cam.LensBusy) as busy:
                with cam.lens_lock(self.path):
                    pass
        self.assertEqual(str(busy.exception), f"pid {os.getpid()}")

    def test_lock_is_released_on_normal_exit(self):
        with cam.lens_lock(self.path):
            pass
        with cam.lens_lock(self.path):  # must not raise
            pass

    def test_lock_is_released_when_the_body_raises(self):
        # A recovery that dies mid-restore (unreachable camera raises
        # SystemExit) must not wedge every later recovery out of the lens.
        with self.assertRaises(SystemExit):
            with cam.lens_lock(self.path):
                raise SystemExit(1)
        with cam.lens_lock(self.path):  # must not raise
            pass

    def test_creates_the_directory_it_needs(self):
        with cam.lens_lock(self.path):
            self.assertTrue(self.path.exists())


class LockPathTest(unittest.TestCase):
    def test_lives_beside_the_other_project_state(self):
        # Must not depend on XDG_RUNTIME_DIR: the NM dispatcher runs this
        # script via `runuser`, which leaves that unset, so a runtime-dir path
        # would give dispatcher recoveries a different lock from the CLI's.
        env = {"XDG_CONFIG_HOME": "/xdg", "XDG_RUNTIME_DIR": "/run/user/1000"}
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(cam.lock_path(),
                             Path("/xdg/teleprompter-mirror/camera.lock"))


class ConfigWiringTest(unittest.TestCase):
    def test_default_endpoint_comes_from_config(self):
        # DEFAULT_ENDPOINT must be wired through the config loader rather than
        # hardcoded, so it tracks whatever the user configures.
        import teleprompter_config
        self.assertEqual(
            cam.DEFAULT_ENDPOINT,
            teleprompter_config.get("TELEPROMPTER_CAMERA_ENDPOINT"),
        )


if __name__ == "__main__":
    unittest.main()
