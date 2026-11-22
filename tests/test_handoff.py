"""JSON handoff to and from Power Automate (D-009)."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app import config, handoff

PBI_ENV = {
    "POD_PBI_TABLE": "POD Data",
    "POD_PBI_CONNOTE_COLUMN": "Connote",
    "POD_PBI_REFERENCE_COLUMN": "Reference",
    "POD_PBI_STATUS_COLUMN": "POD Status",
    "POD_OUTSTANDING_COLUMNS": "Connote No=connote;Ref=reference;Status=pod_status;"
    "Requested By=sender_email;Customer=pbi:Customer;Asked=number",
    # Blank the optional ones too, so values in a local .env cannot leak in.
    **{name: "" for name in ("POD_PBI_LINKS", "POD_PBI_COLUMNS", "POD_PBI_SECOND_TABLE",
                             "POD_PBI_SECOND_CONNOTE_COLUMN", "POD_PBI_SECOND_REFERENCE_COLUMN", "POD_PBI_SECOND_COLUMNS")},
}


class HandoffTest(unittest.TestCase):
    def test_a_backslash_typed_on_a_card_still_gives_a_readable_answer(self):
        results = self.dir / "teams_results"
        results.mkdir(parents=True)
        (results / "c.json").write_text('{"choice": "pod", "connotes": "PO\\123", "references": ""}', encoding="utf-8")
        self.assertEqual(handoff.read_teams_result("c")["connotes"], "PO\\123")

    def test_a_teams_answer_without_the_references_field_says_so(self):
        results = self.dir / "teams_results"
        results.mkdir(parents=True)
        (results / "old.json").write_text('{"choice": "pod", "connotes": "1234567"}', encoding="utf-8")
        (results / "new.json").write_text('{"choice": "pod", "connotes": "", "references": "PO-1"}', encoding="utf-8")
        self.assertIsNone(handoff.read_teams_result("old")["references"])
        self.assertEqual(handoff.read_teams_result("new")["references"], "PO-1")

    def test_a_result_file_still_being_written_is_read_next_time_and_an_old_broken_one_raises(self):
        results = self.dir / "pbi_results"
        results.mkdir(parents=True)
        (results / "abc.json").write_text('{"rows": [{"Conn', encoding="utf-8")  # half written or synced
        self.assertIsNone(handoff.read_pbi_result("abc"))
        old = time.time() - handoff.PARTIAL_FILE_SECONDS - 1
        os.utime(results / "abc.json", (old, old))
        with self.assertRaises(ValueError):
            handoff.read_pbi_result("abc")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        for patcher in (
            mock.patch.object(config, "HANDOFF_DIR", self.dir),
            mock.patch.dict(os.environ, PBI_ENV),
            # A card's Open the POD mailbox link needs it (D-061); never read from a local .env.
            mock.patch.object(config, "POD_SHARED_MAILBOX_ADDRESS", "pod@example.com"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_query_filters_both_columns_with_written_and_cleaned_numbers(self):
        query = handoff.build_pbi_query(["ab-10234", "7654321"])
        self.assertTrue(query.startswith("EVALUATE"))
        self.assertIn("'POD Data'[Connote] & \"\" IN {\"007654321\", \"07654321\", \"7654321\", \"AB-10234\", \"AB10234\"}", query)
        self.assertIn("CONTAINSSTRING('POD Data'[Reference] & \"\", \"AB10234\")", query)

    def test_query_escapes_quotes_in_names_and_values(self):
        with mock.patch.dict(os.environ, {"POD_PBI_TABLE": "Bob's", "POD_PBI_CONNOTE_COLUMN": "C]1"}):
            query = handoff.build_pbi_query(['12"34'])
        self.assertIn("'Bob''s'[C]]1]", query)
        self.assertIn('"12""34"', query)

    def test_query_brings_columns_from_linked_tables(self):
        settings = {
            "POD_PBI_TABLE": "TMS",
            "POD_PBI_LINKS": "POD Scans[Con Note]=TMS[Connote];TMS[Connote]=DeliveryApp[Consignment]",
            "POD_PBI_COLUMNS": "Sender=TMS[Sender];POD Status=POD Scans[Status];Depot=DeliveryApp[Depot]",
        }
        with mock.patch.dict(os.environ, settings):
            query = handoff.build_pbi_query(["1234567"])
        self.assertTrue(query.startswith("EVALUATE\nSELECTCOLUMNS("))
        self.assertIn('"Connote", \'TMS\'[Connote]', query)
        self.assertIn('"Reference", \'TMS\'[Reference]', query)
        self.assertIn('"Sender", \'TMS\'[Sender]', query)
        self.assertIn(
            '"POD Status", MAXX(FILTER(\'POD Scans\', \'POD Scans\'[Con Note] & "" = '
            '\'TMS\'[Connote] & ""), \'POD Scans\'[Status])',
            query,
        )
        # The link may be written either way round.
        self.assertIn("FILTER('DeliveryApp', 'DeliveryApp'[Consignment] & \"\" = 'TMS'[Connote] & \"\")", query)

    def test_query_returns_the_sender_and_a_linked_scan_path_column(self):
        settings = {
            "POD_PBI_TABLE": "Main",
            "POD_PBI_LINKS": "Scans[Key]=Main[Key]",
            "POD_PBI_COLUMNS": "Sender=Main[Sender];POD Status=Main[POD Status];Scan Path=Scans[Path]",
        }
        with mock.patch.dict(os.environ, settings):
            query = handoff.build_pbi_query(["80655501"])
        self.assertIn('"Sender", \'Main\'[Sender]', query)
        self.assertIn('"POD Status", \'Main\'[POD Status]', query)
        self.assertIn(
            '"Scan Path", MAXX(FILTER(\'Scans\', \'Scans\'[Key] & "" = '
            '\'Main\'[Key] & ""), \'Scans\'[Path])',
            query,
        )

    def test_leading_zeros_do_not_count_in_power_bi_either_way(self):
        query = handoff.build_pbi_query(["0086055559"])
        # A whole connote: the number with and without its zeros, and padded.
        self.assertIn('IN {"0086055559", "086055559", "86055559"}', query)
        # A reference is searched without its zeros, which a padded reference contains.
        self.assertIn("CONTAINSSTRING('POD Data'[Reference] & \"\", \"86055559\")", query)
        query = handoff.build_pbi_query(["80655501"])
        self.assertNotIn("CONTAINSSTRING('POD Data'[Reference] & \"\", \"0080655501\")", query)
        rows = [
            {"Connote": "86055559", "Reference": ""},
            {"Connote": "1111111", "Reference": "PO-1, 0077777777"},
        ]
        matched = handoff.match_rows(["0086055559", "77777777", "086055558"], rows)
        self.assertEqual(matched["0086055559"], rows[0])  # asked with zeros, stored without
        self.assertEqual(matched["77777777"], rows[1])  # asked without, stored with
        self.assertIsNone(matched["086055558"])  # a different number

    def test_reference_with_leading_zeros_matches_the_unpadded_request(self):
        rows = [{"Connote": "12345678", "Reference": "0080655501"}]
        self.assertEqual(handoff.match_rows(["80655501"], rows)["80655501"], rows[0])

    def test_a_row_with_the_number_exactly_as_written_wins_over_a_zero_variant(self):
        rows = [{"Connote": "086055559", "Reference": ""}, {"Connote": "86055559", "Reference": ""}]
        self.assertEqual(handoff.match_rows(["86055559"], rows)["86055559"], rows[1])

    def test_a_connote_s_non_pickup_row_wins_and_a_pickup_only_row_still_counts(self):
        rows = [
            {"Connote": "1111111", "Reference": "", "POD Status": "PickUp"},
            {"Connote": "1111111", "Reference": "", "POD Status": "Not Received"},
            {"Connote": "2222222", "Reference": "", "POD Status": "pickup"},
        ]
        matched = handoff.match_rows(["1111111", "2222222"], rows)
        self.assertEqual(matched["1111111"]["POD Status"], "Not Received")
        self.assertEqual(matched["2222222"]["POD Status"], "pickup")  # the only row: not "Not in The System"

    def test_the_main_table_pod_status_is_read_directly_and_pickup_rows_come_back(self):
        settings = {
            "POD_PBI_TABLE": "Main",
            "POD_PBI_CONNOTE_COLUMN": "Connote",
            "POD_PBI_REFERENCE_COLUMN": "Reference",
            "POD_PBI_LINKS": "",
            "POD_PBI_COLUMNS": "POD Status=Main[POD Status];Receiver=Main[Receiver Name]",
            "POD_PBI_STATUS_COLUMN": "POD Status",
            "POD_PBI_SECOND_TABLE": "",
        }
        with mock.patch.dict(os.environ, settings):
            query = handoff.build_pbi_query(["5869657"])
        self.assertIn("\"POD Status\", 'Main'[POD Status]", query)
        self.assertIn("'Main'[Connote] & \"\" IN {\"005869657\", \"05869657\", \"5869657\"}", query)
        # A reference is searched as written: a padded one contains it.
        self.assertIn("CONTAINSSTRING('Main'[Reference] & \"\", \"5869657\")", query)
        self.assertNotIn("CONTAINSSTRING('Main'[Reference] & \"\", \"005869657\")", query)
        self.assertNotIn("PickUp", query)  # chosen in match_rows, so a PickUp-only connote is still found
        self.assertNotIn("'POD Scans'[POD Status]", query)

    def test_second_table_rows_are_added_with_the_same_columns(self):
        settings = {
            "POD_PBI_TABLE": "TMS",
            "POD_PBI_LINKS": "POD Scans[Con Note]=TMS[Connote]",
            "POD_PBI_COLUMNS": "Sender=TMS[Sender];POD Status=POD Scans[Status]",
            "POD_PBI_SECOND_TABLE": "DeliveryApp",
            "POD_PBI_SECOND_CONNOTE_COLUMN": "Consignment",
            "POD_PBI_SECOND_REFERENCE_COLUMN": "Order",
            "POD_PBI_SECOND_COLUMNS": "Sender=Shipper",
        }
        with mock.patch.dict(os.environ, settings):
            query = handoff.build_pbi_query(["1234567"])
        self.assertTrue(query.startswith("EVALUATE\nUNION(\nSELECTCOLUMNS("))
        self.assertIn("'DeliveryApp'[Consignment] & \"\" IN {\"001234567\", \"01234567\", \"1234567\"}", query)
        self.assertIn("CONTAINSSTRING('DeliveryApp'[Order] & \"\", \"1234567\")", query)
        delivery_app = query[query.index("'DeliveryApp'[Consignment] & \"\" IN"):]
        # Same names in the same order; a name DeliveryApp leaves out is empty.
        self.assertIn('"Connote", \'DeliveryApp\'[Consignment]', delivery_app)
        self.assertIn('"Reference", \'DeliveryApp\'[Order]', delivery_app)
        self.assertIn('"Sender", \'DeliveryApp\'[Shipper]', delivery_app)
        self.assertIn('"POD Status", BLANK()', delivery_app)

    def test_second_columns_accept_table_qualified_names(self):
        settings = {
            "POD_PBI_TABLE": "TMS",
            "POD_PBI_LINKS": "",
            "POD_PBI_COLUMNS": (
                "Receiver=TMS[Receiver];Date In TMS=TMS[Date];POD Status=TMS[Status]"
            ),
            "POD_PBI_SECOND_TABLE": "DeliveryApp",
            "POD_PBI_SECOND_CONNOTE_COLUMN": "Consignment",
            "POD_PBI_SECOND_REFERENCE_COLUMN": "Order",
            "POD_PBI_SECOND_COLUMNS": (
                "Receiver=DELIVERYAPP[deliver_to_collect_from];"
                "Date In TMS=DELIVERYAPP[date];POD Status=DELIVERYAPP[Status]"
            ),
        }
        with mock.patch.dict(os.environ, settings):
            query = handoff.build_pbi_query(["1234567"])
        delivery_app = query[query.index("'DeliveryApp'[Consignment] & \"\" IN"):]
        self.assertIn('"Receiver", \'DeliveryApp\'[deliver_to_collect_from]', delivery_app)
        self.assertIn('"Date In TMS", \'DeliveryApp\'[date]', delivery_app)
        self.assertIn('"POD Status", \'DeliveryApp\'[Status]', delivery_app)
        self.assertNotIn("'DeliveryApp'[DELIVERYAPP[", delivery_app)

    def test_second_table_without_reference_column_searches_by_connote(self):
        settings = {
            "POD_PBI_TABLE": "TMS",
            "POD_PBI_LINKS": "",
            "POD_PBI_COLUMNS": "Receiver=TMS[Receiver]",
            "POD_PBI_SECOND_TABLE": "DeliveryApp",
            "POD_PBI_SECOND_CONNOTE_COLUMN": "Consignment",
            "POD_PBI_SECOND_REFERENCE_COLUMN": "",
            "POD_PBI_SECOND_COLUMNS": "Receiver=DELIVERYAPP[deliver_to_collect_from]",
        }
        with mock.patch.dict(os.environ, settings):
            query = handoff.build_pbi_query(["1234567"])
        delivery_app = query[query.index("'DeliveryApp'[Consignment] & \"\" IN"):]
        self.assertIn("'DeliveryApp'[Consignment] & \"\" IN {\"001234567\", \"01234567\", \"1234567\"}", delivery_app)
        self.assertNotIn("CONTAINSSTRING('DeliveryApp'", delivery_app)
        self.assertIn('"Reference", BLANK()', delivery_app)

    def test_wrong_second_settings_are_refused_naming_the_setting(self):
        base = {
            "POD_PBI_TABLE": "TMS", "POD_PBI_LINKS": "", "POD_PBI_COLUMNS": "Sender=TMS[Sender]",
            "POD_PBI_SECOND_TABLE": "DeliveryApp", "POD_PBI_SECOND_CONNOTE_COLUMN": "C", "POD_PBI_SECOND_REFERENCE_COLUMN": "R",
        }
        cases = [
            ({"POD_PBI_SECOND_COLUMNS": "Receiver=To"}, "POD_PBI_SECOND_COLUMNS"),   # not a POD_PBI_COLUMNS name
            ({"POD_PBI_SECOND_COLUMNS": "Sender"}, "POD_PBI_SECOND_COLUMNS"),        # no "="
            ({"POD_PBI_SECOND_COLUMNS": "Sender=Other[Shipper]"}, "POD_PBI_SECOND_COLUMNS"),
            ({"POD_PBI_COLUMNS": ""}, "POD_PBI_COLUMNS"),
        ]
        for extra, setting in cases:
            with mock.patch.dict(os.environ, {**base, "POD_PBI_SECOND_COLUMNS": "", **extra}):
                with self.assertRaisesRegex(config.ConfigError, setting):
                    handoff.build_pbi_query(["1"])

    def test_wrong_links_and_columns_are_refused_naming_the_setting(self):
        cases = {
            "POD_PBI_LINKS": "POD Scans[Connote]",                      # no "="
            "POD_PBI_COLUMNS": "Status=POD Scans[Status]",              # table not linked
        }
        for setting, value in cases.items():
            with mock.patch.dict(os.environ, {"POD_PBI_TABLE": "TMS", "POD_PBI_LINKS": "", setting: value}):
                with self.assertRaisesRegex(config.ConfigError, setting):
                    handoff.build_pbi_query(["1"])
        with mock.patch.dict(os.environ, {"POD_PBI_TABLE": "TMS", "POD_PBI_LINKS": "A[x]=B[y]"}):
            with self.assertRaisesRegex(config.ConfigError, "must link another table to TMS"):
                handoff.build_pbi_query(["1"])

    def test_names_left_as_placeholders_are_refused(self):
        cases = {
            "POD_PBI_CONNOTE_COLUMN": "<connote column in TMS>",
            "POD_PBI_COLUMNS": "Sender=TMS[<sender column>]",
            "POD_PBI_SECOND_COLUMNS": "Sender=<sender column in DeliveryApp>",
            "POD_PBI_SECOND_REFERENCE_COLUMN": "<reference column in DeliveryApp>",
        }
        base = {
            "POD_PBI_TABLE": "TMS", "POD_PBI_LINKS": "", "POD_PBI_COLUMNS": "Sender=TMS[Sender]",
            "POD_PBI_SECOND_TABLE": "DeliveryApp", "POD_PBI_SECOND_CONNOTE_COLUMN": "C",
            "POD_PBI_SECOND_REFERENCE_COLUMN": "R", "POD_PBI_SECOND_COLUMNS": "",
        }
        for setting, value in cases.items():
            with mock.patch.dict(os.environ, {**base, setting: value}):
                with self.assertRaisesRegex(config.ConfigError, f"{setting}.*placeholder"):
                    handoff.build_pbi_query(["1"])

    def test_without_extra_columns_the_main_table_rows_come_back(self):
        with mock.patch.dict(os.environ, {"POD_PBI_COLUMNS": "", "POD_PBI_LINKS": ""}):
            query = handoff.build_pbi_query(["1234567"])
        self.assertTrue(query.startswith("EVALUATE\nFILTER("))

    def test_missing_setting_is_named(self):
        with mock.patch.dict(os.environ, {"POD_PBI_TABLE": ""}):
            with self.assertRaisesRegex(config.ConfigError, "POD_PBI_TABLE"):
                handoff.build_pbi_query(["1234567"])

    def test_request_file_is_complete_json_and_no_temp_file_is_left(self):
        handoff.write_pbi_request("abc", "ENTRY", ["1234567"], ["1234567"])
        data = json.loads((self.dir / "pbi_requests" / "abc.json").read_text(encoding="utf-8"))
        self.assertEqual(data["numbers"], ["1234567"])
        self.assertEqual(data["scan_missing_numbers"], ["1234567"])
        self.assertIn("EVALUATE", data["query"])
        self.assertEqual(list((self.dir / "_tmp").iterdir()), [])

    def test_result_waits_then_strips_table_names_from_columns(self):
        self.assertIsNone(handoff.read_pbi_result("abc"))
        (self.dir / "pbi_results").mkdir()
        (self.dir / "pbi_results" / "abc.json").write_text(
            '﻿{"rows": [{"POD Data[Connote]": "1234567", "[POD Status]": "Not Received"}]}',
            encoding="utf-8",
        )
        self.assertEqual(
            handoff.read_pbi_result("abc"),
            {"rows": [{"Connote": "1234567", "POD Status": "Not Received"}]},
        )

    def test_result_error_is_reported(self):
        (self.dir / "pbi_results").mkdir()
        (self.dir / "pbi_results" / "abc.json").write_text('{"error": "timeout"}', encoding="utf-8")
        self.assertEqual(handoff.read_pbi_result("abc"), {"error": "timeout"})

    def test_rows_match_by_connote_or_one_of_several_references(self):
        rows = [
            {"Connote": "1234567", "Reference": ""},
            {"Connote": "7777777", "Reference": "PO1, SH-99"},
        ]
        matched = handoff.match_rows(["1234567", "SH99", "PO", "5555555"], rows)
        self.assertEqual(matched["1234567"], rows[0])
        self.assertEqual(matched["SH99"], rows[1])
        self.assertIsNone(matched["PO"])  # "PO" is only part of "PO1"
        self.assertIsNone(matched["5555555"])

    def test_outstanding_row_follows_the_column_map(self):
        row = handoff.outstanding_row(
            "SH99",
            {"Connote": "7777777", "Reference": "SH-99", "POD Status": "Missing POD", "Customer": "EXAMPLE1"},
            {"sender_email": "ops@example.com"},
            "2026-09-29T00:00:00+00:00",
        )
        self.assertEqual(
            row,
            {
                "Connote No": "7777777",
                "Ref": "SH-99",
                "Status": "Missing POD",
                "Requested By": "ops@example.com",
                "Customer": "EXAMPLE1",
                "Asked": "SH99",
            },
        )

    def test_number_not_in_power_bi_gets_its_own_number_and_status(self):
        row = handoff.outstanding_row("SH99", None, {"sender_email": "ops@example.com"}, "2026-10-02")
        self.assertEqual(row["Connote No"], "SH99")
        self.assertEqual(row["Status"], "Not in The System")
        self.assertEqual(row["Ref"], "")
        self.assertEqual(row["Customer"], "")  # no Power BI details exist
        self.assertEqual(row["Asked"], "SH99")

    def test_unknown_column_source_is_refused(self):
        with mock.patch.dict(os.environ, {"POD_OUTSTANDING_COLUMNS": "A=nonsense"}):
            with self.assertRaisesRegex(config.ConfigError, "nonsense"):
                handoff.outstanding_row("1", {}, {}, "")

    def test_bad_actions_and_kinds_are_refused(self):
        with self.assertRaises(ValueError):
            handoff.write_outstanding("abc", "update", "1")
        with self.assertRaises(ValueError):
            handoff.write_teams("abc", "email", "hi")

    def test_outstanding_add_is_complete_only_after_every_file_is_archived(self):
        first = handoff.write_outstanding("abc", "add", "1234567")
        second = handoff.write_outstanding("abc", "add", "SH-99")
        self.assertFalse(handoff.outstanding_adds_archived("abc", ["1234567", "SH-99"]))

        archive = self.dir / "archive"
        archive.mkdir()
        first.rename(archive / first.name)
        self.assertFalse(handoff.outstanding_adds_archived("abc", ["1234567", "SH-99"]))

        second.rename(archive / second.name)
        self.assertTrue(handoff.outstanding_adds_archived("abc", ["1234567", "SH-99"]))

    def test_a_half_synced_archive_file_is_not_confirmed_yet_and_does_not_fail(self):
        archive = self.dir / "archive"
        archive.mkdir()
        (archive / "abc_add_1234567.json").write_text('{"request_id": "abc", "act', encoding="utf-8")
        self.assertFalse(handoff.outstanding_adds_archived("abc", ["1234567"]))

    def test_choice_message_carries_a_ready_adaptive_card(self):
        file_path = self.dir / "POD file.pdf"
        options = [
            {
                "id": "file:0",
                "label": f"a.pdf | {file_path}",
            },
            {"id": "manual", "label": "Handle manually"},
        ]
        handoff.write_teams("abc-0", "choice", "Which file?", options)
        data = json.loads((self.dir / "teams" / "abc-0.json").read_text(encoding="utf-8"))
        card = data["card"]
        self.assertEqual(card["type"], "AdaptiveCard")
        choice_set = card["body"][1]
        self.assertEqual(choice_set["id"], "choice")
        self.assertEqual(
            choice_set["choices"],
            [
                {"title": f"a.pdf | {file_path}", "value": "file:0"},
                {"title": "Handle manually", "value": "manual"},
            ],
        )
        self.assertEqual(card["actions"][0], {"type": "Action.Submit", "title": "Submit"})

    def test_a_card_opens_the_pod_mailbox_in_outlook_on_the_web(self):
        # D-061: no Microsoft Graph, so the button opens the POD mailbox, where the email is.
        with mock.patch.object(config, "POD_SHARED_MAILBOX_ADDRESS", "pod@example.com"):
            handoff.write_teams("abc-1", "choice", "Which file?", [{"id": "manual", "label": "Handle manually"}])
        data = json.loads((self.dir / "teams" / "abc-1.json").read_text(encoding="utf-8"))
        self.assertEqual(data["card"]["actions"][-1], {"type": "Action.OpenUrl", "title": "Open the POD mailbox",
                                                       "url": "https://outlook.office.com/mail/pod@example.com/"})
        self.assertNotIn("open_email", data)

    def test_info_message_has_no_card(self):
        handoff.write_teams("abc", "info", "Held")
        data = json.loads((self.dir / "teams" / "abc.json").read_text(encoding="utf-8"))
        self.assertNotIn("card", data)

    def test_a_teams_request_is_pending_until_archived_and_its_result_stays(self):
        handoff.write_teams("old", "info", "Old")
        result = handoff._folder("teams_results") / "old.json"
        result.write_text('{"error": "BadRequest"}', encoding="utf-8")
        self.assertTrue(handoff.teams_request_pending("old"))

        handoff.archive("old.json", ("teams",))
        self.assertFalse(handoff.teams_request_pending("old"))
        self.assertTrue((self.dir / "archive" / "teams__old.json").exists())
        self.assertTrue(result.exists())
        self.assertEqual(handoff.read_teams_result("old")["error"], "BadRequest")

    def test_consumed_files_are_archived_not_deleted(self):
        handoff.write_pbi_request("abc", "ENTRY", ["1234567"], ["1234567"])
        handoff.archive("abc.json")
        self.assertFalse((self.dir / "pbi_requests" / "abc.json").exists())
        self.assertTrue((self.dir / "archive" / "pbi_requests__abc.json").exists())
        # The same email processed again: the earlier archive is kept, not overwritten.
        handoff.write_pbi_request("abc", "ENTRY", ["7654321"], ["7654321"])
        handoff.archive("abc.json")
        self.assertIn("1234567", (self.dir / "archive" / "pbi_requests__abc.json").read_text(encoding="utf-8"))
        self.assertIn("7654321", (self.dir / "archive" / "pbi_requests__abc__1.json").read_text(encoding="utf-8"))

    def test_request_id_is_short_and_stable(self):
        self.assertEqual(handoff.request_id("X" * 140), handoff.request_id("X" * 140))
        self.assertEqual(len(handoff.request_id("X" * 140)), 16)


if __name__ == "__main__":
    unittest.main()
