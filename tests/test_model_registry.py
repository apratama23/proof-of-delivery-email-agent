"""Active model choice and training history."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import joblib

from app import config, model_registry


class ModelRegistryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.models_dir = Path(self._tmp.name)
        patcher = mock.patch.object(config, "MODELS_DIR", self.models_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_nothing_is_active_before_a_choice(self):
        self.assertIsNone(model_registry.get_active("intent"))
        self.assertIsNone(model_registry.load_active("intent"))

    def test_unknown_model_name_is_refused(self):
        with self.assertRaises(ValueError):
            model_registry.set_active("intent", "regex")  # regex is numbers only
        with self.assertRaises(ValueError):
            model_registry.set_active("number", "bert")

    def test_untrained_model_is_refused(self):
        with self.assertRaises(FileNotFoundError):
            model_registry.set_active("intent", "logreg")
        self.assertIsNone(model_registry.get_active("intent"))

    def test_trained_model_can_be_activated_and_loaded(self):
        path = model_registry.model_path("intent", "logreg")
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"stand-in": "model"}, path)

        model_registry.set_active("intent", "logreg")

        self.assertEqual(model_registry.get_active("intent"), "logreg")
        self.assertEqual(model_registry.load_active("intent"), {"stand-in": "model"})

    def test_regex_needs_no_file_and_loads_as_none(self):
        model_registry.set_active("number", "regex")
        self.assertEqual(model_registry.get_active("number"), "regex")
        self.assertIsNone(model_registry.load_active("number"))

    def test_choices_per_task_are_independent(self):
        model_registry.set_active("number", "regex")
        self.assertIsNone(model_registry.get_active("intent"))

    def test_history_is_listed_oldest_first_per_task(self):
        first = model_registry.save_history("intent", {"n": 1})
        model_registry.save_history("number", {"n": 2})
        second = model_registry.save_history("intent", {"n": 3})

        self.assertEqual(model_registry.list_history("intent"), [first, second])
        self.assertEqual(len(model_registry.list_history("number")), 1)

    def test_runs_saved_at_the_same_clock_time_are_all_kept_in_order(self):
        # Python 3.10 on Windows reads the clock in ~15 ms steps; freeze it.
        class FrozenClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2020, 1, 1, tzinfo=timezone.utc)

        with mock.patch.object(model_registry, "datetime", FrozenClock):
            paths = [model_registry.save_history("intent", {"run": run}) for run in range(3)]
        self.assertEqual(len(set(paths)), 3)
        self.assertEqual(model_registry.list_history("intent"), paths)
        runs = [model_registry.load_history(path)["run"] for path in model_registry.list_history("intent")]
        self.assertEqual(runs, [0, 1, 2])


if __name__ == "__main__":
    unittest.main()
