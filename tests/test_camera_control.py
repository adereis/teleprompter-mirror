"""Tests for camera-control.py's device-descriptor parsing.

discover() does network I/O, but the part that extracts the camera's API
endpoint from the UPnP device descriptor XML is pure and worth pinning down —
it's how the script finds the camera when SSDP succeeds.
"""

import types
import unittest

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


class RecoveryGatingTest(unittest.TestCase):
    """Both `start` and `reconnect` must restore zoom only when the camera
    actually reset (NotReady). A brief RF drop leaves the camera IDLE with its
    zoom intact; restoring then clobbers a good setting — the exact bug that
    stranded the A6300 at 100/100 after a WiFi flap."""

    def setUp(self):
        self._orig_api = cam.api_call
        self._orig_restore = cam.restore_zoom
        self.restored = []
        cam.restore_zoom = lambda ep: self.restored.append(ep) or True

    def tearDown(self):
        cam.api_call = self._orig_api
        cam.restore_zoom = self._orig_restore

    def _install_api(self, event):
        """Fake api_call: getEvent returns `event`, everything else returns []."""
        self.methods = []

        def fake(endpoint, method, params=None, **kwargs):
            self.methods.append(method)
            return event if method == "getEvent" else []

        cam.api_call = fake

    def test_start_skips_restore_when_idle(self):
        self._install_api(make_event("IDLE", 38))
        cam.cmd_start("EP")
        self.assertEqual(self.restored, [])
        self.assertNotIn("startRecMode", self.methods)

    def test_start_restores_when_notready(self):
        self._install_api(make_event("NotReady", 0))
        cam.cmd_start("EP")
        self.assertEqual(self.restored, ["EP"])
        self.assertIn("startRecMode", self.methods)

    def test_reconnect_skips_restore_when_idle(self):
        # The incident: WiFi flap, camera still IDLE with a good zoom.
        self._install_api(make_event("IDLE", 38))
        cam.cmd_reconnect("EP")
        self.assertEqual(self.restored, [])
        self.assertNotIn("startRecMode", self.methods)

    def test_reconnect_restores_when_notready(self):
        self._install_api(make_event("NotReady", 0))
        cam.cmd_reconnect("EP")
        self.assertEqual(self.restored, ["EP"])
        self.assertIn("startRecMode", self.methods)


class RestoreZoomTest(unittest.TestCase):
    """restore_zoom must reject an implausibly high final position (the 'stop'
    arrived late and the lens over-ran) and retry, rather than reporting the
    over-run as a successful restore."""

    def setUp(self):
        self._orig = (cam.zoom_to_zero, cam.zoom_timed, cam.time)
        # Stub timing so the backoff loop doesn't actually sleep.
        cam.time = types.SimpleNamespace(sleep=lambda *_: None, monotonic=lambda: 0.0)
        cam.zoom_to_zero = lambda ep: True

    def tearDown(self):
        cam.zoom_to_zero, cam.zoom_timed, cam.time = self._orig

    def test_rejects_overshoot_then_succeeds(self):
        # First attempt overshoots past the ceiling; second lands at a sane pos.
        results = iter([100, 38])
        cam.zoom_timed = lambda ep, d, dur: next(results)
        self.assertTrue(cam.restore_zoom("EP"))

    def test_gives_up_after_persistent_overshoot(self):
        cam.zoom_timed = lambda ep, d, dur: 100  # always over-runs
        self.assertFalse(cam.restore_zoom("EP"))

    def test_accepts_sane_position(self):
        cam.zoom_timed = lambda ep, d, dur: 38
        self.assertTrue(cam.restore_zoom("EP"))


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
