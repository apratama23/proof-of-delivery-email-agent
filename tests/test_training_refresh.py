"""Pulling processed emails into the training data, and keeping their marks (D-013)."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from app import classifier, config, historical_pull, model_registry, tracking, training_refresh


class TrainingRefreshTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.training = tmp / "training.xlsx"
        for patcher in (
            mock.patch.object(config, "TRACKING_DB_PATH", tmp / "t.db"),
            mock.patch.object(config, "MODELS_DIR", tmp / "models"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        email = {"entry_id": "E1", "subject": "POD", "body": "send 1234567", "sender_email": "a@example7.com", "to_recipients": "POD"}
        tracking.record(email, tracking.REPLIED, numbers=["1234567"], intent="POD_REQUEST", confidence=0.97)
        tracking.record({"entry_id": "E2", "body": "hi"}, tracking.SKIPPED_NO_NUMBER)
        tracking.record({"entry_id": "E3", "body": "auto"}, tracking.SKIPPED_GUARD)
        pd.DataFrame([{"entry_id": "OLD", "body": "old email", "intent_label": "OTHER"}]).to_excel(self.training, index=False)

    def test_a_review_becomes_a_manual_review_label_by_entry_id(self):
        entry = tracking.latest(tracking.REPLIED)[0]
        review = {"intent": "POD_REQUEST", "connotes": ["1234567"], "references": ["PO-1"]}
        self.assertEqual(training_refresh.add_review(self.training, entry, review), "")
        # Saving again updates the same row instead of adding one.
        self.assertEqual(training_refresh.add_review(self.training, entry, review), "")
        data = pd.read_excel(self.training, dtype=str)
        self.assertEqual(list(data["entry_id"]), ["OLD", "E1"])
        row = data.set_index("entry_id").loc["E1"]
        self.assertEqual((row["intent_label"], row["connotes_manual"], row["reference_number_manual"]),
                         ("POD_REQUEST", "1234567", "PO-1"))
        self.assertEqual(row["label_source"], "Manual review")
        self.assertTrue(row["labeled_at"].startswith("20"))
        self.assertEqual((row["intent_label_pseudo"], row["connote_number_auto"]), ("POD_REQUEST", "1234567"))
        # Pull new data does not add it a second time.
        self.assertEqual(training_refresh.pull_system_decisions(self.training), 0)

    def test_a_review_replaces_an_earlier_label_and_keeps_the_other_columns(self):
        pd.DataFrame([{"entry_id": "OLD", "body": "old email", "intent_label": "OTHER",
                       "connotes_pseudo": "7654321", "pseudo_confidence": 0.4}]).to_excel(self.training, index=False)
        review = {"intent": "POD_REQUEST", "connotes": ["7654321"], "references": []}
        changed = training_refresh.add_review(self.training, {"entry_id": "OLD"}, review)
        self.assertEqual(changed, "changed from OTHER")
        row = pd.read_excel(self.training, dtype=str).set_index("entry_id").loc["OLD"]
        self.assertEqual((row["intent_label"], row["body"], row["connotes_pseudo"]), ("POD_REQUEST", "old email", "7654321"))
        self.assertTrue(pd.isna(row["reference_number_manual"]))
        # From History the source is Manual.
        training_refresh.add_review(self.training, {"entry_id": "OLD"}, review, label_source=training_refresh.MANUAL)
        self.assertEqual(pd.read_excel(self.training, dtype=str).set_index("entry_id").loc["OLD", "label_source"], "Manual")
        # It counts for training like any manual label.
        self.assertEqual(classifier.load_training_data(self.training).set_index("entry_id").loc["OLD", "intent_label"], "POD_REQUEST")

    def test_new_processed_emails_arrive_as_unreviewed_system_decisions(self):
        added = training_refresh.pull_system_decisions(self.training)

        self.assertEqual(added, 1)  # guard and no-number emails are not training material
        data = pd.read_excel(self.training, dtype=str).set_index("entry_id")
        row = data.loc["E1"]
        self.assertEqual(row["label_source"], "System decision")
        self.assertEqual(row["intent_label_pseudo"], "POD_REQUEST")
        self.assertTrue(pd.isna(row["intent_label"]))  # not a training label yet
        self.assertEqual(row["connote_number_auto"], "1234567")
        self.assertEqual(data.loc["OLD", "intent_label"], "OTHER")  # untouched

    def test_second_pull_adds_nothing_twice(self):
        training_refresh.pull_system_decisions(self.training)
        self.assertEqual(training_refresh.pull_system_decisions(self.training), 0)

    def test_unreviewed_system_decisions_do_not_train_the_model(self):
        training_refresh.pull_system_decisions(self.training)
        labeled = classifier.load_training_data(self.training)
        self.assertEqual(labeled["entry_id"].tolist(), ["OLD"])

    def test_historical_update_keeps_the_label_source(self):
        training_refresh.pull_system_decisions(self.training)
        existing = pd.read_excel(self.training, dtype=str)
        refreshed = {"entry_id": "E1", "body": "send 1234567", **{c: "" for c in ("intent_label", "label_source")}}
        merged = historical_pull._merge_training_records([refreshed], existing)
        self.assertEqual(merged.set_index("entry_id").loc["E1", "label_source"], "System decision")

    def test_pseudo_labeling_does_not_overwrite_system_decisions(self):
        training_refresh.pull_system_decisions(self.training)

        class Always:
            def predict(self, texts):
                return ["OTHER" for _ in texts]

            def predict_proba(self, texts):
                return [[0.99, 0.01] for _ in texts]

        with mock.patch.object(model_registry, "load_active", return_value=Always()):
            classifier.generate_pseudo_labels(self.training, threshold=0.5)
        data = pd.read_excel(self.training, dtype=str).set_index("entry_id")
        self.assertEqual(data.loc["E1", "intent_label_pseudo"], "POD_REQUEST")


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(config, "PROJECT_DIR", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        (Path(self._tmp.name) / ".env").write_text("POD_OUTLOOK_MAILBOX=POD\nPOD_SEND_MODE=draft\n", encoding="utf-8")

    def test_allowed_setting_is_replaced_in_place_or_added(self):
        config.save_setting("POD_SEND_MODE", "send")
        config.save_setting("POD_CONFIDENCE_THRESHOLD", "0.85")
        text = (Path(self._tmp.name) / ".env").read_text(encoding="utf-8")
        self.assertEqual(text, "POD_OUTLOOK_MAILBOX=POD\nPOD_SEND_MODE=send\nPOD_CONFIDENCE_THRESHOLD=0.85\n")
        self.assertEqual(config.current_setting("POD_SEND_MODE"), "send")

    def test_the_two_targets_are_90_percent_until_the_owner_changes_them(self):
        # D-054: the pilot's PODs unchanged target and the intent precision target.
        self.assertEqual((config.pilot_target(), config.precision_target()), (0.90, 0.90))
        config.save_setting("POD_PILOT_TARGET", "0.85")
        config.save_setting("POD_PRECISION_TARGET", "0.95")
        self.assertEqual((config.pilot_target(), config.precision_target()), (0.85, 0.95))
        for value in ("0.4", "1.2", "ninety"):
            with self.subTest(value=value), self.assertRaises(config.ConfigError):
                config.save_setting("POD_PILOT_TARGET", value)

    def test_other_settings_and_bad_values_are_refused(self):
        for name, value in (
            ("POD_OUTLOOK_MAILBOX", "Other"),
            ("POD_SEND_MODE", "sometimes"),
            ("POD_CONFIDENCE_THRESHOLD", "1.5"),
            ("POD_CONFIDENCE_THRESHOLD", "high"),
        ):
            with self.assertRaises(config.ConfigError):
                config.save_setting(name, value)
        self.assertEqual(config.current_setting("POD_SEND_MODE"), "draft")


class AtThresholdTest(unittest.TestCase):
    def test_precision_and_coverage_of_what_the_agent_would_act_on(self):
        record = {
            "test_labels": ["POD_REQUEST", "POD_REQUEST", "OTHER", "POD_REQUEST"],
            "models": {"m": {
                "predictions": ["POD_REQUEST", "POD_REQUEST", "POD_REQUEST", "OTHER"],
                "confidences": [0.95, 0.80, 0.92, 0.99],
            }},
        }
        high = model_registry.at_threshold(record, "m", 0.90)
        self.assertEqual((high["acted"], high["correct"]), (2, 1))
        self.assertEqual(high["precision"], 0.5)
        self.assertAlmostEqual(high["coverage"], 1 / 3)
        nothing = model_registry.at_threshold(record, "m", 0.999)
        self.assertIsNone(nothing["precision"])
        self.assertEqual(nothing["coverage"], 0)


if __name__ == "__main__":
    unittest.main()
