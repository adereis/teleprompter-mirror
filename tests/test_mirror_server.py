"""Tests for mirror-server.py's _fix_mdns SDP candidate rewriting.

Chrome hides local IPs behind mDNS .local hostnames that a tablet on the LAN
can't resolve. _fix_mdns swaps them for real IPs (one copy per IP) so ICE can
find a working path. These tests pin that behavior down.
"""

import json
import unittest

from loader import load_module

server = load_module("app/mirror-server.py", "mirror_server")

MDNS_HOST = "abcd1234-12ab-34cd-56ef-1234567890ab.local"


def make_sdp(*lines):
    return json.dumps({"type": "offer", "sdp": "\r\n".join(lines)})


def sdp_lines(result_json):
    return json.loads(result_json)["sdp"].split("\r\n")


class FixMdnsTest(unittest.TestCase):
    def test_passthrough_when_no_mdns(self):
        original = make_sdp("v=0", "a=candidate:1 1 udp 2122260223 10.0.0.1 5000 typ host")
        result = server._fix_mdns(original, ["192.168.1.5"])
        self.assertEqual(json.loads(result)["sdp"], json.loads(original)["sdp"])

    def test_single_ip_replaces_hostname(self):
        original = make_sdp(
            f"a=candidate:1 1 udp 2122260223 {MDNS_HOST} 54321 typ host")
        lines = sdp_lines(server._fix_mdns(original, ["192.168.1.5"]))
        self.assertEqual(len(lines), 1)
        self.assertIn("192.168.1.5", lines[0])
        self.assertNotIn(".local", lines[0])

    def test_multiple_ips_duplicate_candidate(self):
        original = make_sdp(
            f"a=candidate:1 1 udp 2122260223 {MDNS_HOST} 54321 typ host")
        lines = sdp_lines(server._fix_mdns(original, ["192.168.1.5", "10.42.0.1"]))
        # One candidate line per IP so ICE can try every local path.
        self.assertEqual(len(lines), 2)
        self.assertIn("192.168.1.5", lines[0])
        self.assertIn("10.42.0.1", lines[1])

    def test_duplicate_candidates_have_distinct_foundations(self):
        # ICE treats same-foundation candidates as one; the foundation must
        # vary across the duplicates or the extra paths are ignored.
        original = make_sdp(
            f"a=candidate:1 1 udp 2122260223 {MDNS_HOST} 54321 typ host")
        lines = sdp_lines(server._fix_mdns(original, ["192.168.1.5", "10.42.0.1"]))
        foundation0 = lines[0].split(" ", 1)[0]
        foundation1 = lines[1].split(" ", 1)[0]
        self.assertNotEqual(foundation0, foundation1)

    def test_non_candidate_lines_preserved(self):
        original = make_sdp(
            "v=0",
            f"a=candidate:1 1 udp 2122260223 {MDNS_HOST} 54321 typ host",
            "a=end-of-candidates",
        )
        lines = sdp_lines(server._fix_mdns(original, ["192.168.1.5"]))
        self.assertIn("v=0", lines)
        self.assertIn("a=end-of-candidates", lines)

    def test_result_is_valid_json_preserving_other_keys(self):
        original = make_sdp("v=0")
        result = json.loads(server._fix_mdns(original, ["192.168.1.5"]))
        self.assertEqual(result["type"], "offer")


if __name__ == "__main__":
    unittest.main()
