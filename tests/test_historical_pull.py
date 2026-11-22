"""Training rows from the mailbox look like what the live agent reads."""

import contextlib
import io
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pandas as pd

from app import config, historical_pull, outlook_reader


class CollectItemTest(unittest.TestCase):
    def test_internal_sender_is_stored_as_smtp_address(self):
        user = SimpleNamespace(PrimarySmtpAddress="j.smith@example6.com")
        item = SimpleNamespace(
            EntryID="E1",
            ReceivedTime=datetime.now(),
            Subject="POD 1234567",
            Body="Please send the POD",
            To="POD",
            SenderEmailType="EX",
            SenderEmailAddress="/O=EXCHANGELABS/OU=EXCHANGE ADMINISTRATIVE GROUP/CN=RECIPIENTS/CN=jsmith",
            Sender=SimpleNamespace(GetExchangeUser=lambda: user),
        )
        record = historical_pull._collect_item(item, SimpleNamespace(Name="Inbox"), datetime.now() - timedelta(days=1), {})
        self.assertEqual(record["sender_email"], "j.smith@example6.com")


def _mail(entry_id: str, days_ago: int):
    return SimpleNamespace(
        EntryID=entry_id, ReceivedTime=datetime.now() - timedelta(days=days_ago), Subject=f"POD {entry_id}",
        Body="Please send the POD for 1234567", To="POD", SenderEmailType="SMTP", SenderEmailAddress="ops@example1.com",
    )  # the pull keeps emails with a number


class FakeTable:
    """Outlook's Table: sorted, with the columns asked for, read row by row."""

    def __init__(self, emails):
        self.rows, self.columns = list(emails), []
        self.Columns = SimpleNamespace(RemoveAll=self.columns.clear, Add=self.columns.append)

    def Sort(self, key, descending=False):
        self.rows.sort(key=lambda email: email.ReceivedTime, reverse=descending)

    @property
    def EndOfTable(self):
        return not self.rows

    def GetNextRow(self):
        email = self.rows.pop(0)
        # Outlook's table gives a short entry ID, not the email's own long one.
        value = lambda column: f"short-{email.EntryID}" if column == "EntryID" else getattr(email, column)
        return SimpleNamespace(GetValues=lambda: tuple(value(column) for column in self.columns))


class FakeFolder:
    """A folder of emails, as Outlook keeps them: unsorted."""

    def __init__(self, name, emails, kind=0):
        self.Name, self.emails, self.DefaultItemType = name, emails, kind
        self.Folders = SimpleNamespace(Count=0, Item=lambda index: None)

    def GetTable(self):
        if self.DefaultItemType != 0:
            raise AssertionError("a calendar is never read")
        return FakeTable(self.emails)


class PullTest(unittest.TestCase):
    """The pull takes each email once, the newest first (fix plan)."""

    def pull(self, emails, max_emails, existing=None, show_progress=False):
        inbox = FakeFolder("Inbox", emails)
        calendar = FakeFolder("Calendar", [], kind=1)
        mailbox = SimpleNamespace(Name="POD", Store=SimpleNamespace(StoreID="S"),
                                  Folders=SimpleNamespace(Count=2, Item=lambda index: [inbox, calendar][index - 1]))
        by_id = {}

        def get_item(entry_id, store_id):
            if not by_id:  # an email arrives between the two passes: every place shifts
                emails.insert(0, _mail("NEW", 0))
            by_id.update({f"short-{email.EntryID}": email for email in emails})
            return by_id[entry_id]  # found by the table's short ID

        namespace = SimpleNamespace(GetItemFromID=get_item)
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(config, "POD_SHARED_MAILBOX_ADDRESS", "pod@example.com"), \
                mock.patch.object(outlook_reader, "connect_outlook", return_value=namespace), \
                mock.patch.object(outlook_reader, "get_folder", return_value=mailbox), \
                mock.patch.object(config, "MASTER_NUMBER_DATA_PATH", Path(folder) / "none.xlsx"):
            output = Path(folder) / "training_data.xlsx"
            if existing is not None:
                existing.to_excel(output, index=False)
            historical_pull.pull(output_path=output, show_progress=show_progress, max_emails=max_emails)
            return pd.read_excel(output, dtype=str)

    def test_the_newest_emails_are_taken_each_once_even_when_the_folder_changes(self):
        # Oldest first in the folder, as Outlook keeps them unsorted; the same email
        # read twice in the first pass, as when places shift while it is walked.
        old, middle, newest = _mail("OLD", 30), _mail("MID", 10), _mail("TOP", 1)
        data = self.pull([old, middle, newest, newest], max_emails=2)
        self.assertEqual(data["entry_id"].tolist(), ["TOP", "MID"])

    def test_each_folder_is_named_before_it_is_read(self):
        # D-059: so a slow folder shows where the pull is.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.pull([_mail("TOP", 1), _mail("OLD", 400)], max_emails=5, show_progress=True)
        text = out.getvalue()
        self.assertIn("Scanning folder 2/3: Inbox...", text)
        self.assertIn("1 email(s) from the last year", text)  # the year-old one is not counted
        self.assertNotIn("Calendar", text)  # not an email folder: not read

    def test_a_workbook_holding_an_email_twice_is_merged_into_one_row_keeping_its_labels(self):
        existing = pd.DataFrame({
            "entry_id": ["TOP", "TOP", "KEEP"],
            "intent_label": ["POD_REQUEST", "", "OTHER"],
            "connotes_manual": ["", "1234567", ""],
        })
        data = self.pull([_mail("TOP", 1)], max_emails=1, existing=existing)
        self.assertEqual(sorted(data["entry_id"]), ["KEEP", "TOP"])
        top = data.set_index("entry_id").loc["TOP"]
        self.assertEqual((top["intent_label"], top["connotes_manual"]), ("POD_REQUEST", "1234567"))

    def test_rows_of_one_email_with_different_labels_stop_the_pull_and_write_nothing(self):
        existing = pd.DataFrame({"entry_id": ["TOP", "TOP"], "intent_label": ["POD_REQUEST", "OTHER"]})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "training_data.xlsx"
            existing.to_excel(path, index=False)
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "rows for email TOP with different intent_label"):
                historical_pull._merge_training_records([], pd.read_excel(path, dtype=str))
            self.assertEqual(path.read_bytes(), before)


class MasterLookupTest(unittest.TestCase):
    def test_master_list_is_read_from_excel_with_leading_zeros(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "master.xlsx"
            pd.DataFrame({"DoNumber": ["0123456"], "Reference": ["PO1, AB-2"]}).to_excel(path, index=False)
            lookup = historical_pull._load_master_number_lookup(path)
        self.assertEqual(lookup, {"0123456": "CONNOTE", "PO1": "REFERENCE", "AB-2": "REFERENCE"})


if __name__ == "__main__":
    unittest.main()
