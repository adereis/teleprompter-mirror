"""Identify the tablet from isolated sysfs fixtures, never interface prefixes."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from loader import ROOT


class TabletInterfaceTest(unittest.TestCase):
    def find(self, interfaces):
        with tempfile.TemporaryDirectory() as directory:
            net = Path(directory)
            for name, driver in interfaces.items():
                vendor, product = "04e8", "6864"
                if isinstance(driver, tuple):
                    driver, vendor, product = driver
                device = net / name / "device"
                device.mkdir(parents=True)
                if driver:
                    (device / "driver").symlink_to(f"/drivers/{driver}")
                    (device.parent / "idVendor").write_text(vendor)
                    (device.parent / "idProduct").write_text(product)
            return subprocess.run(
                ["bash", "-euc", '. "$1"; find_tablet_interface "$2"',
                 "test-usb", str(ROOT / "lib/usb.sh"), str(net)],
                text=True, capture_output=True, check=False,
            )

    def test_dock_ethernet_is_not_selected(self):
        result = self.find({"enp1s0": "r8152", "enx1234": "rndis_host", "lo": None})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "enx1234")

    def test_driver_match_handles_any_interface_name(self):
        result = self.find({"tablet0": "cdc_ether"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "tablet0")

    def test_generic_cdc_ether_device_is_not_the_tablet(self):
        result = self.find({"enx0001": ("cdc_ether", "ffff", "0001")})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")

    def test_absent_tablet_can_be_polled_without_errexit(self):
        result = self.find({"enp1s0": "e1000e", "lo": None})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")

    def test_multiple_matches_fail_without_picking_one(self):
        result = self.find({"usb0": "rndis_host", "usb1": "cdc_ether"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("Multiple tethering interfaces", result.stderr)


class UsbLauncherTest(unittest.TestCase):
    def run_launcher(self, connection="Wired connection 1", firewall_status=0):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("bin", "lib", "tools"):
                (root / name).mkdir()
            shutil.copyfile(ROOT / "bin/start-mirror.sh", root / "bin/start-mirror.sh")
            shutil.copyfile(ROOT / "lib/config.sh", root / "lib/config.sh")
            # Device discovery has separate sysfs tests; fix its result here
            # to exercise profile checks without consulting real hardware.
            (root / "lib/usb.sh").write_text("find_tablet_interface() { echo tablet0; }\n")
            commands = {
                "adb": 'if [ "$1" = devices ]; then echo "fixture-device device"; fi\n',
                "sleep": "exit 0\n",
                "ip": "echo '    inet 192.0.2.2/24'\n",
                "sudo": 'exec "$@"\n',
                "firewall-cmd": f"exit {firewall_status}\n",
                "nmcli": '''case "$*" in
  '-g GENERAL.CON-UUID device show tablet0')
    echo '00000000-0000-4000-8000-000000000001' ;;
  '-e no -g connection.id connection show uuid '*)
    printf '%s\\n' "$TEST_CONNECTION_NAME" ;;
  'connection modify uuid '*) ;;
  *) echo "Unexpected nmcli call: $*" >&2; exit 1 ;;
esac
''',
            }
            for name, source in commands.items():
                tool = root / "tools" / name
                tool.write_text('#!/bin/bash\nprintf "%s %s\\n" "${0##*/}" "$*" >> "$TEST_CALLS"\n' + source)
                tool.chmod(0o755)
            calls = root / "calls.log"
            result = subprocess.run(
                ["bash", str(root / "bin/start-mirror.sh"), "reconnect"],
                env={**os.environ, "PATH": f"{root / 'tools'}:{os.environ['PATH']}",
                     "XDG_CONFIG_HOME": str(root), "TEST_CALLS": str(calls),
                     "TEST_CONNECTION_NAME": connection, "TELEPROMPTER_PORT": "9000"},
                capture_output=True, text=True, check=False,
            )
            return result, calls.read_text()

    def test_modifies_exact_uuid_and_uses_configured_port(self):
        result, calls = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("nmcli connection modify uuid 00000000-0000-4000-8000-000000000001", calls)
        self.assertIn("firewall-cmd --zone=trusted --change-interface=tablet0", calls)
        self.assertIn("adb reverse tcp:9000 tcp:9000", calls)

    def test_named_profile_prevents_routing_and_firewall_changes(self):
        result, calls = self.run_launcher(connection="Fixture Ethernet")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("nmcli connection modify", calls)
        self.assertNotIn("firewall-cmd", calls)
        self.assertNotIn("adb reverse", calls)

    def test_firewall_failure_prevents_success_message(self):
        result, calls = self.run_launcher(firewall_status=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("adb reverse", calls)
        self.assertNotIn("USB reconnected", result.stdout)
