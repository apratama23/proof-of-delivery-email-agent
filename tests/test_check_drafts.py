"""The read-only drafts check prints where drafts go and changes nothing (D-032)."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app import check_drafts, config, outlook_reader, tracking


class Folders:
    def __init__(self, items):
        self.items = items
        self.Count = len(items)

    def Item(self, index):
        return self.items[index - 1]


class CheckDraftsTest(unittest.TestCase):
    def test_it_shows_the_agent_s_drafts_folder_and_where_the_last_draft_is(self):
        pod_drafts = SimpleNamespace(Name="Drafts", FolderPath="\\\\POD\\Drafts")
        store = SimpleNamespace(
            DisplayName="POD",
            GetRootFolder=lambda: SimpleNamespace(Folders=Folders([pod_drafts])),
            GetDefaultFolder=lambda number: SimpleNamespace(FolderPath="\\\\me@example.com\\Drafts"),
        )
        draft = SimpleNamespace(Parent=SimpleNamespace(FolderPath="\\\\me@example.com\\Drafts"), Sent=False)
        namespace = mock.Mock(DefaultStore=SimpleNamespace(DisplayName="me@example.com"))
        namespace.GetItemFromID.return_value = draft
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(config, "TRACKING_DB_PATH", Path(tmp) / "t.db"), \
                mock.patch.object(outlook_reader, "connect_outlook", return_value=namespace), \
                mock.patch.object(outlook_reader, "get_folder",
                                  return_value=SimpleNamespace(FolderPath="\\\\POD", Store=store)):
            tracking.record({"entry_id": "E1", "subject": "POD 1234567"}, tracking.REPLIED,
                            state={"draft_entry_id": "D1", "draft_store_id": "S", "drafted_at": "2026-10-20T01:00:00+00:00"})
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                check_drafts.main()
        text = out.getvalue()
        self.assertIn("Drafts the agent uses now: \\\\POD\\Drafts", text)
        self.assertIn("used before): \\\\me@example.com\\Drafts", text)  # the cause, if it shows up here
        self.assertIn("it is in: \\\\me@example.com\\Drafts", text)
        namespace.GetItemFromID.assert_called_once_with("D1", "S")  # read where it is, nothing else


if __name__ == "__main__":
    unittest.main()
