"""Training saves every model and its history, and activates none."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from app import classifier, config, model_registry, number_crf


def _intent_rows(count: int) -> pd.DataFrame:
    rows = []
    for index in range(count):
        rows.append(
            {
                "subject": f"POD request {1000000 + index}",
                "sender_email": "customer@example.com",
                "to_recipients": "POD",
                "body": f"Hi, please send the proof of delivery for connote {1000000 + index}.",
                "intent_label": "POD_REQUEST",
            }
        )
        rows.append(
            {
                "subject": f"Invoice {2000000 + index}",
                "sender_email": "accounts@example.com",
                "to_recipients": "POD",
                "body": f"Invoice {2000000 + index} is overdue, please arrange payment.",
                "intent_label": "OTHER",
            }
        )
    return pd.DataFrame(rows)


def _number_rows(count: int) -> pd.DataFrame:
    rows = []
    for index in range(count):
        connote = str(3000000 + index)
        reference = f"PO{40000 + index}"
        rows.append(
            {
                "subject": "POD request",
                "body": f"Please send POD for connote {connote}, our reference {reference}. Thanks",
                "intent_label": "POD_REQUEST",
                "connotes_manual": connote,
                "reference_number_manual": reference,
            }
        )
    return pd.DataFrame(rows)


class TrainingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        # Two folds are enough to check the flow; quality is not tested here.
        folds = mock.patch.object(model_registry, "FOLDS", 2)
        folds.start()
        self.addCleanup(folds.stop)
        patcher = mock.patch.object(config, "MODELS_DIR", self.tmp / "models")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def _assert_history(self, path: Path, task: str, model_types, labels):
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["task"], task)
        self.assertIn("cross-validation", record["evaluation"])
        self.assertEqual(record["n_test"], record["n_train"])  # every example scored once
        self.assertEqual(record["labels"], list(labels))
        self.assertEqual(set(record["models"]), set(model_types))
        for result in record["models"].values():
            for split in ("train", "test"):
                for key in ("accuracy", "macro_precision", "macro_recall", "macro_f1"):
                    self.assertIn(key, result[split])
                matrix = result[split]["confusion_matrix"]
                self.assertEqual(len(matrix), len(labels))
            # One prediction and confidence per test example.
            self.assertEqual(len(result["predictions"]), record["n_test"])
            self.assertEqual(len(result["confidences"]), record["n_test"])
            self.assertTrue(all(0.0 <= value <= 1.0 for value in result["confidences"]))
        self.assertEqual(len(record["test_labels"]), record["n_test"])

    def test_intent_training_saves_all_models_and_history_without_activating(self):
        history = classifier.train_and_compare(_intent_rows(30), "training.xlsx")

        for name in model_registry.INTENT_MODEL_TYPES:
            self.assertTrue(model_registry.model_path("intent", name).exists(), name)
        self._assert_history(
            history, "intent", model_registry.INTENT_MODEL_TYPES, classifier.INTENT_CLASSES
        )
        self.assertIsNone(model_registry.get_active("intent"))

    def test_intent_training_refuses_empty_data(self):
        with self.assertRaises(ValueError):
            classifier.train_and_compare(pd.DataFrame(columns=["body", "intent_label"]))

    def test_number_training_saves_all_models_and_history_without_activating(self):
        training_file = self.tmp / "training.xlsx"
        _number_rows(30).to_excel(training_file, index=False)

        history = number_crf.train_and_compare(training_file)

        self.assertTrue(model_registry.model_path("number", "crf").exists())
        # The CRF and its yardstick, regex, side by side.
        self._assert_history(history, "number", ("crf", "regex"), number_crf.LABELS)
        record = json.loads(history.read_text(encoding="utf-8"))
        self.assertIn("by email", record["evaluation"])
        # Train scores of the saved model, to set against the test scores.
        self.assertEqual(set(record["emails_train"]), {"crf"})
        self.assertEqual(record["emails_train"]["crf"]["emails"], 30)
        for name in ("crf", "regex"):
            scores = record["emails"][name]
            self.assertEqual(scores["emails"], 30)
            for key in ("exact", "precision", "recall", "f1", "no_number"):
                self.assertIn(key, scores)
            self.assertAlmostEqual(
                scores["f1"], 2 * scores["precision"] * scores["recall"] / (scores["precision"] + scores["recall"])
            )
        self.assertIsNone(model_registry.get_active("number"))

    def test_number_training_refuses_too_few_examples(self):
        training_file = self.tmp / "training.xlsx"
        _number_rows(0).reindex(
            columns=["subject", "body", "intent_label", "connotes_manual", "reference_number_manual"]
        ).to_excel(training_file, index=False)
        with self.assertRaises(ValueError):
            number_crf.train_and_compare(training_file)

    def test_unknown_model_names_are_refused(self):
        # An unknown name must fail, not quietly become another model.
        with self.assertRaises(ValueError):
            classifier._build_model("random_forest")  # no longer an intent model
        with self.assertRaises(ValueError):
            model_registry.set_active("number", "xgboost")  # no longer a number model

    def test_intent_pseudo_labels_fill_only_unlabeled_rows(self):
        classifier.train_and_compare(_intent_rows(30), "training.xlsx")
        model_registry.set_active("intent", "logreg")
        training_file = self.tmp / "training.xlsx"
        pd.DataFrame([
            {"entry_id": "M", "subject": "POD 1", "body": "please send the proof of delivery", "intent_label": "OTHER"},
            {"entry_id": "U", "subject": "POD request 1000001", "sender_email": "customer@example.com",
             "to_recipients": "POD", "body": "Hi, please send the proof of delivery for connote 1000001.", "intent_label": ""},
            {"entry_id": "S", "subject": "Invoice", "body": "please pay", "intent_label": "",
             "intent_label_pseudo": "OTHER", "label_source": "System decision"},
        ]).to_excel(training_file, index=False)

        self.assertEqual(classifier.generate_pseudo_labels(training_file, threshold=0.0), 1)

        data = pd.read_excel(training_file, dtype=str).set_index("entry_id")
        self.assertEqual(len(data), 3)  # no row lost or added
        self.assertEqual(data.loc["U", "intent_label_pseudo"], "POD_REQUEST")
        self.assertTrue(0 < float(data.loc["U", "pseudo_confidence"]) <= 1)
        self.assertTrue(pd.isna(data.loc["U", "intent_label"]))  # a suggestion, not a label
        self.assertEqual(data.loc["M", "intent_label"], "OTHER")  # a manual label is never touched
        self.assertTrue(pd.isna(data.loc["M", "intent_label_pseudo"]))
        self.assertEqual(data.loc["S", "intent_label_pseudo"], "OTHER")  # the agent's decision awaits review
        self.assertEqual(data.loc["U", "subject"], "POD request 1000001")  # other columns kept

    def test_number_pseudo_labels_fill_rows_without_a_manual_intent(self):
        training_file = self.tmp / "training.xlsx"
        _number_rows(30).to_excel(training_file, index=False)
        number_crf.train_and_compare(training_file)
        model_registry.set_active("number", "crf")
        pd.DataFrame([
            {"entry_id": "U", "subject": "POD request", "intent_label": "",
             "body": "Please send POD for connote 3999999, our reference PO49999. Thanks"},
            {"entry_id": "L", "subject": "POD request", "intent_label": "POD_REQUEST",
             "body": "Please send POD for connote 3888888, our reference PO48888. Thanks"},
        ]).to_excel(training_file, index=False)

        number_crf.generate_pseudo_labels(training_file, 0.5)

        data = pd.read_excel(training_file, dtype=str).set_index("entry_id")
        found = f"{data.loc['U', 'connotes_pseudo']};{data.loc['U', 'reference_number_pseudo']}"
        self.assertIn("3999999", found)
        self.assertRegex(f"{data.loc['U', 'connotes_pseudo_confidence']};{data.loc['U', 'reference_pseudo_confidence']}",
                         r"3999999:0\.\d{3}|3999999:1\.000")
        self.assertTrue(pd.isna(data.loc["L", "connotes_pseudo"]))  # labeled rows are left to the person

    def test_pseudo_labels_need_an_active_model(self):
        training_file = self.tmp / "training.xlsx"
        _intent_rows(2).to_excel(training_file, index=False)
        with self.assertRaises(FileNotFoundError):
            classifier.generate_pseudo_labels(training_file)
        with self.assertRaises(FileNotFoundError):
            number_crf.generate_pseudo_labels(training_file, 0.5)


if __name__ == "__main__":
    unittest.main()
