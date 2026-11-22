"""The CRF number model (D-026): tokens, learning, extraction, saving."""

import tempfile
import unittest
from pathlib import Path

import joblib

from app import number_crf


def _emails(count: int) -> list[dict]:
    emails = []
    for i in range(count):
        connote, reference, phone = str(3000000 + i), f"PO{40000 + i}", f"0412{100000 + i}"
        emails.append({
            "subject": "POD request",
            "body": f"Hi,\nPlease send POD for connote {connote}, our ref {reference}.\nRegards\nJo {phone}",
            "connotes": {connote},
            "references": {reference},
        })
    return emails


class TokensTest(unittest.TestCase):
    def test_tokens_know_subject_line_start_and_sign_off(self):
        items = number_crf.tokens("POD 123", "Send 4567890\nRegards\nCall 0412345678")
        by_token = {token: rest for token, *rest in items}
        self.assertEqual(by_token["123"], [True, False, False])  # in the subject
        self.assertEqual(by_token["4567890"], [False, False, False])
        self.assertTrue(by_token["0412345678"][2])  # after "Regards"

    def test_number_features_use_shape_not_the_number_itself(self):
        _, features = number_crf.sequence("", "connote 1234567")
        self.assertEqual(features[1]["word"], "9")  # short shape of 1234567
        self.assertEqual(features[1]["shape"], "9999999")
        self.assertEqual(features[1]["word-1"], "connote")


class CRFTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = number_crf._fit(_emails(30))

    def test_finds_connotes_and_references_and_skips_the_phone_number(self):
        email = _emails(31)[-1]
        tagged = {token: label for token, label, probability in number_crf.tagged(self.model, email["subject"], email["body"]) if probability >= 0.5}
        self.assertEqual(tagged, {"3000030": "CONNOTE", "PO40030": "REFERENCE"})
        self.assertEqual(number_crf.extract(self.model, email["subject"], email["body"], 0.5), ["3000030", "PO40030"])

    def test_probabilities_are_between_zero_and_one(self):
        email = _emails(1)[0]
        for _, _, probability in number_crf.tagged(self.model, email["subject"], email["body"]):
            self.assertTrue(0.0 <= probability <= 1.0)

    def test_saved_model_gives_the_same_answer(self):
        email = _emails(31)[-1]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "crf.pkl"
            joblib.dump(self.model, path)
            loaded = joblib.load(path)
        self.assertEqual(
            number_crf.extract(loaded, email["subject"], email["body"], 0.5),
            number_crf.extract(self.model, email["subject"], email["body"], 0.5),
        )


if __name__ == "__main__":
    unittest.main()
