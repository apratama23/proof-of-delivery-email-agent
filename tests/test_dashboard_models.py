"""Model performance tables shown in the dashboard."""

import unittest

from dashboard.tab_models import _intent_performance


class DashboardModelsTest(unittest.TestCase):
    def test_intent_performance_is_broken_out_by_train_test_and_intent(self):
        scores = {
            "accuracy": 0.75,
            "macro_precision": 0.76,
            "macro_recall": 0.75,
            "macro_f1": 0.74,
            "classification_report": {
                "POD_REQUEST": {"support": 2, "precision": 0.8, "recall": 1.0, "f1-score": 0.89},
                "OTHER": {"support": 2, "precision": 0.7, "recall": 0.5, "f1-score": 0.58},
            },
            "confusion_matrix": [[2, 0], [1, 1]],
        }
        record = {
            "labels": ["POD_REQUEST", "OTHER"],
            "models": {"xgboost": {"train": scores, "test": scores}},
        }

        performance, by_intent = _intent_performance(record)

        self.assertEqual(performance.to_dict("records"), [
            {"model": "xgboost", "set": "train", "emails fully right": "3 of 4",
             "precision": "76.0%", "recall": "75.0%", "F1": "74.0%"},
            {"model": "xgboost", "set": "test", "emails fully right": "3 of 4",
             "precision": "76.0%", "recall": "75.0%", "F1": "74.0%"},
        ])
        self.assertEqual(by_intent["intent"].tolist(), ["POD_REQUEST", "OTHER", "POD_REQUEST", "OTHER"])
        self.assertEqual(by_intent["F1"].tolist(), ["89.0%", "58.0%", "89.0%", "58.0%"])


if __name__ == "__main__":
    unittest.main()
