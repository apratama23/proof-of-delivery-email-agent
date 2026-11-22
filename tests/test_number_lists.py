"""Connote lists and ranges in short form are written out, by the rules agreed with the product owner (D-034)."""

import os
import unittest
from unittest import mock

from app import number_classifier, number_crf, number_lists


class ExpandTest(unittest.TestCase):
    def expand(self, text, found):
        return number_lists.expand(text, found)

    def test_the_agreed_examples(self):
        cases = [
            ("7335842/843/844", ["7335842"], ["7335842", "7335843", "7335844"]),
            ("7335399/7335401/02/03/04", ["7335399", "7335401"],
             ["7335399", "7335401", "7335402", "7335403", "7335404"]),
            ("7336101-103", ["7336101"], ["7336101", "7336102", "7336103"]),
            ("7336101-7336103", ["7336101", "7336103"], ["7336101", "7336102", "7336103"]),
            ("7336101-7336150", ["7336101", "7336150"], ["7336101", "7336150"]),  # over 20: two numbers
            ("7336101-5666103", ["7336101", "5666103"], ["7336101", "5666103"]),  # lower: two numbers
            ("614024-610423\n615186-611678\n617270-613714\n622318-618563 (622220 17/11/2025)",
             ["614024", "610423", "615186", "611678", "617270", "613714", "622318", "618563", "622220"],
             ["614024", "610423", "615186", "611678", "617270", "613714", "622318", "618563", "622220"]),
            ("7335842 / 843 / 844", ["7335842"], ["7335842", "7335843", "7335844"]),  # spaces allowed
            ("0086055559/60", ["0086055559"], ["0086055559", "0086055560"]),  # leading zeros kept
        ]
        for text, found, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(self.expand(text, found), (expected, []))

    def test_a_short_range_too_long_or_backwards_is_not_guessed(self):
        self.assertEqual(self.expand("7336101-199", ["7336101"]), (["7336101"], ["7336101-199"]))
        self.assertEqual(self.expand("7336105-101", ["7336105"]), (["7336105"], ["7336105-101"]))

    def test_codes_with_letters_dates_and_unrelated_lists_are_left_alone(self):
        self.assertEqual(self.expand("XY62302-03 and MN675418-3", ["XY62302"]), (["XY62302"], []))
        self.assertEqual(self.expand("2/3 pallets, see 1234567", ["1234567"]), (["1234567"], []))
        # A list that starts with a number the extractor did not find is not read.
        self.assertEqual(self.expand("call 1234567/89 for 7654321", ["7654321"]), (["7654321"], []))

    def test_a_connote_with_a_one_or_two_digit_part_is_one_number_as_written(self):
        for written in ("4653685-1", "4653685-01", "80655505 - 1"):
            with self.subTest(written=written):
                self.assertEqual(self.expand(f"POD for {written}", [written]), ([written], []))
        # The regex does not take the front of it as a number of its own.
        self.assertEqual(number_classifier.extract_numbers(None, "POD", "Please send POD for 4653685-01"), [])

    def test_an_invoice_and_a_connote_are_split_for_power_bi(self):
        self.assertEqual(self.expand("20260407 - 80655502", ["80655502"]), (["20260407", "80655502"], []))

    def test_a_short_part_returned_on_its_own_is_dropped_and_order_is_kept(self):
        self.assertEqual(self.expand("7777777 and 7335842/843", ["7777777", "7335842", "843"]),
                         (["7777777", "7335842", "7335843"], []))


class ExtractNumbersTest(unittest.TestCase):
    def test_a_list_the_crf_returns_as_one_piece_is_written_out(self):
        self.assertEqual(self.expand_crf("7336101-103"), ["7336101", "7336102", "7336103"])

    def expand_crf(self, piece):
        with mock.patch.object(number_crf, "extract", return_value=[piece]):
            return number_classifier.extract_numbers(object(), "POD", f"Please send POD for {piece}")

    def test_the_regex_reads_a_short_range_too(self):
        numbers = number_classifier.extract_numbers(None, "POD request", "Please send POD for 7336101-103")
        self.assertEqual(numbers, ["7336101", "7336102", "7336103"])

    def test_the_regex_path_writes_lists_out(self):
        numbers = number_classifier.extract_numbers(None, "POD request", "Please send POD for 7335842/843/844")
        self.assertEqual(numbers, ["7335842", "7335843", "7335844"])

    def test_the_crf_path_writes_lists_out_and_drops_its_short_parts(self):
        with mock.patch.object(number_crf, "extract", return_value=["7335842", "843"]):
            numbers = number_classifier.extract_numbers(object(), "POD", "Please send POD for 7335842/843/844")
        self.assertEqual(numbers, ["7335842", "7335843", "7335844"])

    def test_an_unreadable_list_is_reported_to_the_caller(self):
        problems = []
        number_classifier.extract_numbers(None, "POD", "Please send POD for 7336101-199", problems=problems)
        self.assertEqual(problems, ["7336101-199"])

    def test_the_prefixes_come_from_the_setting_in_its_order(self):
        from app import config, entity_extractor

        with mock.patch.dict(os.environ, {"POD_CONNOTE_PREFIXES": "ab; XY"}):
            self.assertEqual(config.connote_prefixes(), ("AB", "XY"))
            self.assertTrue(entity_extractor.looks_like_number("AB-10234"))
            self.assertFalse(entity_extractor.looks_like_number("KL-10234"))  # not set
        with mock.patch.dict(os.environ, {"POD_CONNOTE_PREFIXES": ""}):
            self.assertFalse(entity_extractor.looks_like_number("AB-10234"))  # none set: digits only
            self.assertTrue(entity_extractor.looks_like_number("1234567"))
        with mock.patch.dict(os.environ, {"POD_CONNOTE_PREFIXES": "A1"}), self.assertRaises(config.ConfigError):
            config.connote_prefixes()

    @mock.patch.dict(os.environ, {"POD_CONNOTE_PREFIXES": "AB;XY;KL"})  # the prefixes are set in .env (D-060)
    def test_numbers_with_a_prefix_from_the_setting_are_recognised_by_the_regex(self):
        self.assertEqual(number_classifier.extract_numbers(None, "POD", "Please send POD for XY62302"), ["XY62302"])


if __name__ == "__main__":
    unittest.main()
