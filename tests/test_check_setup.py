"""The setup check reports wrong settings and files a flow does not take, and changes nothing."""

import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from app import check_setup, config


class CheckSetupTest(unittest.TestCase):
    def run_check(self, env: dict) -> str:
        output = StringIO()
        with mock.patch.dict(os.environ, env), redirect_stdout(output):
            check_setup.main()
        return output.getvalue()

    def test_wrong_settings_and_files_flow_3_left_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            stale = Path(tmp) / "teams" / "abc-0.json"
            stale.parent.mkdir()
            stale.write_text("{}", encoding="utf-8")
            hour_ago = time.time() - 3600
            os.utime(stale, (hour_ago, hour_ago))
            with mock.patch.object(config, "HANDOFF_DIR", Path(tmp)):
                text = self.run_check({
                    "POD_PBI_COLUMNS": "Sender=Main[Sender];POD Status=Main[POD Status]",
                    "POD_PBI_STATUS_COLUMN": "POD Status",
                    "POD_PBI_LINKS": "",
                    "POD_PBI_TABLE": "Main",
                    "POD_PBI_SENDER_CHECK": "example1.com=example1",
                    "POD_PBI_SENDER_COLUMN": "Sender",
                    "POD_PBI_SCAN_PATH_COLUMN": "Scan Path",
                    "POD_MANUAL_UPLOAD_DIR": str(config.PROJECT_DIR / "manual"),
                    "POD_MANUAL_UPLOAD_URL": "",
                })
            self.assertTrue(stale.exists())  # read only
        self.assertIn("[OK] POD_PBI_SENDER_COLUMN=Sender", text)
        self.assertIn("[!!] POD_PBI_SCAN_PATH_COLUMN=Scan Path is not a name in POD_PBI_COLUMNS", text)
        self.assertIn("[!!] POD_MANUAL_UPLOAD_DIR", text)  # inside the project
        self.assertIn("1 for over 30 minutes", text)
        self.assertIn("Flow 3 does not take them", text)

    def test_a_sharepoint_list_link_is_flagged_as_not_a_folder_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = self.run_check({
                "POD_PBI_SENDER_CHECK": "",
                "POD_MANUAL_UPLOAD_DIR": tmp,
                "POD_MANUAL_UPLOAD_URL": "https://x.sharepoint.com/sites/POD/Shared Documents/Forms/AllItems.aspx?id=%2F",
            })
        self.assertIn("use the folder's direct path", text)
        self.assertIn("[OK] the agent can write there", text)


if __name__ == "__main__":
    unittest.main()
