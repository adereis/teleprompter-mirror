"""Tests for window_focus — picking and raising the shared window.

Only the pure parts are exercised here: the D-Bus calls into the GNOME Shell
extension are kept behind list_windows/activate_window, which the tests either
bypass (by passing windows in) or replace.
"""

import json
import tempfile
import threading
import unittest
from pathlib import Path

import loader  # noqa: F401  (importing puts lib/ on sys.path)
import window_focus


def window(wid, wm_class, title, mru=0, focused=False, width=1920, height=1080):
    return {"id": str(wid), "wm_class": wm_class, "title": title,
            "mru": mru, "focused": focused, "width": width, "height": height}


class SelectWindowTest(unittest.TestCase):
    def test_no_target_and_no_pattern_matches_nothing(self):
        windows = [window(1, "google-chrome", "Meet")]
        self.assertIsNone(window_focus.select_window(windows))

    def test_target_matches_on_application_class(self):
        windows = [window(1, "zoom", "Zoom Meeting"), window(2, "google-chrome", "Meet")]
        target = {"wm_class": "google-chrome", "title": "Meet"}
        self.assertEqual(window_focus.select_window(windows, target=target)["id"], "2")

    def test_target_matches_after_the_title_changed(self):
        # Call windows rename themselves constantly; the class is the stable part.
        windows = [window(1, "google-chrome", "Meet — abc-defg-hij")]
        target = {"wm_class": "google-chrome", "title": "Meet"}
        self.assertEqual(window_focus.select_window(windows, target=target)["id"], "1")

    def test_the_remembered_window_id_wins(self):
        # The whole point of picking one of several browser windows: without
        # the id, class-and-title scoring cannot tell them apart.
        windows = [window(1, "google-chrome", "Meet", mru=0),
                   window(2, "google-chrome", "Meet", mru=1)]
        target = {"id": "2", "wm_class": "google-chrome", "title": "Meet"}
        self.assertEqual(window_focus.select_window(windows, target=target)["id"], "2")

    def test_a_stale_id_falls_back_to_the_application(self):
        windows = [window(1, "google-chrome", "Renamed")]
        target = {"id": "999", "wm_class": "google-chrome", "title": "Meet"}
        self.assertEqual(window_focus.select_window(windows, target=target)["id"], "1")

    def test_an_id_from_another_application_is_ignored(self):
        # Ids are per-session, so one left over from a previous login could
        # name an unrelated window; the class has to agree as well.
        windows = [window(5, "firefox", "Docs"), window(6, "google-chrome", "Meet")]
        target = {"id": "5", "wm_class": "google-chrome", "title": "Meet"}
        self.assertEqual(window_focus.select_window(windows, target=target)["id"], "6")

    def test_exact_title_beats_a_class_only_match(self):
        windows = [window(1, "google-chrome", "Inbox", mru=0),
                   window(2, "google-chrome", "Meet", mru=1)]
        target = {"wm_class": "google-chrome", "title": "Meet"}
        self.assertEqual(window_focus.select_window(windows, target=target)["id"], "2")

    def test_matching_is_case_insensitive(self):
        windows = [window(1, "Google-Chrome", "MEET")]
        target = {"wm_class": "google-chrome", "title": "meet"}
        self.assertEqual(window_focus.select_window(windows, target=target)["id"], "1")

    def test_the_focused_window_loses_a_tie(self):
        # The cast page shares an application class with the window it mirrors,
        # so without this rule its button would re-focus the cast window.
        windows = [window(1, "google-chrome", "Teleprompter Mirror", mru=0, focused=True),
                   window(2, "google-chrome", "Some call", mru=1)]
        target = {"wm_class": "google-chrome", "title": "A renamed call"}
        self.assertEqual(window_focus.select_window(windows, target=target)["id"], "2")

    def test_ties_fall_back_to_most_recently_used(self):
        windows = [window(1, "google-chrome", "Older", mru=2),
                   window(2, "google-chrome", "Newer", mru=1)]
        target = {"wm_class": "google-chrome", "title": "Gone"}
        self.assertEqual(window_focus.select_window(windows, target=target)["id"], "2")

    def test_target_without_a_class_requires_the_exact_title(self):
        windows = [window(1, "", "Scratch"), window(2, "", "Other")]
        target = {"wm_class": "", "title": "Scratch"}
        self.assertEqual(window_focus.select_window(windows, target=target)["id"], "1")

    def test_unmatched_target_selects_nothing(self):
        windows = [window(1, "zoom", "Zoom Meeting")]
        target = {"wm_class": "google-chrome", "title": "Meet"}
        self.assertIsNone(window_focus.select_window(windows, target=target))

    def test_pattern_matches_title_or_class(self):
        windows = [window(1, "firefox", "Docs"), window(2, "zoom", "Zoom Meeting")]
        self.assertEqual(
            window_focus.select_window(windows, pattern="zoom")["id"], "2")
        self.assertEqual(
            window_focus.select_window(windows, pattern="docs")["id"], "1")

    def test_pattern_is_ignored_when_a_target_is_remembered(self):
        windows = [window(1, "firefox", "Docs"), window(2, "zoom", "Zoom Meeting")]
        target = {"wm_class": "firefox", "title": "Docs"}
        self.assertEqual(
            window_focus.select_window(windows, target=target, pattern="zoom")["id"], "1")

    def test_invalid_pattern_is_reported(self):
        with self.assertRaises(window_focus.FocusError):
            window_focus.select_window([window(1, "zoom", "Zoom")], pattern="(")


class FocusTest(unittest.TestCase):
    def setUp(self):
        self.activated = []
        self.original = window_focus.activate_window
        window_focus.activate_window = self.activate
        self.addCleanup(setattr, window_focus, "activate_window", self.original)

    def activate(self, window_id):
        self.activated.append(window_id)
        return True

    def test_raises_the_matching_window(self):
        windows = [window(1, "zoom", "Zoom Meeting")]
        target = {"wm_class": "zoom", "title": "Zoom Meeting"}
        result = window_focus.focus(target=target, windows=windows)
        self.assertEqual(result["id"], "1")
        self.assertEqual(self.activated, ["1"])

    def test_already_focused_window_is_left_alone(self):
        windows = [window(1, "zoom", "Zoom Meeting", focused=True)]
        target = {"wm_class": "zoom", "title": "Zoom Meeting"}
        window_focus.focus(target=target, windows=windows)
        self.assertEqual(self.activated, [])

    def test_no_match_is_an_error(self):
        with self.assertRaises(window_focus.FocusError):
            window_focus.focus(target={"wm_class": "zoom", "title": "Zoom"},
                               windows=[window(1, "firefox", "Docs")])

    def test_nothing_remembered_is_an_error(self):
        with self.assertRaises(window_focus.FocusError):
            window_focus.focus(windows=[window(1, "firefox", "Docs")])

    def test_a_refused_activation_is_an_error(self):
        # A failed focus must never report success.
        window_focus.activate_window = lambda window_id: False
        with self.assertRaises(window_focus.FocusError):
            window_focus.focus(target={"wm_class": "zoom", "title": "Zoom"},
                               windows=[window(1, "zoom", "Zoom")])


class RememberedTargetTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "nested" / "focus-target.json"
        self.original = window_focus.state_path
        window_focus.state_path = lambda: self.path
        self.addCleanup(setattr, window_focus, "state_path", self.original)

    def test_round_trip(self):
        window_focus.save_target({"wm_class": "zoom", "title": "Zoom Meeting"})
        self.assertEqual(window_focus.load_target(),
                         {"wm_class": "zoom", "title": "Zoom Meeting"})

    def test_missing_file_means_nothing_remembered(self):
        self.assertIsNone(window_focus.load_target())

    def test_unreadable_file_is_an_error(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("not json")
        with self.assertRaises(window_focus.FocusError):
            window_focus.load_target()

    def test_forget_reports_whether_anything_was_removed(self):
        self.assertFalse(window_focus.forget_target())
        window_focus.save_target({"wm_class": "zoom", "title": "Zoom"})
        self.assertTrue(window_focus.forget_target())
        self.assertIsNone(window_focus.load_target())

    def test_remember_stores_the_identifying_fields(self):
        windows = [window(7, "zoom", "Zoom Meeting", width=800, height=600)]
        stored = window_focus.remember("7", windows=windows)
        self.assertEqual(stored["id"], "7")
        self.assertEqual(json.loads(self.path.read_text()),
                         {"id": "7", "wm_class": "zoom", "title": "Zoom Meeting"})

    def test_an_older_target_file_without_an_id_still_works(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"wm_class": "zoom", "title": "Zoom Meeting"}))
        windows = [window(3, "zoom", "Zoom Meeting")]
        chosen = window_focus.select_window(windows, target=window_focus.load_target())
        self.assertEqual(chosen["id"], "3")

    def test_remember_rejects_a_window_that_closed(self):
        with self.assertRaises(window_focus.FocusError):
            window_focus.remember("9", windows=[window(7, "zoom", "Zoom")])

    def test_remember_rejects_a_missing_id(self):
        with self.assertRaises(window_focus.FocusError):
            window_focus.remember(None, windows=[window(7, "zoom", "Zoom")])

    def test_saving_leaves_no_temporary_files_behind(self):
        window_focus.save_target({"id": "1", "wm_class": "zoom", "title": "Zoom"})
        self.assertEqual([p.name for p in self.path.parent.iterdir()], [self.path.name])

    def test_a_reader_never_sees_a_half_written_target(self):
        # The server is threaded and the CLI can run at the same time, so
        # saves overlap. A plain write truncates first, and a reader that
        # catches that window gets an empty or spliced document.
        window_focus.save_target({"id": "0", "wm_class": "zoom", "title": "x"})
        failures = []
        stop = threading.Event()

        def write():
            for index in range(200):
                if stop.is_set():
                    return
                try:
                    window_focus.save_target(
                        {"id": str(index), "wm_class": "zoom", "title": "y" * index})
                except Exception as err:          # noqa: BLE001 - reported below
                    failures.append(err)
                    return

        def read():
            for _ in range(400):
                if stop.is_set():
                    return
                try:
                    self.assertIsNotNone(window_focus.load_target())
                except Exception as err:          # noqa: BLE001 - reported below
                    failures.append(err)
                    return

        threads = [threading.Thread(target=write), threading.Thread(target=write),
                   threading.Thread(target=read)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        stop.set()
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
