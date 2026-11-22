"""Append-only tracking, latest event wins (D-010)."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from app import config, tracking


class TrackingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(config, "TRACKING_DB_PATH", Path(self._tmp.name) / "t.db")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_latest_event_is_the_current_status_and_history_is_kept(self):
        email = {"entry_id": "A", "subject": "POD", "sender_email": "x@example.com"}
        tracking.record(email, tracking.WAITING_PBI, numbers=["1"], state={"missing": ["1"]})
        tracking.record({"entry_id": "B"}, tracking.SKIPPED_NO_NUMBER)
        current = tracking.latest(tracking.WAITING_PBI)[0]
        tracking.record(current, tracking.REPLIED, reason="draft")

        self.assertEqual(tracking.latest(tracking.WAITING_PBI), [])
        replied = tracking.latest(tracking.REPLIED)[0]
        # Email fields, numbers and state are carried forward from the event.
        self.assertEqual(replied["subject"], "POD")
        self.assertEqual(replied["numbers"], "1")
        self.assertEqual(replied["state"], {"missing": ["1"]})
        self.assertEqual([event["status"] for event in tracking.history("A")], [tracking.WAITING_PBI, tracking.REPLIED])
        self.assertEqual(len(tracking.latest()), 2)

    def test_empty_status_or_entry_is_refused_by_the_database(self):
        with self.assertRaises(Exception):
            tracking.record({"entry_id": ""}, tracking.ERROR)
        with self.assertRaises(Exception):
            tracking.record({"entry_id": "A"}, "")

    def test_old_database_schema_is_migrated_before_recording(self):
        with mock.patch.object(config, "TRACKING_DB_PATH", Path(self._tmp.name) / "legacy.db"):
            with closing(sqlite3.connect(config.TRACKING_DB_PATH)) as connection, connection:
                connection.execute(
                    """
                    CREATE TABLE events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        entry_id TEXT NOT NULL,
                        at_utc TEXT NOT NULL,
                        status TEXT NOT NULL,
                        received_time TEXT NOT NULL DEFAULT '',
                        sender_email TEXT NOT NULL DEFAULT '',
                        subject TEXT NOT NULL DEFAULT '',
                        body TEXT NOT NULL DEFAULT '',
                        numbers TEXT NOT NULL DEFAULT '',
                        intent TEXT NOT NULL DEFAULT '',
                        confidence REAL,
                        reason TEXT NOT NULL DEFAULT '',
                        state TEXT NOT NULL DEFAULT '{}'
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO events (entry_id, at_utc, status) VALUES ('old', 'now', 'Old')"
                )

            tracking.record(
                {"entry_id": "new", "to_recipients": "POD"},
                tracking.MANUAL_REVIEW,
            )

            events = tracking.all_events()
            self.assertEqual([event["entry_id"] for event in events], ["old", "new"])
            self.assertEqual(events[0]["to_recipients"], "")
            self.assertEqual(events[1]["to_recipients"], "POD")
            self.assertEqual(events[0]["message_id"], "")  # added the same way (D-041)


if __name__ == "__main__":
    unittest.main()
