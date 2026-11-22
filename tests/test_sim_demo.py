import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pandas as pd

from app import config, dashboard_data, pilot, tracking
from sim import demo

NOW = datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)  # 14:00 in UTC+8, a Friday


class DemoHistoryTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = mock.patch.object(config, "TRACKING_DB_PATH", Path(folder.name) / "tracking.db")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_month_of_emails_with_a_realistic_mix(self):
        count = demo.add_history(now=NOW)
        events = tracking.all_events()
        table = dashboard_data.emails(events)

        self.assertEqual(len(table), count)
        self.assertTrue(250 <= count <= 500, count)
        days = table["first_seen"].dt.normalize().nunique()
        self.assertGreaterEqual(days, 25)  # spread over the month, not one day
        self.assertTrue(all(datetime.fromisoformat(event["at_utc"]) <= NOW for event in events))
        self.assertEqual(set(table["outcome"]), set(dashboard_data.OUTCOMES))

        summary = pilot.summary(tracking.latest(tracking.REPLIED))
        self.assertGreater(summary["decided"], 100)
        self.assertTrue(0.8 <= summary["same_pods_share"] <= 0.98, summary["same_pods_share"])

    def test_the_same_seed_gives_the_same_month(self):
        first = demo.add_history(seed=3, now=NOW)
        rows = [(e["entry_id"], e["status"], e["at_utc"]) for e in tracking.all_events()]
        with tempfile.TemporaryDirectory() as other, mock.patch.object(
            config, "TRACKING_DB_PATH", Path(other) / "tracking.db"
        ):
            self.assertEqual(demo.add_history(seed=3, now=NOW), first)
            self.assertEqual([(e["entry_id"], e["status"], e["at_utc"]) for e in tracking.all_events()], rows)

    def test_the_clock_is_put_back(self):
        demo.add_history(now=NOW)
        self.assertIs(tracking.datetime, datetime)
        tracking.record({"entry_id": "LIVE"}, tracking.SKIPPED_GUARD)
        live = pd.Timestamp(tracking.history("LIVE")[0]["at_utc"])
        self.assertGreater(live, pd.Timestamp(NOW))


if __name__ == "__main__":
    unittest.main()
