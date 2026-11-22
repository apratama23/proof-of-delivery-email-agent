"""The per-email workflow, end to end, with a fake Outlook and temp folders."""

import json
from datetime import datetime, timedelta, timezone
import os
import shutil
import stat
import tempfile
import threading
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pandas as pd

from app import (
    attachments, classifier, config, handoff, main, model_registry, number_classifier, outlook_reader, pod_files,
    pilot, reply_generator, tracking,
)

PBI_ENV = {
    "POD_PBI_TABLE": "POD Data",
    "POD_PBI_CONNOTE_COLUMN": "Connote",
    "POD_PBI_REFERENCE_COLUMN": "Reference",
    "POD_PBI_STATUS_COLUMN": "POD Status",
    "POD_OUTSTANDING_COLUMNS": "Connote=connote;Status=pod_status;Requested By=sender_email",
    "POD_SENDER_FOLDERS": "",
    "POD_SIGNATURE_FILE": "",
    # Blank the optional ones too, so values in a local .env cannot leak in.
    **{name: "" for name in ("POD_MANUAL_UPLOAD_DIR", "POD_MANUAL_UPLOAD_URL",
                             "POD_PBI_LINKS", "POD_PBI_COLUMNS", "POD_PBI_SECOND_TABLE",
                             "POD_PBI_SECOND_CONNOTE_COLUMN", "POD_PBI_SECOND_REFERENCE_COLUMN", "POD_PBI_SECOND_COLUMNS",
                             "POD_PBI_SENDER_CHECK")},
}


class RefusingModel:
    """An intent model that fails the test if it is ever consulted."""

    def predict(self, texts):
        raise AssertionError("intent model must not run")

    predict_proba = predict


class FixedIntentModel:
    """An intent model that always answers the same label and confidence."""

    def __init__(self, label: str = "POD_REQUEST", confidence: float = 0.97):
        self.label = label
        self.confidence = confidence

    def predict(self, texts):
        return [self.label for _ in texts]

    def predict_proba(self, texts):
        return [[1 - self.confidence, self.confidence] for _ in texts]


def _email(body: str, subject: str = "POD request", **extra) -> dict:
    return {
        "entry_id": "E1",
        "message_class": "IPM.Note",
        "received_time": "2026-09-29 08:00",
        "sender_email": "customer@example.com",
        "subject": subject,
        "body": body,
        **extra,
    }


class ResetLocalStateTest(unittest.TestCase):
    def test_reset_backs_up_and_clears_only_local_run_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tracking_db = root / "data" / "tracking.db"
            processed_ids = root / "data" / "processed_ids.txt"
            training_data = root / "data" / "training_data.xlsx"
            tracking_db.parent.mkdir()
            tracking_db.write_bytes(b"tracking")
            processed_ids.write_text("OUTLOOK-ID\n", encoding="utf-8")
            training_data.write_bytes(b"training")

            with (
                mock.patch.object(config, "TRACKING_DB_PATH", tracking_db),
                mock.patch.object(config, "PROCESSED_IDS_PATH", processed_ids),
                mock.patch.object(config, "LOCK_FILE", root / "data" / "run.lock"),
            ):
                backup_dir = main.reset_local_state(progress=False)

            self.assertFalse(tracking_db.exists())
            self.assertFalse(processed_ids.exists())
            self.assertEqual((backup_dir / "tracking.db").read_bytes(), b"tracking")
            self.assertEqual(
                (backup_dir / "processed_ids.txt").read_text(encoding="utf-8"),
                "OUTLOOK-ID\n",
            )
            self.assertEqual(training_data.read_bytes(), b"training")

    def test_reset_option_exits_without_running_a_cycle(self):
        with mock.patch.object(main, "reset_local_state") as reset, mock.patch.object(
            main, "run"
        ) as run:
            main.main(["--reset"])

        reset.assert_called_once_with()
        run.assert_not_called()

    def test_process_new_option_does_not_resume_pending_work(self):
        with mock.patch.object(main, "run") as run:
            main.main(["--process-new"])

        run.assert_called_once_with(progress=True, process_new=True, resume=False)

    def test_resume_option_does_not_fetch_new_emails(self):
        with mock.patch.object(main, "run") as run:
            main.main(["--resume"])

        run.assert_called_once_with(progress=True, process_new=False, resume=True)

    def test_no_option_keeps_the_combined_behavior(self):
        with mock.patch.object(main, "run") as run:
            main.main([])

        run.assert_called_once_with(progress=True, process_new=True, resume=True)

    def test_cli_modes_are_mutually_exclusive(self):
        with (
            mock.patch.object(main, "run") as run,
            redirect_stderr(StringIO()),
            self.assertRaises(SystemExit),
        ):
            main.main(["--process-new", "--resume"])

        run.assert_not_called()


# A customer whose Power BI rows give the path of its signed scan (D-043).
SCAN_PATH_ENV = {
    **PBI_ENV,
    "POD_PBI_LINKS": "Scans[Key]=POD Data[Key]",
    "POD_PBI_COLUMNS": "Sender=POD Data[Sender];POD Status=POD Data[POD Status];Scan Path=Scans[Path]",
    "POD_PBI_SENDER_COLUMN": "Sender",
    "POD_PBI_SCAN_PATH_COLUMN": "Scan Path",
}

class FlowTest(unittest.TestCase):
    """Shared set-up: a scan folder, handoff folder, tracking DB, fake Outlook."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.scan = tmp / "scans" / "2026"
        for name in ("1234567_a.pdf", "2345678.pdf", "555555 - Signed.pdf", "555555 - Signed (2).pdf"):
            (self.scan / "Sep").mkdir(parents=True, exist_ok=True)
            (self.scan / "Sep" / name).write_bytes(b"%PDF-1.4 test")
        self.handoff_dir = tmp / "handoff"
        for patcher in (
            mock.patch.object(config, "TRACKING_DB_PATH", tmp / "tracking.db"),
            mock.patch.object(config, "HANDOFF_DIR", self.handoff_dir),
            mock.patch.object(config, "TRAINING_DATA_PATH", tmp / "training_data.xlsx"),
            mock.patch.object(config, "SCAN_FOLDERS", [self.scan]),
            mock.patch.object(config, "SEND_MODE", "draft"),
            mock.patch.object(config, "CONFIDENCE_THRESHOLD", 0.90),
            mock.patch.object(config, "MAX_ATTACHMENT_MB", 20.0),
            mock.patch.object(config, "POD_SHARED_MAILBOX_ADDRESS", "pod@example.com"),
            mock.patch.dict(os.environ, PBI_ENV),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.reply = mock.patch.object(
            outlook_reader, "reply", return_value={"draft_entry_id": "D1", "conversation_id": "C1"}
        ).start()
        self.mark_read = mock.patch.object(outlook_reader, "mark_read").start()
        self.manual_reply = mock.patch.object(outlook_reader, "replied_manually", return_value=None).start()
        # The POD mailbox folder, and where each waiting email is found in it (D-041).
        self.inbox = SimpleNamespace(FolderPath="\\\\POD\\Inbox", Name="Inbox", Store=SimpleNamespace(StoreID="POD_STORE"))
        self.email_status = mock.patch.object(
            outlook_reader, "email_status", return_value={"folder": self.inbox, "manual": None}
        ).start()
        self.in_finished = mock.patch.object(outlook_reader, "in_child_folder", return_value=False).start()
        # No attachment numbers unless a test gives some (D-044).
        self.attachments = mock.patch.object(attachments, "read", return_value={"sources": [], "not_read": []}).start()
        mock.patch.object(outlook_reader, "mailbox_store_id", return_value="POD_STORE").start()
        self.addCleanup(mock.patch.stopall)
        self.counters = Counter()

    def new_email(self, email, model=None, cold_start=False):
        main._process_new_email(
            email,
            model or FixedIntentModel(),
            None,
            cold_start,
            lambda: pod_files.build_index([self.scan]),
            self.counters,
        )

    def current(self) -> dict:
        return tracking.latest()[-1]

    def rid(self) -> str:
        return handoff.request_id("E1")

    def power_bi_answers(self, data: dict):
        (self.handoff_dir / "pbi_results").mkdir(parents=True, exist_ok=True)
        # Flow 1 names the result after the current request's id.
        request = tracking.latest(tracking.WAITING_PBI)[0]["state"]["pbi_request"]
        (self.handoff_dir / "pbi_results" / f"{request}.json").write_text(json.dumps(data), encoding="utf-8")
        main._resume_pbi(
            tracking.latest(tracking.WAITING_PBI)[0],
            lambda: pod_files.build_index([self.scan]),
            self.counters,
        )

    def to_the_team(self, email):
        """An email whose number matches several files: Power BI is asked first and names none (D-044)."""
        self.new_email(email)
        self.power_bi_answers({"rows": []})

    def team_answers(self, card_id: str, choice: str):
        (self.handoff_dir / "teams_results").mkdir(parents=True, exist_ok=True)
        (self.handoff_dir / "teams_results" / f"{card_id}.json").write_text(
            json.dumps({"choice": choice, "responder": "Ana"}), encoding="utf-8"
        )
        main._resume_team(tracking.latest(tracking.WAITING_TEAM)[0], self.counters)

    def handoff_files(self, folder: str) -> list[dict]:
        path = self.handoff_dir / folder
        if not path.exists():
            return []
        return [json.loads(file.read_text(encoding="utf-8")) for file in sorted(path.glob("*.json"))]


class OrderOfChecksTest(FlowTest):
    """Guard, then intent, then numbers: in the text, else in the attachments (D-002, D-044)."""

    def test_not_a_pod_request_is_done_with_or_without_a_number(self):
        self.new_email(_email("Hi team, thanks for the update."), FixedIntentModel("OTHER"))
        self.assertEqual(self.current()["status"], tracking.SKIPPED_NO_NUMBER)
        self.new_email(_email("Invoice 1234567 is paid", entry_id="E2"), FixedIntentModel("OTHER"))
        self.assertEqual(self.current()["status"], tracking.OTHER_INTENT)
        self.attachments.assert_not_called()
        self.reply.assert_not_called()

    # Below the threshold, as a person reads it (product owner, D-044).
    def test_an_unsure_pod_request_with_its_scan_on_the_share_is_answered(self):
        self.new_email(_email("Please send POD for 1234567"), FixedIntentModel(confidence=0.60))
        self.assertEqual(self.current()["status"], tracking.REPLIED)

    def test_an_unsure_pod_request_whose_number_is_nowhere_goes_to_manual_review_not_the_card(self):
        self.new_email(_email("Please send POD for 7777777"), FixedIntentModel(confidence=0.60))
        self.power_bi_answers({"rows": []})
        self.assertEqual(self.current()["status"], tracking.MANUAL_REVIEW)
        self.assertIn("not on the share or in Power BI: 7777777", self.current()["reason"])
        # A review card, as in the dashboard (D-048), not the correction card.
        self.assertEqual([message["request_id"] for message in self.handoff_files("teams")],
                         [self.current()["state"]["review_card"]])
        self.assertNotIn("correction_card", self.current()["state"])
        self.assertEqual(self.handoff_files("outstanding"), [])

    def test_an_unsure_pod_request_with_numbers_only_in_its_attachments_goes_on(self):
        self.attachments.return_value = {"sources": [{"source": "picture", "numbers": ["1234567"]}], "not_read": []}
        self.new_email(_email("Can you check this?"), FixedIntentModel(confidence=0.60))
        self.assertEqual(self.current()["status"], tracking.REPLIED)

    def test_an_unsure_pod_request_with_no_number_anywhere_goes_to_manual_review(self):
        self.new_email(_email("Hi team, can you check?"), FixedIntentModel("POD_REQUEST", 0.6))
        self.attachments.assert_called_once()
        self.assertEqual(self.current()["status"], tracking.MANUAL_REVIEW)

    def test_an_unsure_other_email_with_a_number_goes_to_manual_review(self):
        self.new_email(_email("Invoice 1234567 attached"), FixedIntentModel("OTHER", 0.6))
        self.assertEqual(self.current()["status"], tracking.MANUAL_REVIEW)
        self.attachments.return_value = {"sources": [{"source": "PDF file name", "numbers": ["7654321"]}], "not_read": []}
        self.new_email(_email("See attached", entry_id="E2"), FixedIntentModel("OTHER", 0.6))
        self.assertEqual(self.current()["status"], tracking.MANUAL_REVIEW)
        self.reply.assert_not_called()

    def test_an_unsure_other_email_with_no_number_anywhere_is_done(self):
        self.new_email(_email("Thanks for the update"), FixedIntentModel("OTHER", 0.6))
        self.attachments.assert_called_once()
        self.assertEqual(self.current()["status"], tracking.SKIPPED_NO_NUMBER)

    def test_a_pod_request_without_any_number_goes_to_manual_review(self):
        self.new_email(_email("Please send the POD for our last delivery"))
        self.attachments.assert_called_once()
        self.assertEqual((self.current()["status"], self.current()["reason"]),
                         (tracking.MANUAL_REVIEW, "POD request without a number"))

    def test_cold_start_holds_emails_with_numbers(self):
        self.new_email(_email("Please send POD for 1234567"), RefusingModel(), cold_start=True)
        self.assertEqual(self.current()["status"], tracking.MANUAL_REVIEW)
        self.assertEqual(self.current()["numbers"], "1234567")

    def test_other_intent_is_left_for_people(self):
        self.new_email(_email("Invoice 1234567 attached"), FixedIntentModel("OTHER"))
        self.assertEqual(self.current()["status"], tracking.OTHER_INTENT)
        self.reply.assert_not_called()

    def test_forwarded_request_is_not_skipped(self):
        email = _email("Can you send the POD for these?")
        email["full_body"] = "Can you send the POD for these?\nFrom: X\nSent: Y\nTo: Z\n1234567"
        self.new_email(email, RefusingModel(), cold_start=True)
        self.assertEqual(self.current()["numbers"], "1234567")


class GuardTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(config, "POD_SHARED_MAILBOX_ADDRESS", "pod@example.com")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_automatic_senders_and_messages_are_never_answered(self):
        cases = {
            "sent by the POD mailbox": _email("x", sender_email="POD@example.com"),
            # The team's reply on behalf of the POD mailbox, back in its inbox through CC (D-055).
            "sent by the POD mailbox ": _email("x", sender_email="ana@example6.com", sent_for="pod@example.com"),
            "automatic reply": _email("x", message_class="IPM.Note.Rules.OofTemplate.Microsoft"),
            "not an ordinary email (REPORT.IPM.Note.NDR)": _email("x", message_class="REPORT.IPM.Note.NDR"),
            "no-reply sender": _email("x", sender_email="no-reply@example5.com"),
            "automatic or bounce subject": _email("x", subject="Automatic reply: POD 1234567"),
        }
        for expected, email in cases.items():
            self.assertEqual(main.skip_reason(email), expected.strip())
        with mock.patch.object(config, "POD_SHARED_MAILBOX_ADDRESS", ""):
            self.assertEqual(main.skip_reason(_email("x", sent_for="")), "")  # no address set: nothing matches

    def test_ordinary_emails_pass(self):
        self.assertEqual(main.skip_reason(_email("x")), "")
        self.assertEqual(main.skip_reason(_email("x", message_class="IPM.Note.SMIME")), "")
        self.assertEqual(main.skip_reason(_email("x", message_class="")), "")


class ReplyFlowTest(FlowTest):
    def test_a_short_list_is_answered_for_every_number(self):
        self.new_email(_email("Please send the POD for 1234567/68"))  # 1234568 has no scan
        latest = tracking.latest()[0]
        self.assertEqual(latest["numbers"], "1234567;1234568")
        self.assertEqual(latest["status"], tracking.WAITING_PBI)  # 1234568 goes to Power BI, not dropped

    def test_a_list_that_cannot_be_read_is_held_not_answered_in_part(self):
        self.new_email(_email("Please send the POD for 1234567-999"))
        latest = tracking.latest()[0]
        self.assertEqual(latest["status"], tracking.HELD)
        self.assertEqual(latest["reason"], "number list could not be read")
        self.reply.assert_not_called()
        message = json.loads(next((self.handoff_dir / "teams").glob("*.json")).read_text(encoding="utf-8"))
        self.assertIn("1234567-999", message["message"])
        self.assertEqual(message["kind"], "choice")  # the team can type the numbers in Teams (D-036)

    def test_the_automated_notice_is_only_in_sent_replies_not_in_drafts(self):
        notice = main.reply_generator.AUTOMATED_NOTICE
        self.new_email(_email("Please send the POD for 1234567"))  # FlowTest runs in draft mode
        self.assertNotIn(notice, self.reply.call_args.args[1])
        with mock.patch.object(config, "SEND_MODE", "send"):
            self.new_email(_email("Please send the POD for 1234567", entry_id="E2"))
        self.assertIn(notice, self.reply.call_args.args[1])
        self.assertEqual(self.reply.call_args.args[3], "send")

    def test_reprocessing_waiting_power_bi_archives_old_files_and_queries_only_scan_missing(self):
        self.new_email(_email("Please send POD for 1234567 and 7777777"))
        event = self.current()
        first = event["state"]["pbi_request"]
        result_path = self.handoff_dir / "pbi_results" / f"{first}.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text('{"rows": []}', encoding="utf-8")

        self.assertEqual(main.reprocess_power_bi([event]), 1)

        request = self.handoff_files("pbi_requests")[0]
        self.assertNotEqual(request["request_id"], first)  # a new request, so Flow 1 runs again
        self.assertEqual(request["request_id"], self.current()["state"]["pbi_request"])
        self.assertEqual(request["numbers"], ["1234567", "7777777"])
        self.assertEqual(request["scan_missing_numbers"], ["7777777"])
        self.assertNotIn('"1234567"', request["query"])
        self.assertIn('"7777777"', request["query"])
        self.assertEqual(self.handoff_files("pbi_results"), [])
        archived = list((self.handoff_dir / "archive").glob("*.json"))
        self.assertEqual(len(archived), 2)
        self.assertTrue(any(path.name.startswith("pbi_requests__") for path in archived))
        self.assertTrue(any(path.name.startswith("pbi_results__") for path in archived))
        latest = self.current()
        self.assertEqual(latest["status"], tracking.WAITING_PBI)
        self.assertEqual(latest["reason"], "Power BI lookup reprocessed from the dashboard")

    def test_an_email_waiting_from_before_requests_were_numbered_still_gets_its_answer(self):
        tracking.record(_email("Please send POD for 7777777"), tracking.WAITING_PBI, numbers=["7777777"],
                        state={"found": {}, "ambiguous": {}, "missing": ["7777777"]})  # no pbi_request
        results = self.handoff_dir / "pbi_results"
        results.mkdir(parents=True, exist_ok=True)
        (results / f"{self.rid()}.json").write_text(
            json.dumps({"rows": [{"Connote": "7777777", "Reference": "", "POD Status": "Not Received"}]}), encoding="utf-8")
        main._resume_pbi(self.current(), lambda: pod_files.build_index([self.scan]), self.counters)
        self.assertEqual(self.current()["status"], tracking.REPLIED)

    def test_a_late_answer_to_an_earlier_power_bi_request_is_never_read(self):
        self.new_email(_email("Please send POD for 7777777"))
        first = self.current()["state"]["pbi_request"]
        main.reprocess_power_bi([self.current()])
        # Flow 1's run for the first request finishes late, with the old answer.
        results = self.handoff_dir / "pbi_results"
        results.mkdir(parents=True, exist_ok=True)
        (results / f"{first}.json").write_text(json.dumps({"rows": []}), encoding="utf-8")
        main._resume_pbi(self.current(), lambda: pod_files.build_index([self.scan]), self.counters)
        self.assertEqual(self.current()["status"], tracking.WAITING_PBI)  # still waiting for its own answer
        # A second in the future, so a file written just now counts as left long
        # enough even on a coarse clock (Windows CI with Python 3.10).
        with mock.patch.object(main, "CARD_RESEND_AFTER", timedelta(seconds=-1)):
            main._sweep_handoff(self.counters)
        self.assertFalse((results / f"{first}.json").exists())  # archived, never read

        self.power_bi_answers({"rows": [{"Connote": "7777777", "Reference": "", "POD Status": "Not Received"}]})
        self.assertEqual(self.current()["status"], tracking.REPLIED)
        self.assertIn("not received the Proof of Delivery for 7777777", self.reply.call_args.args[1])

    def test_all_found_on_the_share_is_answered_with_every_file(self):
        self.new_email(_email("Please send POD for 1234567 and 2345678"))

        self.assertEqual(self.current()["status"], tracking.REPLIED)
        self.assertEqual(self.current()["reason"], "draft")
        entry_id, body, attachments, mode = self.reply.call_args.args
        self.assertEqual((entry_id, mode), ("E1", "draft"))
        self.assertEqual(sorted(path.name for path in attachments), ["1234567_a.pdf", "2345678.pdf"])
        self.assertIn("Hi,", body)
        self.assertIn("1234567 and 2345678", body)
        self.assertNotIn(reply_generator.AUTOMATED_NOTICE, body)
        self.assertEqual(self.reply.call_args.kwargs["send_as"], "pod@example.com")
        # Opened, and its draft kept, in the POD mailbox, never by entry ID alone (D-032).
        self.assertEqual(self.reply.call_args.kwargs["store_id"], "POD_STORE")
        self.mark_read.assert_called_once_with("E1", "POD_STORE")
        removals = self.handoff_files("outstanding")
        self.assertEqual({(item["action"], item["number"]) for item in removals}, {("remove", "1234567"), ("remove", "2345678")})
        self.assertEqual(self.handoff_files("pbi_requests"), [])

    def test_mixed_found_and_not_received_gives_one_reply_and_adds_to_outstanding(self):
        self.new_email(_email("Please send POD for 1234567 and 7777777"))
        self.assertEqual(self.current()["status"], tracking.WAITING_PBI)
        request = self.handoff_files("pbi_requests")[0]
        self.assertEqual(request["numbers"], ["1234567", "7777777"])
        self.assertEqual(request["scan_missing_numbers"], ["7777777"])
        self.assertNotIn('"1234567"', request["query"])
        self.assertIn('"7777777"', request["query"])
        self.reply.assert_not_called()

        self.power_bi_answers({"rows": [{"POD Data[Connote]": "7777777", "POD Data[Reference]": "", "POD Data[POD Status]": "Not Received"}]})

        self.assertEqual(self.current()["status"], tracking.REPLIED)
        body = self.reply.call_args.args[1]
        self.assertIn("Proof of Delivery for 1234567.", body)
        self.assertIn("not received the Proof of Delivery for 7777777", body)
        added = [item for item in self.handoff_files("outstanding") if item["action"] == "add"]
        self.assertEqual(added[0]["row"], {"Connote": "7777777", "Status": "Not Received", "Requested By": "customer@example.com"})
        # The Power BI request and result are archived, not deleted.
        self.assertEqual(self.handoff_files("pbi_requests"), [])
        self.assertEqual(len(list((self.handoff_dir / "archive").glob("*.json"))), 2)

    def test_reference_match_uses_its_connote_to_find_and_attach_scan(self):
        connote_scan = self.scan / "Sep" / "12345678.pdf"
        connote_scan.write_bytes(b"%PDF-1.4 test")
        self.new_email(_email("Please send POD for 80655501"))
        request = self.handoff_files("pbi_requests")[0]
        self.assertIn('"0080655501"', request["query"])

        self.power_bi_answers({
            "rows": [{
                "Connote": "12345678",
                "Reference": "0080655501",
                "POD Status": "Not Received",
            }]
        })

        self.assertEqual(self.current()["status"], tracking.REPLIED)
        self.assertIn("Proof of Delivery for 80655501.", self.reply.call_args.args[1])
        self.assertEqual([path.name for path in self.reply.call_args.args[2]], ["12345678.pdf"])
        self.assertFalse(any(item["action"] == "add" for item in self.handoff_files("outstanding")))
        self.assertTrue(any(item["action"] == "remove" and item["number"] == "80655501"
                            for item in self.handoff_files("outstanding")))

    def test_a_signed_scan_path_from_power_bi_is_used_for_the_matching_reference(self):
        scan = self.scan / "POD" / "Receiver" / "524037 - Signed.pdf"
        scan.parent.mkdir(parents=True)
        scan.write_bytes(b"%PDF-1.4 test")
        scan_path_settings = SCAN_PATH_ENV
        with mock.patch.dict(os.environ, scan_path_settings):
            self.new_email(_email("Please send POD for 80655501"))
            self.power_bi_answers({
                "rows": [{
                    "Connote": "12345678",
                    "Reference": "0080655501",
                    "Sender": "Example1 Company",
                    "POD Status": "Signed",
                    "Scan Path": scan.as_uri(),
                }]
            })

        self.assertEqual(self.current()["status"], tracking.REPLIED)
        self.assertEqual([path.name for path in self.reply.call_args.args[2]], ["524037 - Signed.pdf"])
        self.assertEqual(self.current()["state"]["found"]["80655501"], str(scan))

    def test_a_reference_of_several_consignments_with_scans_goes_to_the_team(self):
        scans = [
            self.scan / "POD" / "a" / "524037 - Signed.pdf",
            self.scan / "POD" / "b" / "524037 - Delivered.pdf",
        ]
        for scan in scans:
            scan.parent.mkdir(parents=True)
            scan.write_bytes(b"%PDF-1.4 test")
        scan_path_settings = SCAN_PATH_ENV
        with mock.patch.dict(os.environ, scan_path_settings):
            self.new_email(_email("Please send POD for 80655501"))
            self.power_bi_answers({
                "rows": [
                    {
                        "Connote": "12345678",
                        "Reference": "0080655501",
                        "Sender": "Example1 Company",
                        "POD Status": "Signed",
                        "Scan Path": scans[0].as_uri(),
                    },
                    {
                        "Connote": "87654321",
                        "Reference": "0080655501",
                        "Sender": "Example1 Company",
                        "POD Status": "Delivered",
                        "Scan Path": scans[1].as_uri(),
                    },
                ]
            })

        self.assertEqual(self.current()["status"], tracking.WAITING_TEAM)
        self.assertEqual(
            [option["label"] for option in self.handoff_files("teams")[0]["options"][:2]],
            [
                f"524037 - Signed.pdf | {scans[0]}",
                f"524037 - Delivered.pdf | {scans[1]}",
            ],
        )

    def test_another_sender_uses_the_existing_connote_search(self):
        scan = self.scan / "Sep" / "12345678.pdf"
        scan.write_bytes(b"%PDF-1.4 test")
        scan_path_settings = SCAN_PATH_ENV
        with mock.patch.dict(os.environ, scan_path_settings):
            self.new_email(_email("Please send POD for 80655501"))
            self.power_bi_answers({
                "rows": [{
                    "Connote": "12345678",
                    "Reference": "0080655501",
                    "Sender": "Other sender",
                    "POD Status": "Signed",
                    "Scan Path": "",
                }]
            })

        self.assertEqual(self.current()["status"], tracking.REPLIED)
        self.assertEqual(
            [path.name for path in self.reply.call_args.args[2]],
            ["12345678.pdf"],
            self.current()["state"],
        )

    def test_number_unknown_to_power_bi_holds_the_whole_email(self):
        self.new_email(_email("Please send POD for 1234567 and 7777777"))
        self.power_bi_answers({"rows": []})

        self.assertEqual(self.current()["status"], tracking.HELD)
        self.assertIn("not in Power BI", self.current()["reason"])
        self.reply.assert_not_called()
        self.mark_read.assert_not_called()  # stays unread for the team
        message = self.handoff_files("teams")[0]
        self.assertEqual(message["kind"], "choice")  # a card to correct the numbers (D-036)
        self.assertIn("7777777", message["message"])
        self.assertIn("Not in The System", message["message"])
        # D-023: the unknown number is recorded for the team to check.
        added = self.handoff_files("outstanding")
        self.assertEqual([(item["action"], item["number"]) for item in added], [("add", "7777777")])
        self.assertEqual(
            added[0]["row"],
            {"Connote": "7777777", "Status": "Not in The System", "Requested By": "customer@example.com"},
        )

    def test_a_number_inside_a_pallet_number_is_not_its_scan(self):
        # PLT73985078J is a pallet number: 7398507 is never read in it (product owner, D-042).
        (self.scan / "Sep" / "PLT73985078J - Signed.pdf").write_bytes(b"%PDF-1.4 test")
        self.new_email(_email("Please send POD for 7398507"))
        self.assertEqual(self.current()["status"], tracking.WAITING_PBI)  # no scan: Power BI is asked
        self.assertEqual(self.current()["state"]["missing"], ["7398507"])
        self.reply.assert_not_called()

    def held_card(self):
        card = self.handoff_files("teams")[-1]
        self.assertEqual(card["request_id"], self.current()["state"]["correction_card"])
        return card

    def team_corrects(self, card_id: str, choice: str, connotes: str = ""):
        (self.handoff_dir / "teams_results").mkdir(parents=True, exist_ok=True)
        (self.handoff_dir / "teams_results" / f"{card_id}.json").write_text(
            json.dumps({"choice": choice, "responder": "Ana", "connotes": connotes}), encoding="utf-8"
        )
        main._resume_held(tracking.latest(tracking.HELD)[0], self.counters)

    def test_numbers_corrected_on_the_teams_card_are_replied_with_every_pod_found(self):
        self.new_email(_email("Please send POD for 1234567 and 7777777"))
        self.power_bi_answers({"rows": []})
        card = self.held_card()
        inputs = [item for item in card["card"]["body"] if item["type"] == "Input.Text"]
        self.assertEqual(inputs[0]["value"], "7777777")  # only the number not found is asked (D-045)
        self.team_corrects(card["request_id"], "correct", "2345678")
        self.assertEqual(self.current()["status"], tracking.REVIEWED)
        self.assertEqual(self.current()["reason"], "reviewed in Teams by Ana: POD request")
        main._resume_reviewed(self.current(), lambda: pod_files.build_index([self.scan]), self.counters)
        self.assertEqual(self.current()["status"], tracking.REPLIED)
        self.assertEqual([path.name for path in self.reply.call_args.args[2]], ["1234567_a.pdf", "2345678.pdf"])
        removed = [item["number"] for item in self.handoff_files("outstanding") if item["action"] == "remove"]
        self.assertIn("7777777", removed)
        labels = pd.read_excel(config.TRAINING_DATA_PATH, dtype=str)
        self.assertEqual((labels.at[0, "connotes_manual"], labels.at[0, "label_source"]),
                         ("1234567;2345678", "Manual review"))

    def test_a_number_the_team_keeps_and_power_bi_still_does_not_know_is_answered_not_asked_again(self):
        self.new_email(_email("Please send POD for 1234567 and 7777777"))
        self.power_bi_answers({"rows": []})
        self.team_corrects(self.held_card()["request_id"], "correct", "7777777")  # the number is right
        main._resume_reviewed(self.current(), lambda: pod_files.build_index([self.scan]), self.counters)
        self.assertEqual(self.current()["status"], tracking.WAITING_PBI)
        cards = len(self.handoff_files("teams"))
        self.power_bi_answers({"rows": []})
        self.assertEqual(self.current()["status"], tracking.REPLIED)
        self.assertEqual(len(self.handoff_files("teams")), cards)  # no new correction card
        self.assertEqual([path.name for path in self.reply.call_args.args[2]], ["1234567_a.pdf"])
        body = self.current()["state"]["reply"]
        self.assertIn("Regarding 7777777, we were unable to find it in our system.", body)
        added = [item for item in self.handoff_files("outstanding") if item["action"] == "add"]
        self.assertEqual(added[-1]["number"], "7777777")
        self.assertEqual(added[-1]["row"]["Status"], "Not in The System")
        self.assertIn("7777777", self.current()["state"]["outstanding_add_numbers"])

    def test_a_changed_number_still_unknown_is_asked_again_and_a_kept_one_is_not(self):
        self.new_email(_email("Please send POD for 1234567, 7777777 and 6666666"))
        self.power_bi_answers({"rows": []})
        card = self.held_card()
        inputs = [item for item in card["card"]["body"] if item["type"] == "Input.Text"]
        self.assertEqual(inputs[0]["value"], "7777777; 6666666")
        self.team_corrects(card["request_id"], "correct", "7777777; 8888888")  # keeps one, changes one
        main._resume_reviewed(self.current(), lambda: pod_files.build_index([self.scan]), self.counters)
        self.power_bi_answers({"rows": []})
        self.assertEqual(self.current()["status"], tracking.HELD)  # the changed number is asked again
        card = self.held_card()
        inputs = [item for item in card["card"]["body"] if item["type"] == "Input.Text"]
        self.assertEqual(inputs[0]["value"], "8888888")
        self.reply.assert_not_called()
        self.team_corrects(card["request_id"], "correct", "8888888")  # now it is confirmed
        main._resume_reviewed(self.current(), lambda: pod_files.build_index([self.scan]), self.counters)
        self.power_bi_answers({"rows": []})
        self.assertEqual(self.current()["status"], tracking.REPLIED)
        self.assertEqual([path.name for path in self.reply.call_args.args[2]], ["1234567_a.pdf"])
        self.assertIn("Regarding 7777777 and 8888888, we were unable to find them in our system.",
                      self.current()["state"]["reply"])

    def test_no_pod_found_and_the_numbers_kept_the_reply_still_says_they_are_not_in_our_system(self):
        self.new_email(_email("Please send POD for 7777777 and 8888888"))
        self.power_bi_answers({"rows": []})
        self.team_corrects(self.held_card()["request_id"], "correct", "7777777; 8888888")
        main._resume_reviewed(self.current(), lambda: pod_files.build_index([self.scan]), self.counters)
        self.power_bi_answers({"rows": []})
        self.assertEqual(self.current()["status"], tracking.REPLIED)
        self.assertEqual(self.reply.call_args.args[2], [])
        self.assertIn("Regarding 7777777 and 8888888, we were unable to find them in our system. "
                      "Could you please check the numbers", self.current()["state"]["reply"])
        self.assertNotIn("Please find attached", self.current()["state"]["reply"])

    def test_a_cleared_card_drops_the_wrong_number_and_replies_with_the_others(self):
        self.new_email(_email("Please send POD for 1234567 and 7777777"))
        self.power_bi_answers({"rows": []})
        self.team_corrects(self.held_card()["request_id"], "correct", "")  # 7777777 is not a POD number
        self.assertEqual(self.current()["state"]["review"]["connotes"], ["1234567"])
        main._resume_reviewed(self.current(), lambda: pod_files.build_index([self.scan]), self.counters)
        self.assertEqual(self.current()["status"], tracking.REPLIED)
        self.assertNotIn("7777777", self.current()["state"]["reply"])
        removed = [item["number"] for item in self.handoff_files("outstanding") if item["action"] == "remove"]
        self.assertIn("7777777", removed)

    def test_a_held_email_with_more_than_20_numbers_is_saved_and_answered(self):
        numbers = [str(3000000 + n) for n in range(22)]
        self.new_email(_email("Please send PODs for " + ", ".join(numbers)))
        self.power_bi_answers({"rows": [{"Connote": n, "Reference": "", "POD Status": "Unsigned", "Sender": "Other"}
                                        for n in numbers[1:]]})
        self.team_corrects(self.held_card()["request_id"], "correct", numbers[0])  # the unknown one is right
        self.assertEqual(self.current()["status"], tracking.REVIEWED)
        main._resume_reviewed(self.current(), lambda: pod_files.build_index([self.scan]), self.counters)
        self.power_bi_answers({"rows": [{"Connote": n, "Reference": "", "POD Status": "Unsigned", "Sender": "Other"}
                                        for n in numbers[1:]]})
        self.assertEqual(self.current()["status"], tracking.REPLIED)
        self.assertIn(f"Regarding {numbers[0]}, we were unable to find it", self.current()["state"]["reply"])

    def resume_pbi(self):
        main._resume_pbi(tracking.latest(tracking.WAITING_PBI)[0], lambda: pod_files.build_index([self.scan]),
                         self.counters)

    def test_a_power_bi_request_flow_1_never_answers_is_written_again_then_shown(self):
        self.new_email(_email("Please send POD for 7777777"))
        first = self.current()["state"]["pbi_request"]
        self.resume_pbi()  # under 30 minutes: nothing yet
        self.assertEqual((self.current()["state"]["pbi_request"], len(tracking.history("E1"))), (first, 1))
        with mock.patch.object(main, "CARD_RESEND_AFTER", timedelta(seconds=-1)):
            for resend in range(1, main.CARD_RESENDS + 1):
                self.resume_pbi()
                self.assertEqual(self.current()["state"]["card_delivery"]["power_bi"]["resends"], resend)
            requests = sorted(path.stem for path in (self.handoff_dir / "pbi_requests").glob("*.json"))
            self.assertEqual(requests, [self.current()["state"]["pbi_request"]])  # the old ones archived
            self.assertNotEqual(requests, [first])
            self.resume_pbi()  # the last one is not answered either
        self.assertEqual(tracking.card_problems(tracking.WAITING_PBI, self.current()["state"]),
                         {"power_bi": "Flow 1 has not answered the Power BI request"})
        with self.assertRaisesRegex(ValueError, "no Teams card"), \
                mock.patch.object(config, "LOCK_FILE", Path(self._tmp.name) / "run.lock"):
            main.send_cards_again([self.current()])  # not a Teams card: Reprocess in Power BI
        with mock.patch.object(config, "LOCK_FILE", Path(self._tmp.name) / "run.lock"):
            main.reprocess_power_bi([self.current()])
        self.assertEqual(tracking.card_problems(tracking.WAITING_PBI, self.current()["state"]), {})
        self.power_bi_answers({"rows": []})  # the answer to the newest request is read
        self.assertEqual(self.current()["status"], tracking.HELD)

    def test_partial_mode_sends_to_the_chosen_domains_only(self):
        # D-057: the same flow; only draft or send differs, by the sender's domain after the @.
        with mock.patch.object(config, "SEND_MODE", "partial"), \
                mock.patch.dict(os.environ, {"POD_SEND_DOMAINS": "@Example.com; example1.com"}):
            self.assertEqual(config.send_domains(), ["example.com", "example1.com"])
            self.assertEqual(config.reply_mode("ops@au.example1.com"), "draft")  # not its subdomains
            self.new_email(_email("Please send POD for 1234567", sender_email="customer@example.com"))
            self.new_email(_email("Please send POD for 2345678", entry_id="E2", sender_email="ops@example4.com"))
        sent, drafted = (call.args for call in self.reply.call_args_list)
        self.assertEqual((sent[3], drafted[3]), ("send", "draft"))
        self.assertIn(reply_generator.AUTOMATED_NOTICE, sent[1])  # a sent reply says it was automatic
        self.assertNotIn(reply_generator.AUTOMATED_NOTICE, drafted[1])
        self.assertEqual([e["reason"] for e in (tracking.history("E1")[-1], tracking.history("E2")[-1])], ["send", "draft"])

    def test_a_malformed_list_of_send_domains_is_refused(self):
        with mock.patch.dict(os.environ, {"POD_SEND_DOMAINS": "ops@example1.com"}):
            with self.assertRaisesRegex(config.ConfigError, "POD_SEND_DOMAINS"):
                config.send_domains()

    def test_one_scan_of_a_connote_and_its_reference_is_attached_once(self):
        (self.scan / "Sep" / "7000003-80600001 - Signed.pdf").write_bytes(b"%PDF-1.4 test")
        self.new_email(_email("Please send POD for 7000003, our reference 80600001"))
        self.assertEqual(self.current()["status"], tracking.REPLIED)
        self.assertEqual([path.name for path in self.reply.call_args.args[2]], ["7000003-80600001 - Signed.pdf"])

    def test_every_card_opens_the_pod_mailbox(self):
        # Correction card and file choice card (D-051, D-061).
        self.new_email(_email("Please send POD for 7777777"))
        self.power_bi_answers({"rows": []})
        self.to_the_team(_email("Please send POD for 555555", entry_id="E2"))
        for card in (self.handoff_files("teams")[0], self.handoff_files("teams")[-1]):
            self.assertEqual(card["card"]["actions"][-1]["title"], "Open the POD mailbox")
            self.assertEqual(card["card"]["actions"][-1]["url"], "https://outlook.office.com/mail/pod@example.com/")

    def test_handle_manually_on_the_teams_card_keeps_the_email_held(self):
        self.new_email(_email("Please send POD for 7777777"))
        self.power_bi_answers({"rows": []})
        self.team_corrects(self.held_card()["request_id"], "manual")
        self.assertEqual((self.current()["status"], self.current()["reason"]),
                         (tracking.HELD, "Ana chose to handle it manually"))
        self.assertNotIn("correction_card", self.current()["state"])
        self.reply.assert_not_called()

    def test_no_numbers_on_the_teams_card_posts_a_new_card(self):
        self.new_email(_email("Please send POD for 7777777"))
        self.power_bi_answers({"rows": []})
        first = self.held_card()["request_id"]
        self.team_corrects(first, "correct", " ; ")
        self.assertEqual((self.current()["status"], self.current()["reason"]), (tracking.HELD, main.UNKNOWN_NUMBER))
        self.assertNotEqual(self.held_card()["request_id"], first)
        self.assertIn("saved no usable numbers", self.held_card()["message"])
        self.team_corrects(self.held_card()["request_id"], "correct", "XY 12302")
        self.assertEqual(self.current()["status"], tracking.HELD)
        self.assertIn("Not a number: 'XY 12302'", self.held_card()["message"])

    def test_other_holds_stay_a_plain_message(self):
        with mock.patch.object(config, "MAX_ATTACHMENT_MB", 0.000001):
            self.new_email(_email("Please send POD for 1234567"))
        self.assertEqual(self.handoff_files("teams")[0]["kind"], "info")
        self.assertNotIn("correction_card", self.current()["state"])

    def test_power_bi_error_holds_the_email(self):
        self.new_email(_email("Please send POD for 7777777"))
        self.power_bi_answers({"error": "dataset refresh in progress"})
        self.assertEqual(self.current()["status"], tracking.HELD)
        self.assertIn("dataset refresh in progress", self.handoff_files("teams")[0]["message"])

    def test_ambiguous_number_waits_for_the_team_then_sends_the_chosen_file(self):
        self.to_the_team(_email("Please send POD for 555555"))
        self.assertEqual(self.current()["status"], tracking.WAITING_TEAM)
        card = self.handoff_files("teams")[0]
        self.assertEqual(card["kind"], "choice")
        self.assertEqual(
            [option["id"] for option in card["options"]],
            ["file:0", "file:1", "all", "not_all", "manual"],
        )
        labels = [option["label"] for option in card["options"][:2]]
        for name in ("555555 - Signed.pdf", "555555 - Signed (2).pdf"):
            path = self.scan / "Sep" / name
            self.assertTrue(any(name in label and str(path) in label for label in labels))
        self.reply.assert_not_called()

        self.team_answers(card["request_id"], "file:1")

        self.assertEqual(self.current()["status"], tracking.REPLIED)
        self.assertEqual([path.name for path in self.reply.call_args.args[2]], ["555555 - Signed.pdf"])

    def resume_team(self):
        main._resume_team(tracking.latest(tracking.WAITING_TEAM)[0], self.counters)

    def pending_cards(self) -> list[str]:
        return sorted(path.stem for path in (self.handoff_dir / "teams").glob("*.json"))

    def test_a_card_waiting_for_an_answer_is_not_posted_again_and_records_nothing(self):
        self.to_the_team(_email("Please send POD for 555555"))
        card = self.current()["state"]["cards"]["555555"]
        (self.handoff_dir / "teams" / f"{card}.json").unlink()  # Flow 3 took it: the card is in Teams
        with mock.patch.object(main, "CARD_RESEND_AFTER", timedelta(0)):
            for _ in range(5):
                self.resume_team()
        self.assertEqual(self.current()["state"]["cards"]["555555"], card)
        self.assertEqual([e["status"] for e in tracking.history("E1")], [tracking.WAITING_PBI, tracking.WAITING_TEAM])

    def test_a_card_flow_3_never_took_is_posted_again_after_30_minutes_then_reported(self):
        self.to_the_team(_email("Please send POD for 555555"))
        original = self.current()["state"]["cards"]["555555"]
        self.resume_team()  # under 30 minutes: nothing yet
        self.assertEqual(self.pending_cards(), [original])
        self.assertEqual(len(tracking.history("E1")), 2)  # Waiting Power BI, then Waiting Team; no more

        with mock.patch.object(main, "CARD_RESEND_AFTER", timedelta(0)):
            for resend in range(1, main.CARD_RESENDS + 1):
                self.resume_team()
                current = self.current()["state"]["cards"]["555555"]
                self.assertEqual(current, f"{original}-r{resend}")
                self.assertEqual(self.pending_cards(), [current])  # the old file is archived, not piled up
            self.resume_team()  # the last resend did not arrive either

        state = self.current()["state"]
        self.assertEqual(self.pending_cards(), [])
        self.assertEqual(tracking.card_problems(tracking.WAITING_TEAM, state),
                         {"555555": "Flow 3 did not take the card from the teams folder"})
        self.assertEqual(self.current()["status"], tracking.WAITING_TEAM)
        self.reply.assert_not_called()

        # Once Flow 3 is fixed, the dashboard posts it again with three more resends.
        with mock.patch.object(config, "LOCK_FILE", Path(self._tmp.name) / "run.lock"):
            self.assertEqual(main.send_cards_again([self.current()]), 1)
        self.assertEqual(self.pending_cards(), [self.current()["state"]["cards"]["555555"]])
        self.assertEqual(tracking.card_problems(tracking.WAITING_TEAM, self.current()["state"]), {})

    def test_a_card_teams_refused_is_posted_again_and_its_error_shown_if_it_keeps_failing(self):
        self.to_the_team(_email("Please send POD for 555555"))
        card = self.current()["state"]["cards"]["555555"]
        (self.handoff_dir / "teams" / f"{card}.json").unlink()  # Flow 3 took it, then Teams refused it
        results = self.handoff_dir / "teams_results"
        results.mkdir(parents=True, exist_ok=True)
        (results / f"{card}.json").write_text(json.dumps({"request_id": card, "error": "BadRequest"}), encoding="utf-8")
        self.resume_team()
        self.assertEqual(self.current()["state"]["card_delivery"]["555555"]["error"], "BadRequest")
        with mock.patch.object(main, "CARD_RESEND_AFTER", timedelta(0)):
            self.resume_team()
        self.assertEqual(self.current()["state"]["cards"]["555555"], f"{card}-r1")
        self.assertEqual(self.current()["state"]["card_delivery"]["555555"]["resends"], 1)

    def test_response_to_original_team_card_after_retry_is_still_accepted(self):
        self.to_the_team(_email("Please send POD for 555555"))
        original = self.current()["state"]["cards"]["555555"]
        with mock.patch.object(main, "CARD_RESEND_AFTER", timedelta(0)):
            self.resume_team()
        self.assertNotEqual(self.current()["state"]["cards"]["555555"], original)

        results = self.handoff_dir / "teams_results"
        results.mkdir(parents=True, exist_ok=True)
        (results / f"{original}.json").write_text(
            json.dumps({"choice": "file:1", "responder": "Ana"}),
            encoding="utf-8",
        )
        self.resume_team()

        self.assertEqual(self.current()["status"], tracking.REPLIED)
        self.assertEqual([path.name for path in self.reply.call_args.args[2]], ["555555 - Signed.pdf"])

    def test_a_correction_card_flow_3_never_took_is_posted_again(self):
        self.new_email(_email("Please send POD for 7777777"))
        self.power_bi_answers({"rows": []})
        first = self.current()["state"]["correction_card"]
        with mock.patch.object(main, "CARD_RESEND_AFTER", timedelta(0)):
            main._resume_held(tracking.latest(tracking.HELD)[0], self.counters)
        second = self.current()["state"]["correction_card"]
        self.assertNotEqual(second, first)
        self.assertIn(second, self.pending_cards())
        self.assertNotIn(first, self.pending_cards())
        self.assertIn("Correct the number(s) below", self.handoff_files("teams")[-1]["message"])

    def test_an_email_answered_by_hand_gets_no_draft_and_no_outstanding_change(self):
        self.manual_reply.return_value = {"action": "reply all", "at": "2026-10-21T02:15:00+00:00"}
        self.new_email(_email("Please send POD for 1234567"))
        self.assertEqual(self.current()["status"], tracking.RESOLVED)
        self.assertEqual(self.current()["reason"], "replied manually in Outlook (reply all on 21 Oct 2026 10:15 UTC+8)")
        self.reply.assert_not_called()
        self.mark_read.assert_not_called()
        self.assertEqual(self.handoff_files("outstanding"), [])

    def test_a_failed_check_before_replying_sends_nothing(self):
        self.manual_reply.side_effect = RuntimeError("Outlook not reachable")
        with self.assertRaises(RuntimeError):
            self.new_email(_email("Please send POD for 1234567"))
        self.reply.assert_not_called()

    def check_outlook(self):
        main._check_outlook(self.inbox, self.counters)

    def test_a_waiting_email_answered_by_hand_is_resolved_and_its_card_withdrawn(self):
        self.to_the_team(_email("Please send POD for 555555"))
        card = self.current()["state"]["cards"]["555555"]
        self.email_status.return_value = {"folder": self.inbox, "manual": {"action": "reply", "at": ""}}
        self.check_outlook()
        self.assertEqual((self.current()["status"], self.current()["reason"]),
                         (tracking.RESOLVED, "replied manually in Outlook (reply)"))
        self.assertEqual(self.pending_cards(), [])  # not posted yet: withdrawn
        self.assertTrue((self.handoff_dir / "archive" / f"teams__{card}.json").exists())
        self.assertEqual(tracking.latest(tracking.WAITING_TEAM), [])  # an answer to it is never read

    def test_a_late_power_bi_result_for_an_email_answered_by_hand_is_archived(self):
        self.new_email(_email("Please send POD for 7777777"))
        request = self.current()["state"]["pbi_request"]
        self.email_status.return_value = {"folder": self.inbox, "manual": {"action": "reply", "at": ""}}
        self.check_outlook()
        self.assertEqual(self.handoff_files("pbi_requests"), [])
        results = self.handoff_dir / "pbi_results"
        results.mkdir(parents=True, exist_ok=True)
        (results / f"{request}.json").write_text('{"rows": []}', encoding="utf-8")
        # A second in the future, so a file written just now counts as left long
        # enough even on a coarse clock (Windows CI with Python 3.10).
        with mock.patch.object(main, "CARD_RESEND_AFTER", timedelta(seconds=-1)):
            main._sweep_handoff(self.counters)
        self.assertEqual(self.handoff_files("pbi_results"), [])
        self.assertEqual(self.current()["status"], tracking.RESOLVED)

    def test_a_waiting_email_still_in_the_inbox_and_not_answered_waits_on(self):
        self.new_email(_email("Please send POD for 7777777"))
        self.check_outlook()
        self.assertEqual(self.current()["status"], tracking.WAITING_PBI)
        self.assertEqual(len(tracking.history("E1")), 1)

    def test_a_pod_request_moved_to_the_finished_folder_is_resolved(self):
        self.to_the_team(_email("Please send POD for 555555"))
        finished = SimpleNamespace(Name=config.FINISHED_FOLDER, FolderPath="\\\\POD\\Inbox\\done", Parent=self.inbox)
        self.email_status.return_value = {"folder": finished, "manual": None}  # Outlook kept its entry ID
        self.check_outlook()
        self.assertEqual((self.current()["status"], self.current()["reason"]),
                         (tracking.RESOLVED, f"moved to {config.FINISHED_FOLDER} in Outlook"))
        self.assertEqual(self.pending_cards(), [])

    def test_a_moved_email_is_found_in_the_finished_folder_by_its_message_id(self):
        self.new_email(_email("Please send POD for 7777777", message_id="<abc@customer.example>"))
        self.email_status.return_value = None  # moved: a new entry ID
        self.in_finished.return_value = True
        self.check_outlook()
        self.in_finished.assert_called_once_with(self.inbox, config.FINISHED_FOLDER, "<abc@customer.example>")
        self.assertEqual(self.current()["status"], tracking.RESOLVED)
        self.reply.assert_not_called()

    def test_a_pod_request_moved_elsewhere_or_deleted_is_held_once_for_the_team(self):
        self.new_email(_email("Please send POD for 7777777"))
        self.email_status.return_value = None
        for _ in range(3):
            self.check_outlook()
        self.assertEqual((self.current()["status"], self.current()["reason"]), (tracking.HELD, main.MOVED))
        self.assertEqual([e["status"] for e in tracking.history("E1")], [tracking.WAITING_PBI, tracking.HELD])
        self.assertEqual(self.handoff_files("pbi_requests"), [])  # nothing more is asked for it

    def test_a_held_email_moved_elsewhere_gets_no_more_teams_cards(self):
        self.new_email(_email("Please send POD for 7777777"))
        self.power_bi_answers({"rows": []})  # held, with a correction card
        self.assertIn("correction_card", self.current()["state"])
        self.email_status.return_value = None
        self.check_outlook()
        self.assertEqual((self.current()["status"], self.current()["reason"]), (tracking.HELD, main.MOVED))
        self.assertNotIn("correction_card", self.current()["state"])  # its card is not posted again
        self.assertEqual(self.pending_cards(), [])

    def test_an_other_email_moved_to_any_folder_is_resolved(self):
        tracking.record(_email("Remittance 20261111"), tracking.OTHER_INTENT, numbers=["20261111"])
        self.email_status.return_value = {"folder": SimpleNamespace(Name="Accounts", FolderPath="\\\\POD\\Accounts", Parent=self.inbox), "manual": None}
        self.check_outlook()
        self.assertEqual((self.current()["status"], self.current()["reason"]),
                         (tracking.RESOLVED, "moved to Accounts in Outlook"))

    def test_the_message_id_is_kept_on_every_later_event(self):
        self.to_the_team(_email("Please send POD for 555555", message_id="<m1@customer.example>"))
        self.team_answers(self.current()["state"]["cards"]["555555"], "file:0")
        self.assertEqual({e["message_id"] for e in tracking.history("E1")}, {"<m1@customer.example>"})

    def test_files_no_open_card_needs_are_archived_after_30_minutes(self):
        handoff.write_teams("old-info", "info", "Held")
        self.to_the_team(_email("Please send POD for 555555"))
        card = self.current()["state"]["cards"]["555555"]
        main._sweep_handoff(self.counters)
        self.assertIn("old-info", self.pending_cards())  # not 30 minutes yet
        # A second in the future, so a file written just now counts as left long
        # enough even on a coarse clock (Windows CI with Python 3.10).
        with mock.patch.object(main, "CARD_RESEND_AFTER", timedelta(seconds=-1)):
            main._sweep_handoff(self.counters)
        self.assertEqual(self.pending_cards(), [card])  # the open card is left to its own check
        self.assertTrue((self.handoff_dir / "archive" / "teams__old-info.json").exists())

    def test_team_choosing_manual_holds_the_email(self):
        self.to_the_team(_email("Please send POD for 555555"))
        card = self.handoff_files("teams")[0]
        self.team_answers(card["request_id"], "manual")
        self.assertEqual(self.current()["status"], tracking.HELD)
        self.assertIn("Ana chose to handle it manually", self.current()["reason"])
        self.reply.assert_not_called()

    def test_not_all_checks_power_bi_and_unsigned_uses_not_received_reply_and_outstanding(self):
        self.to_the_team(_email("Please send POD for 555555"))
        card = self.handoff_files("teams")[0]
        self.team_answers(card["request_id"], "not_all")

        self.assertEqual(self.current()["status"], tracking.WAITING_PBI)
        request = self.handoff_files("pbi_requests")[0]
        self.assertEqual(request["numbers"], ["555555"])
        self.assertEqual(request["scan_missing_numbers"], ["555555"])
        self.assertEqual(
            set(self.current()["state"]["skipped_files"]["555555"]),
            {
                str(self.scan / "Sep" / "555555 - Signed.pdf"),
                str(self.scan / "Sep" / "555555 - Signed (2).pdf"),
            },
        )
        self.reply.assert_not_called()

        self.power_bi_answers({
            "rows": [{
                "Connote": "555555",
                "Reference": "",
                "POD Status": "Unsigned",
            }]
        })

        self.assertEqual(self.current()["status"], tracking.REPLIED)
        self.assertIn("not received the Proof of Delivery for 555555", self.reply.call_args.args[1])
        self.assertEqual(self.reply.call_args.args[2], [])
        added = [
            item for item in self.handoff_files("outstanding")
            if item["action"] == "add" and item["number"] == "555555"
        ]
        self.assertEqual(added[0]["row"]["Status"], "Unsigned")

    def test_not_all_keeps_the_earlier_power_bi_answers_for_the_other_numbers(self):
        self.new_email(_email("Please send POD for 555555 and 7777777"))  # 7777777: no scan
        self.power_bi_answers({"rows": [{"Connote": "7777777", "Reference": "", "POD Status": "Not Received"}]})
        self.assertEqual(self.current()["status"], tracking.WAITING_TEAM)
        self.team_answers(self.handoff_files("teams")[0]["request_id"], "not_all")
        self.power_bi_answers({"rows": [{"Connote": "555555", "Reference": "", "POD Status": "Unsigned"}]})

        self.assertEqual(self.current()["status"], tracking.REPLIED)
        self.assertEqual(set(self.current()["state"]["not_received"]), {"555555", "7777777"})
        body = self.reply.call_args.args[1]
        self.assertIn("555555", body)
        self.assertIn("7777777", body)
        added = {item["number"] for item in self.handoff_files("outstanding") if item["action"] == "add"}
        self.assertEqual(added, {"555555", "7777777"})

    def test_not_all_with_no_power_bi_match_uses_the_existing_unknown_number_review(self):
        self.to_the_team(_email("Please send POD for 555555"))
        card = self.handoff_files("teams")[0]
        self.team_answers(card["request_id"], "not_all")

        self.power_bi_answers({"rows": []})

        self.assertEqual(self.current()["status"], tracking.HELD)
        self.assertIn("not in Power BI", self.current()["reason"])
        self.reply.assert_not_called()
        added = self.handoff_files("outstanding")
        self.assertEqual([(item["action"], item["number"]) for item in added], [("add", "555555")])
        self.assertEqual(added[0]["row"]["Status"], "Not in The System")

    def test_team_answer_outside_the_options_is_refused(self):
        self.to_the_team(_email("Please send POD for 555555"))
        card = self.handoff_files("teams")[0]
        with self.assertRaises(ValueError):
            self.team_answers(card["request_id"], "file:9")

    def test_attachments_over_the_limit_make_a_draft_without_them_even_in_send_mode(self):
        with mock.patch.object(config, "MAX_ATTACHMENT_MB", 0.000001), mock.patch.object(config, "SEND_MODE", "send"):
            self.new_email(_email("Please send POD for 1234567"))
        self.assertEqual(self.current()["status"], tracking.REPLIED)
        self.assertIn("draft without its attachments", self.current()["reason"])
        entry_id, body, files, mode = self.reply.call_args.args[:4]
        self.assertEqual((files, mode), ([], "draft"))  # a draft, whatever the send mode (product owner, D-050)
        self.assertNotIn(reply_generator.AUTOMATED_NOTICE, body)  # drafts carry no notice
        self.assertIn("Please find attached the Proof of Delivery for 1234567.", body)
        self.mark_read.assert_called_once()
        self.assertTrue(self.current()["state"]["attach_by_hand"])
        self.assertEqual([Path(path).name for path in self.current()["state"]["attachments"]], ["1234567_a.pdf"])
        message = self.handoff_files("teams")[0]["message"]
        self.assertIn("a Reply All draft without them is in the POD mailbox's Drafts", message)
        self.assertIn("- 1234567: scan found (1234567_a.pdf, 0.0 MB)", message)
        self.assertNotIn("to attach by hand", message)  # no folder set: the list only
        removed = [item["number"] for item in self.handoff_files("outstanding") if item["action"] == "remove"]
        self.assertEqual(removed, ["1234567"])

    def test_numbers_without_a_scan_reach_pod_outstanding_when_the_files_are_too_large(self):
        # Not received (its Power BI status) and not in our system (product owner): both are added (D-050).
        state = {"found": {"1234567": str(self.scan / "Sep" / "1234567_a.pdf")},
                 "not_received": {"7777777": {"Connote": "7777777", "Reference": "", "POD Status": "Unsigned"}},
                 "not_in_system": ["8888888"]}
        email = {**_email("Please send POD for 1234567, 7777777 and 8888888"), "numbers": "1234567;7777777;8888888"}
        with mock.patch.object(config, "MAX_ATTACHMENT_MB", 0.000001):
            main._reply(email, state, self.counters)
        added = {item["number"]: item["row"] for item in self.handoff_files("outstanding") if item["action"] == "add"}
        self.assertEqual(sorted(added), ["7777777", "8888888"])
        self.assertEqual(added["8888888"]["Status"], "Not in The System")
        self.assertEqual(self.current()["state"]["outstanding_add_numbers"], ["7777777", "8888888"])
        body = self.reply.call_args.args[1]
        self.assertIn("We have not received the Proof of Delivery for 7777777", body)
        self.assertIn("Regarding 8888888, we were unable to find it in our system.", body)

    def test_a_held_large_email_lists_each_number_and_links_its_copied_files(self):
        upload = Path(self._tmp.name) / "SharePoint" / "Manual upload"
        settings = {"POD_MANUAL_UPLOAD_DIR": str(upload),
                    "POD_MANUAL_UPLOAD_URL": "https://example.sharepoint.com/sites/POD/Shared Documents/Manual upload"}
        with mock.patch.object(config, "MAX_ATTACHMENT_MB", 0.000001), mock.patch.dict(os.environ, settings):
            self.new_email(_email("Please send POD for 1234567 and 7777777"))
            self.power_bi_answers({"rows": [{"Connote": "7777777", "Reference": "", "POD Status": "Unsigned"}]})
            message = self.handoff_files("teams")[0]["message"]
            self.assertIn("- 1234567: scan found (1234567_a.pdf, 0.0 MB)", message)
            self.assertIn("- 7777777: not received (Power BI status: Unsigned)", message)
            rid = handoff.request_id("E1")
            self.assertIn(f"https://example.sharepoint.com/sites/POD/Shared%20Documents/Manual%20upload/{rid}", message)
            self.assertEqual([path.name for path in (upload / rid).iterdir()], ["1234567_a.pdf"])
            self.assertEqual([(item["action"], item["number"]) for item in self.handoff_files("outstanding")],
                             [("add", "7777777"), ("remove", "1234567")])

            main._clean_manual_uploads(self.counters)  # its draft is still a draft: kept
            self.assertTrue((upload / rid).exists())
            self.assertEqual(pilot.summary(tracking.latest(tracking.REPLIED))["drafts"], 0)  # not measured
            # Sent with the files added by hand: the pilot measure records it, the copies go.
            tracking.record(self.current(), tracking.REPLIED, reason="draft sent edited",
                            state={**self.current()["state"], "pilot": pilot.SENT_OTHER_PODS})
            (upload / "notes").mkdir()  # not the agent's: never touched
            main._clean_manual_uploads(self.counters)
            self.assertFalse((upload / rid).exists())
            self.assertTrue((upload / "notes").exists())

    def test_a_read_only_copy_is_removed_and_one_that_fails_does_not_stop_the_others(self):
        # D-059: a synced file may be read-only; a folder in use is tried again next run.
        upload = Path(self._tmp.name) / "Manual upload"
        read_only, in_use = upload / ("a" * 16), upload / ("b" * 16)
        for folder in (read_only, in_use):
            folder.mkdir(parents=True)
            (folder / "scan.pdf").write_bytes(b"x")
        os.chmod(read_only / "scan.pdf", stat.S_IREAD)
        real_rmtree = shutil.rmtree

        def rmtree(path, **kwargs):
            if Path(path).name == in_use.name:  # by name: the folder is resolved, the temp path may be short (RUNNER~1)
                raise PermissionError(5, "Access is denied", str(path))
            return real_rmtree(path, **kwargs)

        with mock.patch.dict(os.environ, {"POD_MANUAL_UPLOAD_DIR": str(upload)}), \
                mock.patch.object(main.shutil, "rmtree", side_effect=rmtree):
            main._clean_manual_uploads(self.counters)
        self.assertFalse(read_only.exists())
        self.assertTrue(in_use.exists())
        self.assertEqual(self.counters[f"files to send by hand not removed ({in_use.name}: PermissionError)"], 1)

    def test_an_email_held_for_its_size_before_keeps_its_copies_until_resolved(self):
        upload = Path(self._tmp.name) / "Manual upload"
        rid = handoff.request_id("E1")
        (upload / rid).mkdir(parents=True)
        tracking.record(_email("Please send POD for 1234567"), tracking.HELD, reason=f"{main.OVERSIZE} 25.0 MB, over the 20 MB limit")
        with mock.patch.dict(os.environ, {"POD_MANUAL_UPLOAD_DIR": str(upload)}):
            main._clean_manual_uploads(self.counters)
            self.assertTrue((upload / rid).exists())
            tracking.record(self.current(), tracking.RESOLVED, reason="resolved in the dashboard")
            main._clean_manual_uploads(self.counters)
        self.assertFalse((upload / rid).exists())

    def test_a_folder_for_files_to_send_by_hand_inside_the_project_or_a_scan_folder_is_refused(self):
        for folder in (config.PROJECT_DIR / "data" / "manual", self.scan / "manual"):
            with self.subTest(folder=folder), mock.patch.dict(os.environ, {"POD_MANUAL_UPLOAD_DIR": str(folder)}):
                with self.assertRaisesRegex(config.ConfigError, "POD_MANUAL_UPLOAD_DIR"):
                    config.manual_upload_dir()

    def test_file_in_newly_excluded_folder_is_not_attached_from_pending_state(self):
        excluded = self.scan / "Archive"
        excluded.mkdir()
        path = excluded / "1234567.pdf"
        path.write_bytes(b"%PDF-1.4 test")
        email = _email("Please send POD for 1234567")

        with mock.patch.object(config, "SCAN_EXCLUDED_FOLDERS", [Path("Archive")]):
            main._reply(email, {"found": {"1234567": str(path)}}, self.counters)

        self.assertEqual(self.current()["status"], tracking.HELD)
        self.assertIn("excluded by POD_SCAN_EXCLUDED_FOLDERS", self.current()["reason"])
        self.reply.assert_not_called()

    def test_wrong_column_map_fails_before_any_reply(self):
        self.new_email(_email("Please send POD for 7777777"))
        with mock.patch.dict(os.environ, {"POD_OUTSTANDING_COLUMNS": "A=nonsense"}):
            with self.assertRaises(config.ConfigError):
                self.power_bi_answers({"rows": [{"Connote": "7777777"}]})
        self.reply.assert_not_called()


class PilotCheckTest(FlowTest):
    """Each run records what became of the agent's drafts (D-014)."""

    def draft_one(self):
        self.new_email(_email("Please send POD for 1234567"))
        state = self.current()["state"]
        self.assertEqual((state["draft_entry_id"], state["conversation_id"]), ("D1", "C1"))
        self.assertIn("drafted_at", state)
        return state

    def test_a_sent_draft_is_recorded_once(self):
        state = self.draft_one()
        sent = [{"conversation_id": "C1", "sent_on": state["drafted_at"], "body": state["reply"], "attachments": ["1234567_a.pdf"]}]
        with mock.patch.object(outlook_reader, "sent_replies_since", return_value=sent) as read_sent,                 mock.patch.object(outlook_reader, "draft_exists", return_value=False):
            main._check_drafts("POD folder", self.counters)
            main._check_drafts("POD folder", self.counters)  # already decided: not read again
        self.assertEqual(read_sent.call_count, 1)
        self.assertEqual(self.current()["status"], tracking.REPLIED)
        self.assertEqual(self.current()["reason"], "draft sent, same PODs")
        self.assertEqual(self.current()["state"]["pilot"], pilot.SENT_SAME_PODS)

    def test_a_draft_sent_earlier_as_unedited_still_gets_its_category(self):
        # Recorded before D-053 with the old outcome name.
        state = self.draft_one()
        tracking.record(self.current(), tracking.REPLIED, reason="draft sent unedited",
                        state={**state, "pilot": "sent unedited"})
        with mock.patch.object(outlook_reader, "add_categories", return_value=["POD Agent Reply"]) as add:
            main._check_email_categories(mock.MagicMock(), self.counters)
        add.assert_called_once()

    def test_sent_draft_gets_pod_agent_reply_category(self):
        state = self.draft_one()
        tracking.record(
            self.current(),
            tracking.REPLIED,
            reason="draft sent unedited",
            state={**state, "pilot": pilot.SENT_SAME_PODS},
        )
        mailbox = mock.Mock()
        mailbox.Store.StoreID = "POD_STORE"
        with mock.patch.object(
            outlook_reader, "add_categories", return_value=["POD Agent Reply"]
        ) as add_categories:
            main._check_email_categories(mailbox, self.counters)

        add_categories.assert_called_once_with(
            "E1", ["POD Agent Reply"], store_id="POD_STORE"
        )
        self.assertTrue(self.current()["state"]["category_pod_agent_replay"])

    def test_a_sent_draft_moved_by_hand_gets_its_category_by_its_new_entry_id(self):
        # D-059: moved to Actioned/Finished by hand, the email has a new entry ID in Outlook.
        state = self.draft_one()
        tracking.record({**self.current(), "message_id": "<e1@customer.example>"}, tracking.REPLIED,
                        reason="draft sent, same PODs", state={**state, "pilot": pilot.SENT_SAME_PODS})
        mailbox = mock.Mock()
        mailbox.Store.StoreID = "POD_STORE"
        not_found = Exception("(-2147221233, 'The operation failed. An object could not be found.')")
        added = mock.patch.object(outlook_reader, "add_categories",
                                  side_effect=lambda entry_id, *_, **__: (_ for _ in ()).throw(not_found)
                                  if entry_id == "E1" else ["POD Agent Reply"]).start()
        found = mock.patch.object(outlook_reader, "find_by_message_id",
                                  return_value={"entry_id": "E1-NEW", "in_finished": True}).start()
        main._check_email_categories(mailbox, self.counters)
        found.assert_called_once_with(mailbox, "<e1@customer.example>", config.FINISHED_FOLDER)
        self.assertEqual(added.call_args.args[0], "E1-NEW")
        self.assertEqual(self.current()["state"]["moved_entry_id"], "E1-NEW")
        self.assertTrue(self.current()["state"]["category_pod_agent_replay"])
        self.assertNotIn("category update failed (Exception)", self.counters)
        main._check_email_categories(mailbox, self.counters)  # done: not tried again
        self.assertEqual(added.call_count, 2)

    def test_on_list_category_waits_until_all_add_requests_are_archived(self):
        email = _email("POD request", entry_id="LIST1")
        tracking.record(
            email,
            tracking.HELD,
            state={"outstanding_add_numbers": ["1234567", "2345678"]},
        )
        rid = handoff.request_id("LIST1")
        first = handoff.write_outstanding(rid, "add", "1234567")
        second = handoff.write_outstanding(rid, "add", "2345678")
        archive = self.handoff_dir / "archive"
        archive.mkdir()
        first.rename(archive / first.name)
        mailbox = mock.Mock()
        mailbox.Store.StoreID = "POD_STORE"
        with mock.patch.object(outlook_reader, "add_categories", return_value=["On List"]) as add_categories:
            main._check_email_categories(mailbox, self.counters)
            add_categories.assert_not_called()

            second.rename(archive / second.name)
            main._check_email_categories(mailbox, self.counters)

        add_categories.assert_called_once_with("LIST1", ["On List"], store_id="POD_STORE")
        self.assertTrue(self.current()["state"]["category_on_list"])

    def test_draft_still_waiting_is_left_for_the_next_run(self):
        self.draft_one()
        with mock.patch.object(outlook_reader, "sent_replies_since", return_value=[]),                 mock.patch.object(outlook_reader, "draft_exists", return_value=True):
            main._check_drafts("POD folder", self.counters)
        self.assertEqual(self.current()["reason"], "draft")
        self.assertNotIn("pilot", self.current()["state"])

    def test_sent_mode_replies_are_not_measured(self):
        self.reply.return_value = {"conversation_id": "C1"}
        with mock.patch.object(config, "SEND_MODE", "send"):
            self.new_email(_email("Please send POD for 1234567"))
        body = self.reply.call_args.args[1]
        self.assertIn(reply_generator.AUTOMATED_NOTICE, body)
        self.assertEqual(self.reply.call_args.args[3], "send")
        with mock.patch.object(outlook_reader, "sent_replies_since") as read_sent:
            main._check_drafts("POD folder", self.counters)
        read_sent.assert_not_called()


class CycleTest(FlowTest):
    """One whole run of `_cycle`: unread mail in, replies and resumes out, every email accounted for."""

    def setUp(self):
        super().setUp()
        self.unread = []
        self.processed = []
        for patcher in (
            mock.patch.object(outlook_reader, "connect_outlook"),
            mock.patch.object(outlook_reader, "get_folder", return_value=self.inbox),
            mock.patch.object(outlook_reader, "get_unread_emails", side_effect=lambda folder, ids: list(self.unread)),
            mock.patch.object(outlook_reader, "load_processed_ids", return_value=set()),
            mock.patch.object(outlook_reader, "mark_as_processed",
                              side_effect=lambda entry_id, path: self.processed.append(entry_id)),
            mock.patch.object(outlook_reader, "sent_replies_since", return_value=[]),
            mock.patch.object(outlook_reader, "draft_exists", return_value=True),
            mock.patch.object(model_registry, "load_active",
                              side_effect=lambda task: FixedIntentModel() if task == "intent" else None),
            mock.patch.object(classifier, "load_training_data", return_value=pd.DataFrame()),
            mock.patch.object(classifier, "has_enough_training_data", return_value=True),
        ):
            patcher.start()

    def test_a_run_holds_a_waiting_email_moved_out_of_the_inbox_before_its_answer_is_read(self):
        self.unread = [_email("Please send the POD for 7777777")]
        main._cycle(self.counters)
        self.assertEqual(self.current()["status"], tracking.WAITING_PBI)
        self.unread = []
        self.email_status.return_value = None  # the team moved it, not to the finished folder
        main._cycle(self.counters, process_new=False, resume=True)
        self.assertEqual((self.current()["status"], self.current()["reason"]), (tracking.HELD, main.MOVED))
        self.assertFalse([key for key in self.counters if "failed" in key], self.counters)

    def test_each_unread_email_is_handled_and_marked_processed_even_when_one_fails(self):
        self.unread = [
            _email("Please send the POD for 1234567"),
            _email("Please send the POD for 2345678", entry_id="BAD"),
        ]
        real = main._process_new_email

        def process(email, *args, **kwargs):
            if email["entry_id"] == "BAD":
                raise RuntimeError("boom")
            return real(email, *args, **kwargs)

        with mock.patch.object(main, "_process_new_email", side_effect=process):
            self.assertEqual(main._cycle(self.counters), 2)

        statuses = {event["entry_id"]: event for event in tracking.latest()}
        self.assertEqual(statuses["E1"]["status"], tracking.REPLIED)
        self.reply.assert_called_once()
        self.assertEqual(statuses["BAD"]["status"], tracking.ERROR)
        self.assertIn("RuntimeError: boom", statuses["BAD"]["reason"])
        # Both are marked processed: an error is recorded, not retried every 5 minutes.
        self.assertEqual(self.processed, ["E1", "BAD"])

    def test_progress_reports_processing_stages(self):
        self.unread = [_email("Please send the POD for 1234567")]
        output = StringIO()
        with redirect_stdout(output):
            main._cycle(self.counters, progress=True)

        messages = output.getvalue()
        self.assertIn("Found 1 unread email(s)", messages)
        self.assertIn("Extracting connote/reference numbers", messages)
        self.assertIn("Classifying whether this is a POD request", messages)
        self.assertIn("Searching scan files", messages)
        self.assertIn("Creating draft Reply All", messages)
        self.assertIn(f"Finished email 1/1: {tracking.REPLIED}", messages)

    def test_progress_is_suppressed_without_standard_output(self):
        with mock.patch.object(main.sys, "stdout", None):
            main._progress(True, "silent scheduled run")

    def test_process_new_cycle_skips_resume_work(self):
        with (
            mock.patch.object(outlook_reader, "load_processed_ids", return_value=set()),
            mock.patch.object(classifier, "load_training_data"),
            mock.patch.object(model_registry, "load_active", return_value=None),
            mock.patch.object(classifier, "has_enough_training_data", return_value=False),
            mock.patch.object(outlook_reader, "connect_outlook"),
            mock.patch.object(outlook_reader, "get_folder", return_value="MAILBOX"),
            mock.patch.object(outlook_reader, "get_unread_emails", return_value=[]),
            mock.patch.object(main, "_check_drafts") as check_drafts,
            mock.patch.object(main, "_check_email_categories") as check_categories,
            mock.patch.object(main, "_move_finished") as move_finished,
            mock.patch.object(tracking, "latest") as latest,
        ):
            self.assertEqual(main._cycle(self.counters, process_new=True, resume=False), 0)

        check_drafts.assert_not_called()
        check_categories.assert_not_called()
        move_finished.assert_not_called()
        latest.assert_not_called()

    def test_resume_cycle_skips_email_intake_and_model_loading(self):
        with (
            mock.patch.object(outlook_reader, "load_processed_ids") as load_processed,
            mock.patch.object(classifier, "load_training_data") as load_training,
            mock.patch.object(model_registry, "load_active") as load_model,
            mock.patch.object(outlook_reader, "connect_outlook"),
            mock.patch.object(outlook_reader, "get_folder", return_value="MAILBOX"),
            mock.patch.object(outlook_reader, "get_unread_emails") as get_unread,
            mock.patch.object(main, "_check_drafts") as check_drafts,
            mock.patch.object(main, "_check_email_categories") as check_categories,
            mock.patch.object(main, "_move_finished") as move_finished,
            mock.patch.object(tracking, "latest", return_value=[]),
        ):
            self.assertEqual(main._cycle(self.counters, process_new=False, resume=True), 0)

        load_processed.assert_not_called()
        load_training.assert_not_called()
        load_model.assert_not_called()
        get_unread.assert_not_called()
        check_drafts.assert_called_once()
        check_categories.assert_called_once()
        move_finished.assert_called_once()

    def test_an_unreadable_share_leaves_new_and_reviewed_emails_for_the_next_run(self):
        tracking.record(_email("Can you check?", entry_id="R1"), tracking.MANUAL_REVIEW)
        main.record_review(tracking.latest()[0], "POD_REQUEST", "1234567", "")
        self.unread = [_email("Please send the POD for 1234567", entry_id="NEW")]
        with mock.patch.object(config, "SCAN_FOLDERS", [Path(self._tmp.name) / "missing"]):
            main._cycle(self.counters)

        self.assertEqual(self.processed, [])  # read again next run
        self.assertEqual([e["entry_id"] for e in tracking.latest()], ["R1"])  # nothing recorded for NEW
        self.assertEqual(tracking.latest()[0]["status"], tracking.REVIEWED)  # still waiting
        self.assertTrue(any(key.startswith("share_unavailable") for key in self.counters))
        self.reply.assert_not_called()

    def test_a_failing_category_check_never_stops_the_run(self):
        self.unread = [_email("Please send the POD for 1234567")]
        with mock.patch.object(main, "_check_email_categories", side_effect=RuntimeError("sync")):
            self.assertEqual(main._cycle(self.counters), 1)
        self.assertEqual(tracking.latest()[0]["status"], tracking.REPLIED)
        self.assertIn("category check failed (RuntimeError: sync)", self.counters)

    def test_a_reviewed_email_is_continued_in_the_same_run(self):
        tracking.record(_email("Can you check?", entry_id="R1"), tracking.MANUAL_REVIEW)
        main.record_review(tracking.latest()[0], "POD_REQUEST", "1234567", "")
        main._cycle(self.counters)
        self.assertEqual(tracking.latest()[0]["status"], tracking.REPLIED)
        self.reply.assert_called_once()


class MoveFinishedTest(FlowTest):
    """Replied emails with every POD attached go to Actioned/Finished, from the switch-on time (D-031)."""

    SINCE = "2026-10-19T08:00:00+00:00"

    def setUp(self):
        super().setUp()
        for patcher in (
            mock.patch.object(config, "MOVE_FINISHED", True),
            mock.patch.object(config, "MOVE_FINISHED_SINCE", self.SINCE),
            mock.patch.object(config, "FINISHED_FOLDER", "Actioned/Finished"),
        ):
            patcher.start()
        self.move = mock.patch.object(outlook_reader, "move_to_child_folder", return_value="MOVED").start()

    def replied(self, entry_id, sent_at=None, **state):
        state = {"found": {"1234567": "a.pdf"}, **state}
        if sent_at:
            state["sent_at"] = sent_at
        tracking.record({"entry_id": entry_id, "subject": entry_id}, tracking.REPLIED, state=state)

    def test_only_finished_replies_sent_after_the_switch_on_move_once(self):
        self.replied("SENT_AFTER", "2026-10-19T09:00:00+00:00")
        self.replied("SENT_BEFORE", "2026-10-19T07:59:00+00:00")
        self.replied("NOT_RECEIVED", "2026-10-19T09:00:00+00:00", not_received={"7654321": {}})
        self.replied("STILL_A_DRAFT")  # no sent_at yet
        main._move_finished("POD folder", self.counters)
        self.move.assert_called_once_with("SENT_AFTER", "POD folder", "Actioned/Finished")
        latest = {event["entry_id"]: event for event in tracking.latest()}
        self.assertEqual(latest["SENT_AFTER"]["state"]["moved_to"], "Actioned/Finished")
        self.assertEqual(latest["SENT_AFTER"]["state"]["moved_entry_id"], "MOVED")
        main._move_finished("POD folder", self.counters)  # next run: not moved again
        self.move.assert_called_once()

    def test_a_reply_already_filed_by_hand_is_recorded_as_moved(self):
        # D-059: its old entry ID fails; found in Actioned/Finished by its Internet Message ID.
        tracking.record({"entry_id": "BY_HAND", "subject": "s", "message_id": "<x@customer.example>"}, tracking.REPLIED,
                        state={"found": {"1234567": "a.pdf"}, "sent_at": "2026-10-19T09:00:00+00:00"})
        self.move.side_effect = RuntimeError("not found")
        with mock.patch.object(outlook_reader, "find_by_message_id",
                               return_value={"entry_id": "FILED", "in_finished": True}):
            main._move_finished("POD folder", self.counters)
        self.move.assert_called_once_with("BY_HAND", "POD folder", "Actioned/Finished")  # not moved again
        state = tracking.latest()[0]["state"]
        self.assertEqual((state["moved_to"], state["moved_entry_id"]), ("Actioned/Finished", "FILED"))

    def test_the_folder_name_comes_from_the_setting(self):
        self.replied("SENT_AFTER", "2026-10-19T09:00:00+00:00")
        with mock.patch.object(config, "FINISHED_FOLDER", "Done"):
            main._move_finished("POD folder", self.counters)
        self.move.assert_called_once_with("SENT_AFTER", "POD folder", "Done")
        self.assertEqual(tracking.latest()[0]["state"]["moved_to"], "Done")

    def test_nothing_moves_while_the_switch_is_off(self):
        self.replied("SENT_AFTER", "2026-10-19T09:00:00+00:00")
        with mock.patch.object(config, "MOVE_FINISHED", False):
            main._move_finished("POD folder", self.counters)
        self.move.assert_not_called()

    def test_a_failed_move_is_counted_and_the_others_still_move(self):
        self.replied("A", "2026-10-19T09:00:00+00:00")
        self.replied("B", "2026-10-19T09:01:00+00:00")
        self.move.side_effect = [LookupError("Folder 'Actioned/Finished' not found"), "MOVED"]
        main._move_finished("POD folder", self.counters)
        self.assertEqual(self.move.call_count, 2)
        self.assertTrue(any(key.startswith("move to Actioned/Finished failed") for key in self.counters))
        self.assertNotIn("moved_to", tracking.history("A")[-1]["state"])  # tried again next run

    def test_a_sent_reply_records_when_it_went_out_and_a_draft_only_once_sent(self):
        with mock.patch.object(config, "SEND_MODE", "send"):
            self.reply.return_value = {"conversation_id": "C1"}
            self.new_email(_email("Please send the POD for 1234567"))
        self.assertIn("sent_at", tracking.latest()[0]["state"])
        self.reply.return_value = {"draft_entry_id": "D1", "conversation_id": "C2"}
        self.new_email(_email("Please send the POD for 1234567", entry_id="E2"))
        draft = tracking.history("E2")[-1]
        self.assertNotIn("sent_at", draft["state"])
        sent_on = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        sent_item = {"conversation_id": "C2", "sent_on": sent_on,
                     "body": draft["state"]["reply"], "attachments": ["1234567_a.pdf"]}
        with mock.patch.object(outlook_reader, "sent_replies_since", return_value=[sent_item]), \
                mock.patch.object(outlook_reader, "draft_exists", return_value=False):
            main._check_drafts("POD folder", self.counters)
        self.assertEqual(tracking.history("E2")[-1]["state"]["sent_at"], sent_on)


# A customer whose Power BI rows must name it, with its own scan folder (D-044).
CUSTOMER_ENV = {
    **PBI_ENV,
    "POD_PBI_LINKS": "Scans[Key]=POD Data[Key]",
    "POD_PBI_COLUMNS": "Sender=POD Data[Sender];POD Status=POD Data[POD Status];Scan Path=Scans[Path]",
    "POD_PBI_SCAN_PATH_COLUMN": "Scan Path",
    "POD_PBI_SENDER_COLUMN": "Sender",
    "POD_PBI_SENDER_CHECK": "special.example=special",
    "POD_SENDER_FOLDERS": "special.example=Special",
}


class PowerBIFlowTest(FlowTest):
    """The product owner's checking order: share, then Power BI by DoNumber, then Reference (D-044)."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, CUSTOMER_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)

    def scan_file(self, relative: str) -> Path:
        path = self.scan / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4 test")
        return path

    def customer_email(self, body: str):
        self.new_email(_email(body, sender_email="ops@special.example"))

    def row(self, connote, reference="", status="Signed", sender="Special Foods", scan=None):
        return {"Connote": connote, "Reference": reference, "POD Status": status, "Sender": sender,
                "Scan Path": scan.as_uri() if scan else ""}

    def attached(self) -> list[str]:
        return sorted(path.name for path in self.reply.call_args.args[2])

    def test_several_files_on_the_share_ask_power_bi_which_one_first(self):
        chosen = self.scan_file("Sep/3000001 - Signed.pdf")
        self.scan_file("Sep/3000001 - Signed (2).pdf")
        self.new_email(_email("Please send POD for 3000001"))
        self.assertEqual(self.current()["status"], tracking.WAITING_PBI)
        self.power_bi_answers({"rows": [self.row("3000001", sender="Example2 Company", scan=chosen)]})
        self.assertEqual(self.current()["status"], tracking.REPLIED)
        self.assertEqual(self.attached(), ["3000001 - Signed.pdf"])

    def test_several_files_and_no_scan_in_power_bi_go_to_the_team_with_a_note(self):
        self.scan_file("Sep/3000002 - Signed.pdf")
        self.scan_file("Sep/3000002 - Signed (2).pdf")
        self.new_email(_email("Please send POD for 3000002"))
        self.power_bi_answers({"rows": [self.row("3000002", sender="Example2 Company")]})
        self.assertEqual(self.current()["status"], tracking.WAITING_TEAM)
        self.assertIn("Power BI does not name its scan", self.handoff_files("teams")[0]["message"])

    def test_customer_a_connote_with_a_scan_path_uses_that_file_whatever_its_status(self):
        scan = self.scan_file("POD/Receiver/3000003 - Signed.pdf")
        self.customer_email("Please send POD for 3000003")
        self.power_bi_answers({"rows": [self.row("3000003", status="Unsigned", scan=scan)]})
        self.assertEqual(self.attached(), ["3000003 - Signed.pdf"])

    def test_customer_a_connote_without_a_scan_is_not_received_and_recorded(self):
        self.customer_email("Please send POD for 3000004")
        self.power_bi_answers({"rows": [self.row("3000004", status="Unsigned")]})
        self.assertEqual(self.current()["status"], tracking.REPLIED)
        self.assertIn("not received the Proof of Delivery for 3000004", self.reply.call_args.args[1])
        self.assertEqual([(i["action"], i["number"]) for i in self.handoff_files("outstanding")], [("add", "3000004")])

    def test_customer_b_neither_connote_nor_reference_is_not_in_the_system(self):
        self.customer_email("Please send POD for 3000005")
        self.power_bi_answers({"rows": []})
        self.assertEqual((self.current()["status"], self.current()["reason"]), (tracking.HELD, main.UNKNOWN_NUMBER))
        self.assertEqual(self.handoff_files("outstanding")[0]["row"]["Status"], "Not in The System")

    def test_customer_c_a_reference_without_scan_path_whose_connote_is_only_elsewhere_asks_the_team(self):
        self.scan_file("Sep/4000006.pdf")  # not in the customer's folder
        self.customer_email("Please send POD for our reference 80600006")
        self.power_bi_answers({"rows": [self.row("4000006", reference="80600006")]})
        self.assertEqual(self.current()["status"], tracking.WAITING_TEAM)
        self.assertIn("not in the Special folder", self.handoff_files("teams")[0]["message"])

    def test_customer_c_a_reference_whose_connote_is_in_its_folder_is_sent(self):
        self.scan_file("Special/Sep/4000007 - Signed.pdf")
        self.customer_email("Please send POD for our reference 80600007")
        self.power_bi_answers({"rows": [self.row("4000007", reference="80600007")]})
        self.assertEqual(self.attached(), ["4000007 - Signed.pdf"])

    def test_customer_d_a_reference_with_no_scan_anywhere_is_not_received(self):
        self.customer_email("Please send POD for our reference 80600008")
        self.power_bi_answers({"rows": [self.row("4000008", reference="80600008", status="Unsigned")]})
        self.assertIn("not received the Proof of Delivery for 80600008", self.reply.call_args.args[1])

    def test_customer_rows_of_another_sender_are_not_what_it_asked_for(self):
        scan = self.scan_file("POD/x/Delivery docket - Signed.pdf")  # named by Power BI only
        self.customer_email("Please send POD for 3000009")
        self.power_bi_answers({"rows": [self.row("3000009", sender="Example2 Company", scan=scan)]})
        self.assertEqual((self.current()["status"], self.current()["reason"]), (tracking.HELD, main.UNKNOWN_NUMBER))
        self.reply.assert_not_called()

    def test_a_connote_match_wins_over_a_reference_match(self):
        right = self.scan_file("POD/a/Docket A - Signed.pdf")  # found through Power BI only
        wrong = self.scan_file("POD/b/Docket B - Signed.pdf")
        self.new_email(_email("Please send POD for 3000010"))
        self.power_bi_answers({"rows": [self.row("9999999", reference="3000010", sender="Example2 Company", scan=wrong),
                                        self.row("3000010", sender="Example2 Company", scan=right)]})
        self.assertEqual(self.attached(), ["Docket A - Signed.pdf"])

    def test_a_reference_of_another_number_in_the_email_is_that_document_once(self):
        self.scan_file("Sep/3000011 - Signed.pdf")
        self.new_email(_email("Please send POD for 3000011, our reference 80600011"))
        self.power_bi_answers({"rows": [self.row("3000011", reference="80600011", sender="Example2 Company")]})
        self.assertEqual(self.current()["status"], tracking.REPLIED)
        self.assertEqual(self.attached(), ["3000011 - Signed.pdf"])  # once
        self.assertIn("for 3000011 (your reference 80600011)", self.reply.call_args.args[1])
        self.assertEqual(self.current()["state"]["same_as"], {"80600011": "3000011"})


class ReviewCardTest(FlowTest):
    """A Manual Review email gets a Teams card to review it there, as in the dashboard (product owner, D-048)."""

    def queued(self, body="Invoice 1234567 attached", model=None) -> dict:
        self.new_email(_email(body), model or FixedIntentModel("OTHER", 0.6))
        self.assertEqual(self.current()["status"], tracking.MANUAL_REVIEW)
        return self.handoff_files("teams")[-1]

    def answer(self, card_id: str, **answer):
        (self.handoff_dir / "teams_results").mkdir(parents=True, exist_ok=True)
        (self.handoff_dir / "teams_results" / f"{card_id}.json").write_text(
            json.dumps({"responder": "Ana", **answer}), encoding="utf-8")
        main._resume_manual(tracking.latest(tracking.MANUAL_REVIEW)[0], self.counters)

    def test_the_card_holds_the_email_and_both_number_fields(self):
        card = self.queued("Invoice 1234567 attached, " + "long text " * 80)
        self.assertEqual(card["request_id"], self.current()["state"]["review_card"])
        self.assertIn("Why: intent below threshold: maybe not a POD request.", card["message"])
        self.assertIn('Email: "Invoice 1234567 attached, long text', card["message"])
        self.assertIn('..."', card["message"])  # cut to the first 500 characters
        inputs = {item["id"]: item["value"] for item in card["card"]["body"] if item["type"] == "Input.Text"}
        self.assertEqual(inputs, {"connotes": "1234567", "references": ""})
        self.assertEqual([action.get("data", {}).get("choice") for action in card["card"]["actions"]],
                         ["pod", "other", "manual", None])
        self.assertEqual(card["card"]["actions"][-1]["title"], "Open the POD mailbox")
        self.assertTrue(card["card"]["actions"][-1]["url"].startswith("https://outlook.office.com/mail/"))

    def test_pod_request_with_a_connote_and_a_reference_goes_on_as_a_dashboard_review(self):
        card = self.queued()
        self.answer(card["request_id"], choice="pod", connotes="1234567", references="PO-1")
        self.assertEqual(self.current()["status"], tracking.REVIEWED)
        self.assertEqual(self.current()["reason"], "reviewed in Teams by Ana: POD request")
        self.assertEqual(self.current()["state"]["review"]["references"], ["PO-1"])
        labels = pd.read_excel(config.TRAINING_DATA_PATH, dtype=str)
        self.assertEqual(labels.at[0, "reference_number_manual"], "PO-1")
        main._resume_reviewed(self.current(), lambda: pod_files.build_index([self.scan]), self.counters)
        self.assertEqual(self.current()["status"], tracking.WAITING_PBI)  # 1234567 on the share, PO-1 asked

    def test_not_a_pod_request_sends_nothing(self):
        self.answer(self.queued()["request_id"], choice="other", connotes="", references="")
        self.assertEqual((self.current()["status"], self.current()["reason"]),
                         (tracking.REVIEWED_OTHER, "reviewed in Teams by Ana: not a POD request"))
        self.reply.assert_not_called()

    def test_handle_manually_stays_in_the_queue_without_a_card(self):
        self.answer(self.queued()["request_id"], choice="manual", connotes="", references="")
        self.assertEqual((self.current()["status"], self.current()["reason"]),
                         (tracking.MANUAL_REVIEW, "Ana chose to handle it manually"))
        self.assertNotIn("review_card", self.current()["state"])

    def test_an_answer_from_a_flow_3_without_the_references_field_is_not_used(self):
        self.answer(self.queued()["request_id"], choice="pod", connotes="1234567")  # no "references" key
        self.assertEqual(self.current()["status"], tracking.MANUAL_REVIEW)
        self.assertIn("Flow 3 does not pass the references field", self.current()["reason"])
        self.assertNotIn("review_card", self.current()["state"])

    def test_a_number_that_cannot_be_one_posts_a_new_card_saying_why(self):
        first = self.queued()["request_id"]
        self.answer(first, choice="pod", connotes="XY 12302", references="")
        self.assertEqual(self.current()["status"], tracking.MANUAL_REVIEW)
        self.assertNotEqual(self.current()["state"]["review_card"], first)
        self.assertIn("Not a number: 'XY 12302'", self.handoff_files("teams")[-1]["message"])

    def test_a_review_in_the_dashboard_first_withdraws_the_card(self):
        card = self.queued()["request_id"]
        main.record_review(self.current(), "OTHER", "", "")
        self.assertEqual(self.handoff_files("teams"), [])  # Flow 3 had not taken it: archived
        self.assertEqual(self.current()["status"], tracking.REVIEWED_OTHER)
        (self.handoff_dir / "teams_results").mkdir(parents=True, exist_ok=True)
        (self.handoff_dir / "teams_results" / f"{card}.json").write_text(
            json.dumps({"choice": "pod", "connotes": "1234567", "references": ""}), encoding="utf-8")
        self.assertEqual(tracking.latest(tracking.MANUAL_REVIEW), [])  # nothing reads the late answer

    def test_an_unknown_number_kept_on_the_review_card_is_not_asked_again(self):
        # The card's reason names it as not in Power BI; kept unchanged, it is confirmed (D-045).
        self.new_email(_email("Please send POD for 1234567 and 7777777"), FixedIntentModel(confidence=0.60))
        self.power_bi_answers({"rows": []})
        self.answer(self.current()["state"]["review_card"], choice="pod", connotes="1234567; 7777777", references="")
        main._resume_reviewed(self.current(), lambda: pod_files.build_index([self.scan]), self.counters)
        self.power_bi_answers({"rows": []})
        self.assertEqual(self.current()["status"], tracking.REPLIED)
        self.assertIn("Regarding 7777777, we were unable to find it", self.current()["state"]["reply"])

    def test_cold_start_posts_no_card(self):
        self.new_email(_email("Please send POD for 1234567"), cold_start=True)
        self.assertEqual(self.current()["status"], tracking.MANUAL_REVIEW)
        self.assertEqual(self.handoff_files("teams"), [])

    def test_a_card_that_never_reached_teams_is_posted_again_then_shown(self):
        first = self.queued()["request_id"]
        with mock.patch.object(main, "CARD_RESEND_AFTER", timedelta(seconds=-1)):
            for resend in range(1, main.CARD_RESENDS + 1):
                main._resume_manual(tracking.latest(tracking.MANUAL_REVIEW)[0], self.counters)
                self.assertNotEqual(self.current()["state"]["review_card"], first)
            main._resume_manual(tracking.latest(tracking.MANUAL_REVIEW)[0], self.counters)
        self.assertEqual(tracking.card_problems(tracking.MANUAL_REVIEW, self.current()["state"]),
                         {"review": "Flow 3 did not take the card from the teams folder"})
        with mock.patch.object(config, "LOCK_FILE", Path(self._tmp.name) / "run.lock"):
            self.assertEqual(main.send_cards_again([self.current()]), 1)
        self.assertEqual(tracking.card_problems(tracking.MANUAL_REVIEW, self.current()["state"]), {})


class AttachmentNumbersTest(FlowTest):
    """A POD request whose text has no number: its attachments, one source after another (D-044)."""

    def sources(self, *sources, not_read=()):
        self.attachments.return_value = {
            "sources": [{"source": name, "numbers": numbers} for name, numbers in sources],
            "not_read": list(not_read),
        }

    def test_the_next_source_is_tried_when_none_of_the_first_is_known(self):
        self.sources(("PDF file name", ["7777771"]), ("picture", ["1234567"]))
        self.new_email(_email("Please send the POD, see attached"))
        self.assertEqual(self.current()["state"]["number_source"], "PDF file name")
        self.power_bi_answers({"rows": []})  # the file name's number is nowhere
        self.assertEqual(self.current()["status"], tracking.REPLIED)  # the picture's is on the share
        self.assertEqual(self.current()["state"]["number_source"], "picture")
        self.assertEqual([path.name for path in self.reply.call_args.args[2]], ["1234567_a.pdf"])
        self.assertFalse([item for item in self.handoff_files("outstanding") if item["action"] == "add"])

    def test_unknown_numbers_beside_a_found_one_are_dropped_and_add_no_rows(self):
        self.sources(("PDF content", ["1234567", "7777772"]))
        self.new_email(_email("Please send the POD, see attached"))
        self.power_bi_answers({"rows": []})
        self.assertEqual(self.current()["status"], tracking.REPLIED)
        self.assertEqual([path.name for path in self.reply.call_args.args[2]], ["1234567_a.pdf"])
        self.assertNotIn("7777772", self.reply.call_args.args[1])
        self.assertFalse([item for item in self.handoff_files("outstanding") if item["action"] == "add"])

    def test_an_unsure_pod_request_stays_unsure_when_the_next_source_is_tried(self):
        self.sources(("PDF file name", ["7777771"]), ("picture", ["7777772"]))
        self.new_email(_email("Can you check this?"), FixedIntentModel(confidence=0.60))
        self.power_bi_answers({"rows": []})  # the file name's number is nowhere: the picture's is asked
        self.power_bi_answers({"rows": []})  # nowhere either
        self.assertEqual(self.current()["status"], tracking.MANUAL_REVIEW)  # not held (D-044)
        self.assertNotIn("correction_card", self.current()["state"])

    def test_the_last_source_unknown_holds_without_not_in_the_system_rows(self):
        self.sources(("PDF file name", ["7777773"]))
        self.new_email(_email("Please send the POD, see attached"))
        self.power_bi_answers({"rows": []})
        self.assertEqual((self.current()["status"], self.current()["reason"]), (tracking.HELD, main.UNKNOWN_NUMBER))
        self.assertIn("read from the PDF file name", self.handoff_files("teams")[0]["message"])
        self.assertEqual(self.handoff_files("outstanding"), [])

    def test_attachments_over_the_limits_with_no_number_are_held_for_a_person(self):
        self.sources(not_read=["big.pdf: over 10 MB"])
        self.new_email(_email("Please send the POD, see attached"))
        self.assertEqual((self.current()["status"], self.current()["reason"]), (tracking.HELD, main.ATTACHMENTS_NOT_READ))
        self.assertIn("big.pdf: over 10 MB", self.handoff_files("teams")[0]["message"])

    def test_numbers_in_the_text_mean_the_attachments_are_never_read(self):
        self.new_email(_email("Please send POD for 1234567"))
        self.attachments.assert_not_called()

    # D-056: when every number of the text is not in Power BI, the attachments are read.
    def pbi(self, data):
        with mock.patch.object(model_registry, "load_active", return_value=None):
            self.power_bi_answers(data)

    def test_the_right_connote_in_an_attachment_replaces_a_wrong_number_in_the_text(self):
        self.sources(("PDF content", ["1234567"]))
        self.new_email(_email("Please send the POD for invoice 7777779"))
        self.attachments.assert_not_called()  # the text's number goes first
        self.pbi({"rows": []})  # not in Power BI: the attachments are read, 1234567 is on the share
        self.attachments.assert_called_once()
        self.assertEqual(self.current()["status"], tracking.REPLIED)
        self.assertEqual([path.name for path in self.reply.call_args.args[2]], ["1234567_a.pdf"])
        self.assertNotIn("7777779", self.reply.call_args.args[1])  # dropped: no "not in our system" line
        self.assertFalse([item for item in self.handoff_files("outstanding") if item["action"] == "add"])
        self.assertFalse([m for m in self.handoff_files("teams") if m["kind"] == "choice"])  # no card

    def test_attachments_that_do_not_help_end_as_before_with_the_texts_numbers(self):
        self.sources(("picture", ["7777778"]))
        self.new_email(_email("Please send the POD for 7777779"))
        self.pbi({"rows": []})  # the text's number: not in Power BI; the picture's is asked
        self.assertEqual(self.current()["state"]["number_source"], "picture")
        self.pbi({"rows": []})  # not in Power BI either
        current = self.current()
        self.assertEqual((current["status"], current["reason"]), (tracking.HELD, main.UNKNOWN_NUMBER))
        self.assertEqual(current["numbers"], "7777779")
        card = self.handoff_files("teams")[-1]
        inputs = [item for item in card["card"]["body"] if item["type"] == "Input.Text"]
        self.assertEqual(inputs[0]["value"], "7777779")  # the text's number is asked, as before
        self.assertIn("Its attachments were read too", card["message"])
        added = [(item["number"], item["row"]["Status"]) for item in self.handoff_files("outstanding")
                 if item["action"] == "add"]
        self.assertEqual(added, [("7777779", "Not in The System")])

    def test_attachments_are_not_read_when_a_number_of_the_text_is_known(self):
        self.sources(("PDF content", ["2345678"]))
        self.new_email(_email("Please send POD for 1234567 and 7777779"))  # 1234567 on the share
        self.pbi({"rows": []})
        self.attachments.assert_not_called()
        self.assertEqual(self.current()["status"], tracking.HELD)

    def test_attachments_are_not_read_for_a_number_not_received_yet(self):
        self.sources(("PDF content", ["2345678"]))
        self.new_email(_email("Please send POD for 7777779"))
        self.pbi({"rows": [{"Connote": "7777779", "Reference": "", "POD Status": "Unsigned", "Sender": "Other"}]})
        # Power BI knows it: a right number whose POD is not here yet
        self.attachments.assert_not_called()
        self.assertEqual(self.current()["status"], tracking.REPLIED)

    def test_numbers_a_person_saved_are_not_second_guessed_by_the_attachments(self):
        self.sources(("PDF content", ["1234567"]))
        self.new_email(_email("Please send the POD for 7777779"))
        self.pbi({"rows": []})  # attachments read: none of those numbers was a person's
        self.attachments.reset_mock()
        self.sources()
        tracking.record(self.current(), tracking.MANUAL_REVIEW, reason="test", state={})
        main.record_review(self.current(), "POD_REQUEST", "8888889", "")
        main._resume_reviewed(self.current(), lambda: pod_files.build_index([self.scan]), self.counters)
        self.pbi({"rows": []})
        self.attachments.assert_not_called()
        self.assertEqual(self.current()["status"], tracking.HELD)

    def test_an_unsure_request_whose_attachments_do_not_help_goes_to_manual_review(self):
        self.sources(("picture", ["7777778"]))
        self.new_email(_email("Can you check 7777779?"), FixedIntentModel(confidence=0.60))
        self.pbi({"rows": []})
        self.pbi({"rows": []})
        self.assertEqual(self.current()["status"], tracking.MANUAL_REVIEW)
        self.assertEqual(self.handoff_files("outstanding"), [])


class SenderFolderTest(FlowTest):
    def test_config_maps_a_sender_domain_to_its_folder(self):
        with mock.patch.dict(os.environ, {"POD_SENDER_FOLDERS": "example3.com=Customer; example4.com = Sub\\Dir"}):
            self.assertEqual(config.sender_folder("ops@example3.com"), "Customer")
            self.assertIsNone(config.sender_folder("a@au.example3.com"))  # the domain after the @ exactly (D-057)
            self.assertEqual(config.sender_folder("x@example4.com"), "Sub\\Dir")
            self.assertIsNone(config.sender_folder("x@notexample3.com"))
        for bad in ("example3.com", "<customer domain>=<folder under each year folder>"):
            with mock.patch.dict(os.environ, {"POD_SENDER_FOLDERS": bad}):
                with self.assertRaisesRegex(config.ConfigError, "POD_SENDER_FOLDERS"):
                    config.sender_folder("")

    def test_a_scan_only_outside_the_customer_folder_goes_to_the_team(self):
        with mock.patch.dict(os.environ, {"POD_SENDER_FOLDERS": "example1.com=Customer"}):
            self.new_email({"entry_id": "S1", "sender_email": "ops@example1.com", "subject": "POD 2345678", "body": "Please send"})
            self.assertEqual(self.current()["status"], tracking.WAITING_PBI)  # Power BI first (D-044)
            self.power_bi_answers({"rows": []})
        latest = tracking.latest()[0]
        self.assertEqual(latest["status"], tracking.WAITING_TEAM)
        self.reply.assert_not_called()
        card = json.loads(next((self.handoff_dir / "teams").glob("*.json")).read_text(encoding="utf-8"))
        self.assertIn("not in the Customer folder", card["message"])


class ReviewTest(FlowTest):
    """A person decides an email the intent model was unsure of (bug report item 5)."""

    def setUp(self):
        super().setUp()
        self.email = {
            "entry_id": "R1", "sender_email": "ops@example1.com", "subject": "Can you check?",
            "body": "Please send the POD", "received_time": "2026-10-14 09:00:00",
        }
        tracking.record(self.email, tracking.MANUAL_REVIEW, intent="POD_REQUEST", confidence=0.8,
                        reason="intent confidence below threshold", numbers=["1234567"])
        self.entry = tracking.latest(tracking.MANUAL_REVIEW)[0]

    def resume(self):
        for event in tracking.latest(tracking.REVIEWED):
            main._resume_reviewed(event, lambda: pod_files.build_index([self.scan]), self.counters)

    def test_reviewed_pod_request_with_a_file_on_the_share_is_replied(self):
        status, review = main.record_review(self.entry, "POD_REQUEST", "1234567", "")
        self.assertEqual((status, review["connotes"]), (tracking.REVIEWED, ["1234567"]))
        self.reply.assert_not_called()  # the dashboard only records; the agent continues
        self.resume()
        self.reply.assert_called_once()
        self.assertEqual(Path(self.reply.call_args.args[2][0]).name, "1234567_a.pdf")
        latest = tracking.latest()[0]
        self.assertEqual(latest["status"], tracking.REPLIED)
        self.assertEqual(latest["state"]["review"]["connotes"], ["1234567"])

    def test_reviewed_number_not_on_the_share_asks_power_bi_in_the_usual_format(self):
        main.record_review(self.entry, "POD_REQUEST", "9999999", "PO-1; 9999999")
        self.resume()
        latest = tracking.latest()[0]
        self.assertEqual(latest["status"], tracking.WAITING_PBI)
        request = json.loads(next((self.handoff_dir / "pbi_requests").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(set(request), {"request_id", "entry_id", "numbers", "scan_missing_numbers", "query"})
        self.assertEqual((request["entry_id"], request["numbers"]), ("R1", ["9999999", "PO-1"]))
        self.assertEqual(request["scan_missing_numbers"], ["9999999", "PO-1"])
        self.assertIn("EVALUATE", request["query"])
        # The next cycle does not ask again: the email is no longer Reviewed.
        self.resume()
        self.assertEqual(len(list((self.handoff_dir / "pbi_requests").glob("*.json"))), 1)

    def test_reviewed_as_other_sends_nothing_and_leaves_the_queue(self):
        self.assertEqual(main.record_review(self.entry, "OTHER", "", "")[0], tracking.REVIEWED_OTHER)
        self.resume()
        self.reply.assert_not_called()
        self.assertFalse((self.handoff_dir / "pbi_requests").exists())
        self.assertNotIn(tracking.REVIEWED_OTHER, (*tracking.OPEN_STATUSES, *tracking.NEEDS_PEOPLE))

    def test_saving_twice_records_nothing_more(self):
        main.record_review(self.entry, "POD_REQUEST", "1234567", "")
        with self.assertRaisesRegex(ValueError, "no longer waiting"):
            main.record_review(self.entry, "POD_REQUEST", "1234567", "")
        self.assertEqual([e["status"] for e in tracking.history("R1")], [tracking.MANUAL_REVIEW, tracking.REVIEWED])

    def test_a_pod_request_saved_without_numbers_is_continued_with_its_attachments(self):
        # The numbers are only in an Excel attachment (D-046).
        status, review = main.record_review(self.entry, "POD_REQUEST", " ; ", "")
        self.assertEqual((status, review["connotes"]), (tracking.REVIEWED, []))
        self.assertEqual(tracking.latest()[0]["reason"], "reviewed in the dashboard: POD request, numbers from its attachments")
        self.attachments.return_value = {"sources": [{"source": "spreadsheet", "numbers": ["1234567"]}], "not_read": []}
        with mock.patch.object(model_registry, "load_active", return_value=None):
            self.resume()
        latest = tracking.latest()[0]
        self.assertEqual((latest["status"], latest["state"]["number_source"]), (tracking.REPLIED, "spreadsheet"))
        self.assertEqual(Path(self.reply.call_args.args[2][0]).name, "1234567_a.pdf")

    def test_a_pod_request_saved_without_numbers_and_none_in_its_attachments_comes_back(self):
        main.record_review(self.entry, "POD_REQUEST", "", "")
        with mock.patch.object(model_registry, "load_active", return_value=None):
            self.resume()
        latest = tracking.latest()[0]
        self.assertEqual((latest["status"], latest["reason"]), (tracking.MANUAL_REVIEW, "POD request without a number"))
        self.reply.assert_not_called()

    def test_a_held_email_with_its_numbers_corrected_is_replied_and_the_wrong_row_removed(self):
        # Held: an invoice number was read as a connote and added as "Not in The System" (D-035).
        tracking.record(self.email, tracking.HELD, numbers=["20260407", "1234567"], intent="POD_REQUEST",
                        reason="number not on the share and not in Power BI",
                        state={"unknown": ["20260407"], "outstanding_add_numbers": ["20260407"]})
        held = tracking.latest(tracking.HELD)[0]
        self.assertEqual(main.record_review(held, "POD_REQUEST", "1234567", "")[0], tracking.REVIEWED)
        self.resume()
        self.reply.assert_called_once()
        self.assertEqual(Path(self.reply.call_args.args[2][0]).name, "1234567_a.pdf")
        outstanding = sorted(path.name for path in (self.handoff_dir / "outstanding").glob("*.json"))
        rid = handoff.request_id("R1")
        self.assertEqual(outstanding, [f"{rid}_remove_1234567.json", f"{rid}_remove_20260407.json"])
        # The original numbers stay in the history next to the corrected ones.
        numbers = [(e["status"], e["numbers"]) for e in tracking.history("R1")]
        self.assertIn((tracking.HELD, "20260407;1234567"), numbers)
        self.assertIn((tracking.REVIEWED, "1234567"), numbers)

    def test_typed_numbers_that_cannot_be_a_number_are_refused_and_any_count_is_accepted(self):
        for typed in ("XY 12302", "abc", "1234567 please", "12/34/56/78/90/12345678"):
            with self.subTest(typed=typed), self.assertRaisesRegex(ValueError, "Not a number"):
                main.record_review(self.entry, "POD_REQUEST", typed, "")
        main.check_numbers(["1234567", "XY62302", "PO-1", "MN675418-3"])  # accepted
        self.assertEqual(tracking.latest()[0]["status"], tracking.MANUAL_REVIEW)  # nothing recorded
        main.check_numbers([str(1000000 + n) for n in range(50)])  # no limit on how many (product owner, D-047)

    def test_a_review_while_a_cycle_runs_waits_and_then_saves_nothing(self):
        with mock.patch.object(config, "LOCK_FILE", Path(self._tmp.name) / "run.lock"), \
                mock.patch.object(main, "DASHBOARD_LOCK_WAIT_SECONDS", 0):
            with main._run_lock():
                with self.assertRaises(main.AlreadyRunning):
                    main.record_review(self.entry, "POD_REQUEST", "1234567", "")
                with self.assertRaises(main.AlreadyRunning):
                    main.mark_resolved([self.entry])
            self.assertEqual(tracking.latest()[0]["status"], tracking.MANUAL_REVIEW)  # the cycle's view stands
            self.assertEqual(main.mark_resolved([self.entry]), 1)  # once it is done
        self.assertEqual(tracking.latest()[0]["status"], tracking.RESOLVED)

    def test_numbers_typed_by_a_person_are_split_and_deduplicated(self):
        self.assertEqual(main.split_numbers("1234567; 7654321,\n1234567 ;"), ["1234567", "7654321"])


class ProcessAgainTest(FlowTest):
    def setUp(self):
        super().setUp()
        tmp = Path(self._tmp.name)
        self.processed = tmp / "processed_ids.txt"
        self.processed.write_text("E1\nE2\nE3\n", encoding="utf-8")
        for patcher in (
            mock.patch.object(config, "PROCESSED_IDS_PATH", self.processed),
            mock.patch.object(config, "LOCK_FILE", tmp / "run.lock"),
        ):
            patcher.start()
        tracking.record({"entry_id": "E1", "subject": "POD 1234567"}, tracking.MANUAL_REVIEW, reason="low confidence")
        tracking.record({"entry_id": "E2"}, tracking.REPLIED)

    def test_the_email_is_read_again_and_its_history_is_kept(self):
        entry = tracking.latest(tracking.MANUAL_REVIEW)[0]
        self.assertEqual(main.process_again([entry]), 1)
        self.assertEqual(self.processed.read_text(encoding="utf-8"), "E2\nE3\n")
        history = [event["status"] for event in tracking.history("E1")]
        self.assertEqual(history, [tracking.MANUAL_REVIEW, tracking.RESET])
        # Out of the review queue, and the next run reads it again.
        self.assertNotIn(tracking.RESET, (*tracking.OPEN_STATUSES, *tracking.NEEDS_PEOPLE))
        self.assertNotIn("E1", outlook_reader.load_processed_ids(self.processed))

    def test_a_replied_email_is_never_processed_again(self):
        replied = tracking.latest(tracking.REPLIED)[0]
        with self.assertRaisesRegex(ValueError, "Replied"):
            main.process_again([tracking.latest(tracking.MANUAL_REVIEW)[0], replied])
        # Nothing changed for either email.
        self.assertEqual(self.processed.read_text(encoding="utf-8"), "E1\nE2\nE3\n")
        self.assertEqual(len(tracking.history("E1")), 1)

    def test_nothing_changes_while_a_cycle_runs(self):
        entry = tracking.latest(tracking.MANUAL_REVIEW)[0]
        with main._run_lock():
            with self.assertRaises(main.AlreadyRunning):
                main.process_again([entry])
        self.assertIn("E1", outlook_reader.load_processed_ids(self.processed))
        self.assertEqual(len(tracking.history("E1")), 1)


class RunTest(FlowTest):
    def test_stop_file_prevents_everything(self):
        stop = Path(self._tmp.name) / "STOP"
        stop.write_text("", encoding="utf-8")
        with mock.patch.object(config, "STOP_FILE", stop), mock.patch.object(
            config, "LOG_PATH", Path(self._tmp.name) / "run_log.txt"
        ), mock.patch.object(outlook_reader, "connect_outlook", side_effect=AssertionError("must not read mail")):
            main.run()

    def test_missing_mailbox_address_stops_before_reading_mail(self):
        for address in ("", "pod@<company domain>"):
            with mock.patch.object(config, "STOP_FILE", Path(self._tmp.name) / "no-stop"), mock.patch.object(
                config, "POD_SHARED_MAILBOX_ADDRESS", address
            ), mock.patch.object(outlook_reader, "connect_outlook", side_effect=AssertionError("must not read mail")):
                with self.assertRaisesRegex(config.ConfigError, "POD_SHARED_MAILBOX_ADDRESS"):
                    main.run()

    def test_second_run_while_one_is_running_does_nothing(self):
        lock = Path(self._tmp.name) / "run.lock"
        with mock.patch.object(config, "LOCK_FILE", lock), mock.patch.object(
            config, "STOP_FILE", Path(self._tmp.name) / "no-stop"
        ), mock.patch.object(config, "LOG_PATH", Path(self._tmp.name) / "run_log.txt"), mock.patch.object(
            outlook_reader, "connect_outlook", side_effect=AssertionError("must not read mail")
        ):
            with main._run_lock(), mock.patch.object(main, "LOCK_WAIT_SECONDS", 0):
                main.run()
        line = (Path(self._tmp.name) / "run_log.txt").read_text(encoding="utf-8")
        self.assertIn("skipped_already_running", line)
        self.assertRegex(line, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC\+8 ")  # local time (UTC+8)


class LockAndLogTest(FlowTest):
    """Intake waits for a resume run to finish; resume runs that did nothing leave no log line."""

    def setUp(self):
        super().setUp()
        tmp = Path(self._tmp.name)
        self.log = tmp / "run_log.txt"
        for patcher in (
            mock.patch.object(config, "LOCK_FILE", tmp / "run.lock"),
            mock.patch.object(config, "STOP_FILE", tmp / "no-stop"),
            mock.patch.object(config, "LOG_PATH", self.log),
            mock.patch.object(main, "LOCK_WAIT_SECONDS", 5),
            mock.patch.object(main, "LOCK_RETRY_SECONDS", 0.05),
        ):
            patcher.start()
        self.cycle = mock.patch.object(main, "_cycle", return_value=0).start()

    def lines(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def test_intake_waits_for_the_lock_and_then_runs(self):
        held, release = threading.Event(), threading.Event()

        def hold():
            with main._run_lock():
                held.set()
                release.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        held.wait(5)
        threading.Timer(0.3, release.set).start()  # the resume run ends after a moment
        main.run(process_new=True, resume=False)
        holder.join()
        self.cycle.assert_called_once()
        self.assertNotIn("skipped_already_running", "".join(self.lines()))

    def test_resume_skips_at_once_and_quietly_while_the_lock_is_held(self):
        with main._run_lock(), mock.patch.object(main.time, "sleep") as sleep:
            main.run(process_new=False, resume=True)
        sleep.assert_not_called()
        self.cycle.assert_not_called()
        self.assertEqual(self.lines(), [])

    def test_intake_gives_up_after_the_wait_and_says_so(self):
        with main._run_lock(), mock.patch.object(main, "LOCK_WAIT_SECONDS", 0.2):
            main.run(process_new=True, resume=False)
        self.cycle.assert_not_called()
        self.assertIn("skipped_already_running", self.lines()[-1])

    def test_an_idle_resume_run_is_not_logged_but_a_busy_one_and_intake_are(self):
        main.run(process_new=False, resume=True)
        self.assertEqual(self.lines(), [])  # nothing happened
        self.cycle.side_effect = lambda counters, **_: counters.update({"pilot sent unedited": 1}) or 0
        main.run(process_new=False, resume=True)
        self.assertIn("pilot sent unedited=1", self.lines()[-1])
        self.cycle.side_effect = None
        main.run(process_new=True, resume=False)
        self.assertEqual(len(self.lines()), 2)  # intake always leaves a line


class ExtractNumbersTest(unittest.TestCase):
    @mock.patch.dict(os.environ, {"POD_CONNOTE_PREFIXES": "AB;XY;KL"})  # the prefixes are set in .env (D-060)
    def test_regex_is_used_without_a_model(self):
        numbers = number_classifier.extract_numbers(
            None, "POD 1234567", "Also AB-10234 please. Regards, 7654321"
        )
        # Text after "Regards" is a signature, not a request.
        self.assertEqual(numbers, ["1234567", "AB-10234"])

    def test_forwarded_numbers_are_used_when_the_new_message_has_none(self):
        body = (
            "Hi, can you send the POD for these?\nKind regards\nJane\n"
            "From: Customer\nSent: Monday\nTo: Jane\nSubject: missing POD\n"
            "1234567\n2345678"
        )
        latest = "Hi, can you send the POD for these?\nKind regards\nJane"
        self.assertEqual(
            number_classifier.extract_numbers(None, "Fw: missing POD", latest, full_body=body),
            ["1234567", "2345678"],
        )
        # Without the full email, text after the sign-off in the body is read.
        self.assertEqual(
            number_classifier.extract_numbers(None, "Fw: missing POD", body.replace("From:", "Fwd")),
            ["1234567", "2345678"],
        )

    def test_forwarded_numbers_are_ignored_when_the_new_message_has_one(self):
        latest = "Please send POD 1234567\nRegards"
        full = latest + "\nFrom: A\nSent: B\nTo: C\nOld thread about 7654321"
        self.assertEqual(
            number_classifier.extract_numbers(None, "POD", latest, full_body=full),
            ["1234567"],
        )

    def test_no_number_anywhere_returns_nothing(self):
        self.assertEqual(
            number_classifier.extract_numbers(None, "Thanks", "Thanks!\nRegards", full_body="Thanks!"),
            [],
        )

    def test_crf_numbers_are_used_when_it_finds_some(self):
        with mock.patch("app.number_crf.extract", return_value=["PO40001"]) as extract:
            numbers = number_classifier.extract_numbers(object(), "POD", "Ref PO40001 and 1234567", 0.5)
        self.assertEqual(numbers, ["PO40001"])
        self.assertEqual(extract.call_args.args[3], 0.5)

    def test_crf_finding_nothing_falls_back_to_regex(self):
        with mock.patch("app.number_crf.extract", return_value=[]):
            numbers = number_classifier.extract_numbers(object(), "POD", "Please send 1234567", 0.5)
        self.assertEqual(numbers, ["1234567"])


if __name__ == "__main__":
    unittest.main()
