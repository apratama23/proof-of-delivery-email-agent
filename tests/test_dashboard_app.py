"""Smoke test of the dashboard: every tab renders with data, and the review pop-up opens."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
from streamlit.testing.v1 import AppTest

from app import config, jobs, tracking

DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard" / "app.py"


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        for name, value in {
            "TRACKING_DB_PATH": tmp / "tracking.db",
            "TRAINING_DATA_PATH": tmp / "training_data.xlsx",
            "MODELS_DIR": tmp / "models",
            "HANDOFF_DIR": tmp / "handoff",
            "STOP_FILE": tmp / "STOP",
            "LOCK_FILE": tmp / "run.lock",
            "LOG_PATH": tmp / "run_log.txt",
            "PROCESSED_IDS_PATH": tmp / "processed_ids.txt",
            # Settings read and write <PROJECT_DIR>/.env: keep the real one out of reach.
            "PROJECT_DIR": tmp,
        }.items():
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        email = {"entry_id": "E1", "subject": "Can you check 1234567?", "sender_email": "ops@example1.com",
                 "body": "Please send the POD for 1234567", "received_time": "2026-10-14 09:00:00"}
        tracking.record(email, tracking.MANUAL_REVIEW, intent="POD_REQUEST", confidence=0.8, numbers=["1234567"],
                        reason="intent confidence below threshold")
        tracking.record({**email, "entry_id": "E2", "subject": "POD 7654321"}, tracking.REPLIED, intent="POD_REQUEST",
                        confidence=0.97, numbers=["7654321"], reason="draft",
                        state={"found": {"7654321": "7654321.pdf"}, "reply": "Hi,", "attachments": ["7654321.pdf"]})
        pd.DataFrame([
            {"entry_id": "E2", "subject": "POD 1", "body": "please send POD 1234567", "intent_label": "POD_REQUEST",
             "connotes_manual": "1234567", "label_source": "Manual", "labeled_at": "2026-10-14T01:00:00+00:00"},
            {"entry_id": "T2", "subject": "Invoice", "body": "please pay", "intent_label": "",
             "intent_label_pseudo": "OTHER", "pseudo_confidence": 0.9},
        ]).to_excel(config.TRAINING_DATA_PATH, index=False)

    def run_app(self) -> AppTest:
        # From a string, so `import app` finds the package, not dashboard/app.py itself.
        app = AppTest.from_string(DASHBOARD.read_text(encoding="utf-8"), default_timeout=120).run()
        self.assertEqual([e.message for e in app.exception], [])
        return app

    def test_every_tab_renders_with_data(self):
        app = self.run_app()
        self.assertEqual(len(app.tabs), 7)
        self.assertTrue(next(toggle for toggle in app.toggle if toggle.key == "settings_dashboard_auto_refresh").value)
        self.assertEqual(
            next(number for number in app.number_input if number.key == "settings_dashboard_refresh_interval").value,
            30,
        )
        tables = [frame.value for frame in app.dataframe]
        self.assertTrue(any(
            "Email date" in table.columns
            and "Waiting" in table.columns
            and "Request ID" in table.columns
            for table in tables
        ))  # queue
        history = next(table for table in tables if "Outcome" in table.columns)
        marks = dict(zip(history["Subject"], history["In training data"]))
        self.assertEqual(marks["POD 7654321"], "POD_REQUEST (Manual)")  # pulled into the training data
        self.assertEqual(marks["Can you check 1234567?"], "")  # not yet
        self.assertTrue(any(caption.value.startswith("2 of 2 rows") for caption in app.caption))  # labeling

    def test_history_can_be_filtered_by_intent(self):
        email = {"entry_id": "E3", "subject": "Invoice", "sender_email": "ops@example1.com",
                 "body": "Please review invoice", "received_time": "2026-10-15 09:00:00"}
        tracking.record(email, tracking.SKIPPED_NO_NUMBER, intent="OTHER", confidence=0.9, reason="not a POD request")

        app = self.run_app()
        app.multiselect(key="h_intent").set_value(["POD_REQUEST"]).run()

        self.assertEqual([error.message for error in app.exception], [])
        history = next(table for table in app.dataframe if "Outcome" in table.value.columns)
        self.assertCountEqual(history.value["Subject"], ["POD 7654321", "Can you check 1234567?"])
        self.assertTrue(any(caption.value.startswith("2 of 3 emails") for caption in app.caption))

    def test_running_dashboard_job_can_be_cancelled(self):
        from dashboard import tab_jobs

        tab_jobs._job_controller.clear()
        self.addCleanup(tab_jobs._job_controller.clear)
        job = jobs.Job("Long test job", "", (("-c", "import time; print('started', flush=True); time.sleep(30)"),))
        with mock.patch.dict(jobs.JOBS, {"test": job}, clear=True):
            app = self.run_app()
            app.button(key="job_test").click().run()
            self.assertEqual([error.message for error in app.exception], [])
            app.button(key="cancel_dashboard_job").click().run()

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and tab_jobs._job_controller().snapshot()["status"] != "cancelled":
                time.sleep(0.01)

            self.assertEqual(tab_jobs._job_controller().snapshot()["status"], "cancelled")
            self.assertEqual([error.message for error in app.exception], [])

    def test_dashboard_auto_refresh_can_be_disabled_and_interval_changed(self):
        app = self.run_app()
        app.toggle(key="settings_dashboard_auto_refresh").set_value(False).run()
        self.assertEqual([e.message for e in app.exception], [])
        self.assertIn("POD_DASHBOARD_AUTO_REFRESH=off", self.env())
        self.assertTrue(any(caption.value == "Dashboard auto-refresh is off." for caption in app.caption))

        app.number_input(key="settings_dashboard_refresh_interval").set_value(60).run()
        app.button(key="save_dashboard_refresh_interval").click().run()
        self.assertEqual([e.message for e in app.exception], [])
        self.assertIn("POD_DASHBOARD_REFRESH_INTERVAL_SECONDS=60", self.env())

        app.toggle(key="settings_dashboard_auto_refresh").set_value(True).run()
        self.assertEqual([e.message for e in app.exception], [])
        self.assertIn("POD_DASHBOARD_AUTO_REFRESH=on", self.env())
        self.assertTrue(any(caption.value == "Dashboard tabs refresh automatically every 60 seconds." for caption in app.caption))

    def test_the_review_pop_up_opens_for_a_manual_review_email(self):
        app = self.run_app()
        next(button for button in app.button if button.label.startswith("Review 1 emails")).click().run()
        self.assertEqual([e.message for e in app.exception], [])
        self.assertTrue(any("Model says POD_REQUEST (80%)" in caption.value for caption in app.caption))
        self.assertEqual(app.session_state["open_popup"], "review")  # the auto-refresh waits meanwhile
        self.assertTrue(next(button for button in app.button if button.label == "Save").disabled)  # no intent yet
        self.assertIn("Open in Outlook", [button.label for button in app.button])
        self.assertTrue(next(button for button in app.button if button.label == "Reprocess 0 in Power BI").disabled)

    def test_stale_review_queue_selection_positions_are_ignored(self):
        from dashboard.tab_review import _selected_queue_rows

        view = pd.DataFrame({
            "entry_id": ["E1", "E2"],
            "status": [tracking.HELD, tracking.WAITING_PBI],
        })
        selected = _selected_queue_rows(view, [1, 3])
        self.assertEqual(selected["entry_id"].tolist(), ["E2"])

    def open_held(self, numbers: list[str]) -> AppTest:
        tracking.record(tracking.latest(tracking.MANUAL_REVIEW)[0], tracking.RESOLVED, reason="resolved in the dashboard")
        tracking.record({"entry_id": "E3", "subject": "POD 1234567", "sender_email": "ops@example1.com",
                         "body": "Invoice 20260407 - 1234567"}, tracking.HELD, intent="POD_REQUEST",
                        numbers=numbers, reason="number not on the share and not in Power BI")
        app = self.run_app()
        next(button for button in app.button if button.label.startswith("Review 1 emails")).click().run()
        self.assertEqual([e.message for e in app.exception], [])
        return app

    def test_a_held_email_opens_as_a_pod_request_with_the_list_to_look_up(self):
        app = self.open_held(["20260407", "1234567"])
        self.assertEqual(next(box for box in app.selectbox if box.label == "Intent").value, "POD_REQUEST")
        self.assertFalse(next(button for button in app.button if button.label == "Save").disabled)
        self.assertIn("Will look up: 20260407, 1234567", [caption.value for caption in app.caption])

    def test_a_list_that_cannot_be_saved_is_warned_about_before_save(self):
        # Typing inside the pop-up closes it in AppTest, so the held list itself holds a wrong number.
        app = self.open_held(["1234567", "XY 12302"])
        self.assertTrue(any("Not a number: 'XY 12302'" in warning.value for warning in app.warning))

    def test_a_long_list_is_not_refused(self):
        numbers = [str(1000000 + n) for n in range(25)]
        app = self.open_held(numbers)
        self.assertIn(f"Will look up: {', '.join(numbers)}", [caption.value for caption in app.caption])

    def test_a_card_that_never_reached_teams_is_shown_with_its_request_id(self):
        tracking.record({"entry_id": "E4", "subject": "POD 555555", "sender_email": "ops@example1.com"}, tracking.WAITING_TEAM,
                        numbers=["555555"], state={"cards": {"555555": "abc-0-r3"}, "ambiguous": {"555555": ["a.pdf", "b.pdf"]},
                                                   "card_delivery": {"555555": {"failed": "BadRequest"}}})
        app = self.run_app()
        warning = next(w.value for w in app.warning if "never reached Teams" in w.value)
        self.assertIn("POD 555555 · abc-0-r3 · 555555: BadRequest", warning)
        self.assertTrue(next(b for b in app.button if b.label.startswith("Send Teams card again")).disabled)  # none selected

    def test_a_power_bi_request_flow_1_never_answered_is_shown_with_its_request_id(self):
        tracking.record({"entry_id": "E5", "subject": "POD 7777777", "sender_email": "ops@example1.com"}, tracking.WAITING_PBI,
                        numbers=["7777777"], state={"pbi_request": "abc-p4", "missing": ["7777777"],
                                                    "card_delivery": {"power_bi": {"failed": "Flow 1 has not answered"}}})
        app = self.run_app()
        warning = next(w.value for w in app.warning if "Flow 1 has not answered" in w.value)
        self.assertIn("POD 7777777 · abc-p4", warning)
        self.assertIn("Reprocess in Power BI", warning)
        self.assertFalse(any("never reached Teams" in w.value for w in app.warning))

    def test_the_history_shows_the_numbers_of_each_status(self):
        from dashboard.tab_history import _processing_details
        held = {"at_utc": "2026-10-14T01:00:00+00:00", "status": "Held", "reason": "number not in Power BI",
                "numbers": "20260407;1234567", "state": {}}
        reviewed = {**held, "status": "Reviewed", "reason": "reviewed in Teams by Ana", "numbers": "1234567"}
        text = _processing_details([held, reviewed])
        self.assertIn("Held · number not in Power BI · 20260407; 1234567", text)
        self.assertIn("Reviewed · reviewed in Teams by Ana · 1234567", text)

    def test_the_labeling_pop_up_opens_with_its_buttons(self):
        app = self.run_app()
        next(button for button in app.button if button.label.startswith("Label one by one")).click().run()
        self.assertEqual([e.message for e in app.exception], [])
        labels = [button.label for button in app.button]
        for label in ("Previous", "Use recommendation", "Save", "Next", "Open in Outlook"):
            self.assertIn(label, labels)

    def test_switching_on_moving_finished_emails_records_when(self):
        (config.PROJECT_DIR / ".env").write_text("POD_MOVE_FINISHED=off\n", encoding="utf-8")
        app = self.run_app()
        app.toggle(key="settings_move_finished").set_value(True).run()
        self.assertEqual([e.message for e in app.exception], [])
        self.assertIn("POD_MOVE_FINISHED=on", self.env())
        self.assertRegex(self.env(), r"POD_MOVE_FINISHED_SINCE=20\d\d-\d\d-\d\dT[\d:.]+\+00:00")
        app.toggle(key="settings_move_finished").set_value(False).run()
        self.assertIn("POD_MOVE_FINISHED=off", self.env())

    def env(self) -> str:
        path = config.PROJECT_DIR / ".env"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_a_target_is_changed_in_the_settings_tab_and_shown_on_the_overview(self):
        (config.PROJECT_DIR / ".env").write_text("POD_SEND_MODE=draft\n", encoding="utf-8")
        app = self.run_app()
        self.assertEqual(app.number_input(key="target_POD_PILOT_TARGET").value, 90)
        app.number_input(key="target_POD_PILOT_TARGET").set_value(80).run()
        app.button(key="save_POD_PILOT_TARGET").click().run()
        self.assertEqual([e.message for e in app.exception], [])
        self.assertIn("POD_PILOT_TARGET=0.80", self.env())
        self.assertTrue(any("(target 80%" in caption.value for caption in app.caption))

    def test_switching_on_asks_first_and_cancel_changes_nothing(self):
        (config.PROJECT_DIR / ".env").write_text("POD_SEND_MODE=draft\n", encoding="utf-8")
        app = self.run_app()
        app.radio(key="settings_send_mode").set_value("send").run()
        self.assertIn("POD_SEND_MODE=draft", self.env())  # nothing saved yet
        self.assertTrue(any("**every reply**" in warning.value for warning in app.warning))
        app.button(key="cancel_automatic_replies").click().run()
        self.assertEqual([e.message for e in app.exception], [])
        self.assertIn("POD_SEND_MODE=draft", self.env())
        self.assertEqual(app.radio(key="settings_send_mode").value, "draft")

    def test_confirming_saves_send_and_switching_off_needs_no_confirmation(self):
        (config.PROJECT_DIR / ".env").write_text("POD_SEND_MODE=draft\n", encoding="utf-8")
        app = self.run_app()
        app.radio(key="settings_send_mode").set_value("send").run()
        next(button for button in app.button if button.label == "Yes, send automatically").click().run()
        self.assertIn("POD_SEND_MODE=send", self.env())
        app.radio(key="settings_send_mode").set_value("draft").run()
        self.assertIn("POD_SEND_MODE=draft", self.env())

    def test_partial_sending_asks_for_each_domain_added_but_not_for_one_taken_out(self):
        # D-057: more emails sent without review need a yes; fewer do not.
        (config.PROJECT_DIR / ".env").write_text("POD_SEND_MODE=draft\n", encoding="utf-8")
        app = self.run_app()
        app.text_input(key="settings_send_domains").input("example1.com").run()
        app.radio(key="settings_send_mode").set_value("partial").run()
        self.assertTrue(any("replies to example1.com" in warning.value for warning in app.warning))
        self.assertIn("POD_SEND_MODE=draft", self.env())
        next(button for button in app.button if button.label == "Yes, send automatically").click().run()
        self.assertIn("POD_SEND_MODE=partial", self.env())
        self.assertIn("POD_SEND_DOMAINS=example1.com", self.env())

        app.text_input(key="settings_send_domains").input("example1.com; example2.com").run()
        self.assertTrue(any("replies to example2.com" in warning.value for warning in app.warning))
        self.assertIn("POD_SEND_DOMAINS=example1.com\n", self.env() + "\n")  # not yet
        next(button for button in app.button if button.label == "Yes, send automatically").click().run()
        self.assertIn("POD_SEND_DOMAINS=example1.com; example2.com", self.env())

        app.text_input(key="settings_send_domains").input("example2.com").run()  # one taken out: no question
        self.assertIn("POD_SEND_DOMAINS=example2.com", self.env())
        app.text_input(key="settings_send_domains").input("ops@example2.com").run()
        self.assertTrue(any("after the @" in error.value for error in app.error))
        self.assertIn("POD_SEND_DOMAINS=example2.com", self.env())


if __name__ == "__main__":
    unittest.main()
