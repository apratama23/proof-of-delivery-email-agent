"""Cross-validated scores (D-022): every example predicted once, by a model that never saw it."""

import unittest

import numpy as np
from sklearn.model_selection import GroupKFold, StratifiedKFold

from app import model_registry


class Memorizer:
    """Knows only the exact examples it was fitted on; guesses "B" for anything else.

    If a test example leaked into training, it would be answered right.
    """

    def fit(self, inputs, labels):
        self.seen = dict(zip(inputs, labels))
        self.classes_ = np.array(["A", "B"])
        return self

    def predict(self, inputs):
        return np.array([self.seen.get(item, "B") for item in inputs], dtype=object)

    def predict_proba(self, inputs):
        return np.array([[1.0, 0.0] if label == "A" else [0.0, 1.0] for label in self.predict(inputs)])


INPUTS = [f"email {i}" for i in range(20)]
LABELS = ["A"] * 10 + ["B"] * 10


class CrossValidateTest(unittest.TestCase):
    def test_no_example_is_scored_by_a_model_that_saw_it(self):
        folds = list(StratifiedKFold(5, shuffle=True, random_state=0).split(INPUTS, LABELS))
        final, result = model_registry.cross_validate(lambda _: Memorizer(), INPUTS, LABELS, folds, ["A", "B"])
        # Out of fold the memorizer can only guess "B": every "A" is missed.
        self.assertEqual(result["predictions"], ["B"] * 20)
        self.assertEqual(result["test"]["accuracy"], 0.5)
        # The final model learned from all data, so it knows every example.
        self.assertEqual(result["train"]["accuracy"], 1.0)
        self.assertEqual(list(final.predict(INPUTS)), LABELS)

    def test_every_example_gets_one_prediction_and_confidence(self):
        folds = list(StratifiedKFold(5, shuffle=True, random_state=0).split(INPUTS, LABELS))
        _, result = model_registry.cross_validate(lambda _: Memorizer(), INPUTS, LABELS, folds, ["A", "B"])
        self.assertEqual(len(result["predictions"]), 20)
        self.assertEqual(len(result["confidences"]), 20)
        self.assertEqual(sum(result["test"]["confusion_matrix"][0]) + sum(result["test"]["confusion_matrix"][1]), 20)

    def test_grouped_folds_keep_an_email_together(self):
        # Two tokens per email; the second token of each email repeats the first.
        inputs = [f"email {i // 2}" for i in range(20)]
        labels = ["A" if i < 10 else "B" for i in range(20)]
        groups = [i // 2 for i in range(20)]
        folds = list(GroupKFold(5).split(inputs, labels, groups=groups))
        _, result = model_registry.cross_validate(lambda _: Memorizer(), inputs, labels, folds, ["A", "B"])
        # Had the twin token been in training, the memorizer would answer "A".
        self.assertEqual(result["predictions"], ["B"] * 20)


if __name__ == "__main__":
    unittest.main()
