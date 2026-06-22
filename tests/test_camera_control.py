"""Tests for camera-control.py's device-descriptor parsing.

discover() does network I/O, but the part that extracts the camera's API
endpoint from the UPnP device descriptor XML is pure and worth pinning down —
it's how the script finds the camera when SSDP succeeds.
"""

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
