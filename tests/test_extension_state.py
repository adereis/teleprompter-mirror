"""Tests for system/extension-state.py — the installer's gsettings edits.

The subtlety being pinned down: GNOME's `disabled-extensions` takes precedence
over `enabled-extensions` (its own schema says so), so enabling an extension
the user once switched off means clearing the disabled entry as well. Adding
to the enabled list alone makes the installer report success and change
nothing.
"""

import unittest

from loader import load_module

state = load_module("system/extension-state.py", "extension_state")

UUID = "teleprompter-focus@teleprompter-mirror.local"
OTHER = "something-else@example.invalid"


class PlanTest(unittest.TestCase):
    def test_enabling_adds_to_the_enabled_list(self):
        self.assertEqual(state.plan(UUID, True, [OTHER], []),
                         {"enabled-extensions": [OTHER, UUID]})

    def test_enabling_clears_a_previous_disable(self):
        # The regression: without this the next login still skips it.
        self.assertEqual(
            state.plan(UUID, True, [], [UUID, OTHER]),
            {"enabled-extensions": [UUID], "disabled-extensions": [OTHER]})

    def test_enabling_an_already_enabled_extension_changes_nothing(self):
        self.assertEqual(state.plan(UUID, True, [UUID], [OTHER]), {})

    def test_enabling_still_clears_a_stale_disable(self):
        self.assertEqual(state.plan(UUID, True, [UUID], [UUID]),
                         {"disabled-extensions": []})

    def test_disabling_removes_it_from_both_lists(self):
        self.assertEqual(
            state.plan(UUID, False, [UUID, OTHER], [UUID]),
            {"enabled-extensions": [OTHER], "disabled-extensions": []})

    def test_disabling_an_absent_extension_changes_nothing(self):
        self.assertEqual(state.plan(UUID, False, [OTHER], [OTHER]), {})

    def test_other_extensions_are_never_disturbed(self):
        edits = state.plan(UUID, True, [OTHER], [OTHER])
        self.assertEqual(edits, {"enabled-extensions": [OTHER, UUID]})


class GsettingsFormatTest(unittest.TestCase):
    def read(self, output):
        return state.read_list("enabled-extensions", run=lambda *a, **k: output)

    def test_reads_an_empty_array(self):
        self.assertEqual(self.read("@as []\n"), [])
        self.assertEqual(self.read("[]\n"), [])
        self.assertEqual(self.read("\n"), [])

    def test_reads_a_populated_array(self):
        self.assertEqual(self.read(f"['{UUID}', '{OTHER}']\n"), [UUID, OTHER])

    def test_formats_an_array_gsettings_can_parse(self):
        self.assertEqual(state.format_list([]), "[]")
        self.assertEqual(state.format_list([UUID]), f"['{UUID}']")
        self.assertEqual(state.format_list([UUID, OTHER]), f"['{UUID}', '{OTHER}']")

    def test_quotes_in_a_uuid_are_escaped(self):
        self.assertEqual(state.format_list(["it's"]), r"['it\'s']")

    def test_round_trip(self):
        items = [UUID, OTHER]
        self.assertEqual(self.read(state.format_list(items)), items)


if __name__ == "__main__":
    unittest.main()
