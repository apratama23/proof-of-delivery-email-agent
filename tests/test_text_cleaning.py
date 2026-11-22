"""Text normalization shared by intent training and prediction (D-016)."""

import unittest
from unittest import mock

import pandas as pd

from app import classifier, config


class NormalizeTextTest(unittest.TestCase):
    def test_numbers_of_any_shape_become_one_token(self):
        self.assertEqual(
            classifier.normalize_text("POD for 1234567, AB-10234 and PO40001 by 29/09"),
            "pod for numtoken , numtoken and numtoken by numtoken / numtoken",
        )

    def test_urls_and_email_addresses_become_tokens(self):
        self.assertEqual(
            classifier.normalize_text("See https://x.example/a?b=1 or mail ops@example.com"),
            "see urltoken or mail emailtoken",
        )

    def test_whitespace_and_case_are_normalized(self):
        self.assertEqual(classifier.normalize_text("  Please\n\n SEND\tPOD  "), "please send pod")

    def test_empty_text_stays_empty(self):
        self.assertEqual(classifier.normalize_text(""), "")

    def test_sender_becomes_its_domain_and_recipients_are_kept(self):
        text = classifier.build_email_text(
            "POD 1234567", "ops@example.com", "pod@example.com", "Send 7654321"
        )
        self.assertIn("Sender: example.com", text)
        self.assertIn("To: pod@example.com", text)
        self.assertIn("Subject: pod numtoken", text)
        self.assertIn("Body: send numtoken", text)

    def test_training_and_prediction_see_the_same_input(self):
        row = {
            "subject": "POD 1234567",
            "sender_email": "ops@example.com",
            "to_recipients": "POD",
            "body": "Please send https://x.example 7654321",
        }
        training_text = classifier.build_training_features(pd.DataFrame([row])).iloc[0]
        prediction_text = classifier.build_email_text(
            row["subject"], row["sender_email"], row["to_recipients"], row["body"]
        )
        self.assertEqual(training_text, prediction_text)

    def test_internal_senders_look_the_same_in_training_and_live(self):
        with mock.patch.object(config, "POD_SHARED_MAILBOX_ADDRESS", "pod@example6.com"):
            exchange_path = classifier.sender_feature("/O=EXCHANGELABS/OU=EXCHANGE ADMINISTRATIVE GROUP/CN=RECIPIENTS/CN=jsmith")
            smtp = classifier.sender_feature("J.Smith@Example6.com")
            external = classifier.sender_feature("ops@example3.com")
        self.assertEqual((exchange_path, smtp, external), ("internal", "internal", "example3.com"))
        self.assertEqual(classifier.sender_feature(float("nan")), "unknown")

    def test_missing_values_do_not_become_the_word_nan(self):
        text = classifier.build_email_text(float("nan"), None, "", float("nan"))
        self.assertNotIn("nan", text)



class StemmingTest(unittest.TestCase):
    """Intent models stem each word before the vectorizer (D-016)."""

    def test_each_word_is_stemmed_then_lower_cased(self):
        self.assertEqual(classifier.stem_text("Please send PODs, sending plans"), "pleas send pods, send plan")

    def test_a_new_model_stems_and_survives_saving(self):
        import joblib, tempfile
        texts = ["please send the pods", "sending plans for numtoken", "invoice attached", "payment made"] * 3
        labels = ["POD_REQUEST", "POD_REQUEST", "OTHER", "OTHER"] * 3
        model = classifier._build_model("logreg").fit(texts, labels)
        vocabulary = model.named_steps["tfidf"].vocabulary_
        self.assertIn("plan", vocabulary)
        self.assertNotIn("plans", vocabulary)
        with tempfile.TemporaryDirectory() as folder:
            path = f"{folder}/intent.joblib"
            joblib.dump(model, path)
            self.assertEqual(joblib.load(path).predict(["please send the pod"])[0], "POD_REQUEST")


if __name__ == "__main__":
    unittest.main()
