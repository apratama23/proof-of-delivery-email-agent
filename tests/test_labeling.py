"""Labeling rules and dashboard jobs."""

import sys
import unittest

import pandas as pd

from app import jobs, labeling


def _training() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "entry_id": ["A", "B", "C", "D"],
            "received_time": ["2026-09-01 08:00", "2026-09-10 08:00", "2026-09-20 08:00", "2026-09-25 08:00"],
            "sender_email": ["ops@example1.com", "x@example2.com", "ops@example1.com", "y@example2.com"],
            "subject": ["POD 1234567", "Invoice", "Fw: POD", "Where is my POD"],
            "body": ["Please send <b>POD</b> 1234567", "Pay 999", "see below 7654321", "PO40001 please"],
            "intent_label": ["POD_REQUEST", "", "", ""],
            "label_source": ["", "", "System decision", ""],
            "intent_label_pseudo": ["", "OTHER", "POD_REQUEST", ""],
            "pseudo_confidence": ["", "0.55", "0.97", ""],
            "connotes_manual": ["1234567", "", "", ""],
            "reference_number_manual": ["", "", "", ""],
            "connote_number_auto": ["", "", "7654321", ""],
            "ref_number_auto": ["", "", "", ""],
        }
    )


class LabelingTest(unittest.TestCase):
    def test_filters(self):
        data = _training()
        ids = lambda **kwargs: labeling.filter_rows(data, labeling.Filters(**kwargs))["entry_id"].tolist()
        self.assertEqual(ids(), ["A", "B", "C", "D"])
        self.assertEqual(ids(search="pod"), ["A", "C", "D"])
        self.assertEqual(ids(sender="example1"), ["A", "C"])
        self.assertEqual(ids(label="Unlabeled"), ["B", "D"])
        self.assertEqual(ids(label="System decision to review"), ["C"])
        self.assertEqual(ids(label="POD_REQUEST"), ["A"])
        self.assertEqual(ids(source="Manual"), ["A"])  # older hand labels count as Manual
        self.assertEqual(ids(start=pd.Timestamp("2026-09-10"), end=pd.Timestamp("2026-09-20")), ["B", "C"])
        self.assertEqual(ids(min_confidence=0.9), ["C"])
        self.assertEqual(ids(pseudo="Has pseudo-label"), ["B", "C"])
        self.assertEqual(ids(pseudo="No pseudo-label"), ["A", "D"])

    def test_edited_rows_are_saved_and_marked_manual(self):
        data = _training()
        edited = data.loc[[1, 2], ["intent_label", "connotes_manual", "reference_number_manual"]].copy()
        edited.at[1, "intent_label"] = "OTHER"
        self.assertEqual(labeling.apply_edits(data, edited), 1)
        self.assertEqual(data.at[1, "intent_label"], "OTHER")
        self.assertEqual(data.at[1, "label_source"], "Manual")
        self.assertEqual(data.at[2, "label_source"], "System decision")  # unchanged row

    def test_recommendation_comes_from_pseudo_labels_and_blank_cells_are_empty(self):
        row = pd.Series({
            "intent_label_pseudo": "POD_REQUEST", "pseudo_confidence": 0.973,
            "connotes_pseudo": "7336608", "reference_number_pseudo": float("nan"),
        })
        row["connotes_pseudo_confidence"] = "7336608:0.62;7336608:0.873"
        self.assertEqual(
            labeling.recommendation(row),
            {
                "intent": "POD_REQUEST", "confidence": "0.97", "connotes": "7336608", "references": "",
                "numbers_source": "CRF", "connotes_shown": "7336608 (87%)", "references_shown": "",
            },
        )
        # No CRF number: the numbers found automatically, like the agent's regex fallback.
        auto = labeling.recommendation(pd.Series({"connotes_pseudo": "", "connote_number_auto": "576963"}))
        self.assertEqual(
            (auto["connotes"], auto["connotes_shown"], auto["numbers_source"]),
            ("576963", "576963", "master list or regex"),
        )
        empty = labeling.recommendation(pd.Series({"intent_label_pseudo": float("nan")}))
        self.assertEqual(
            empty,
            {"intent": "", "confidence": "", "connotes": "", "references": "",
             "numbers_source": "", "connotes_shown": "", "references_shown": ""},
        )

    def test_saved_email_leaves_the_pop_up_list_when_it_no_longer_matches(self):
        # Labeled under "Unlabeled": it leaves, and the next email takes its place.
        self.assertEqual(labeling.after_save(["A", "B", "C"], 1, still_matches=False), (["A", "C"], 1))
        # The last one leaves: back to the one before it; nothing left gives -1.
        self.assertEqual(labeling.after_save(["A", "B"], 1, still_matches=False), (["A"], 0))
        self.assertEqual(labeling.after_save(["A"], 0, still_matches=False), ([], -1))
        # Still matching (filter "All"): step to the next, stay on the last.
        self.assertEqual(labeling.after_save(["A", "B"], 0, still_matches=True), (["A", "B"], 1))
        self.assertEqual(labeling.after_save(["A", "B"], 1, still_matches=True), (["A", "B"], 1))

    def test_labels_get_a_date_to_filter_and_sort_by(self):
        data = _training()
        edited = data[["intent_label", "connotes_manual", "reference_number_manual"]].copy()
        edited.loc[1, "intent_label"] = "OTHER"
        labeling.apply_edits(data, edited)
        labeling.set_intent(data, [3], "POD_REQUEST")
        self.assertTrue(pd.isna(data.at[0, "labeled_at"]))  # labeled before dates were kept
        self.assertTrue(data.at[1, "labeled_at"] and data.at[3, "labeled_at"])
        data.at[1, "labeled_at"] = "2026-10-01T01:00:00+00:00"
        data.at[3, "labeled_at"] = "2026-10-05T01:00:00+00:00"
        today = labeling.labeled_local(data).loc[3].normalize()
        ids = lambda **kwargs: labeling.filter_rows(data, labeling.Filters(**kwargs))["entry_id"].tolist()
        self.assertEqual(ids(labeled_start=today), ["D"])
        self.assertEqual(ids(labeled_start=today - pd.Timedelta(days=10), labeled_end=today), ["B", "D"])
        newest = labeling.sort_rows(data, "Labeled, newest first")["entry_id"].tolist()
        self.assertEqual(newest[:2], ["D", "B"])
        self.assertEqual(labeling.sort_rows(data, "Labeled, oldest first")["entry_id"].tolist()[:2], ["B", "D"])
        self.assertEqual(labeling.sort_rows(data, "File order")["entry_id"].tolist(), ["A", "B", "C", "D"])

    def test_label_dates_are_local_dates(self):
        data = pd.DataFrame({"labeled_at": ["2026-10-19T20:00:00+00:00"]})
        self.assertEqual(labeling.labeled_local(data).iloc[0], pd.Timestamp("2026-10-20 04:00"))

    def test_training_marks_say_the_label_and_who_set_it(self):
        marks = labeling.training_marks(_training())
        self.assertEqual(marks["A"], "POD_REQUEST (Manual)")
        self.assertEqual(marks["C"], "no label (System decision)")
        self.assertEqual(marks["B"], "no label (nobody)")
        self.assertEqual(labeling.training_marks(pd.DataFrame()), {})

    def test_save_and_go_on_lands_on_the_email_the_person_asked_for(self):
        # Next: the email after it, whether the saved one leaves the list or stays.
        self.assertEqual(labeling.step_after_save(["A", "B", "C"], 1, False, "next"), (["A", "C"], 1))
        self.assertEqual(labeling.step_after_save(["A", "B", "C"], 1, True, "next"), (["A", "B", "C"], 2))
        # Previous: the email before it, either way.
        self.assertEqual(labeling.step_after_save(["A", "B", "C"], 1, False, "previous"), (["A", "C"], 0))
        self.assertEqual(labeling.step_after_save(["A", "B", "C"], 1, True, "previous"), (["A", "B", "C"], 0))
        # The last one saved and gone: nothing left.
        self.assertEqual(labeling.step_after_save(["A"], 0, False, "previous"), ([], -1))

    def test_only_label_edits_in_the_table_count_as_unsaved(self):
        self.assertFalse(labeling.has_label_edits(None))
        self.assertFalse(labeling.has_label_edits({"edited_rows": {0: {"select": True}}}))
        self.assertTrue(labeling.has_label_edits({"edited_rows": {0: {"select": True}, 3: {"intent_label": "OTHER"}}}))

    def test_bulk_label_and_confirming_system_decisions(self):
        data = _training()
        self.assertEqual(labeling.set_intent(data, [1, 3], "OTHER"), 2)
        self.assertEqual(data.loc[[1, 3], "intent_label"].tolist(), ["OTHER", "OTHER"])
        # Only rows still awaiting review are confirmed.
        self.assertEqual(labeling.confirm_system_decisions(data, [0, 2]), 1)
        self.assertEqual(data.at[2, "intent_label"], "POD_REQUEST")
        self.assertEqual(data.at[2, "label_source"], "Manual")

    def test_numbers_are_highlighted_in_escaped_text(self):
        row = _training().iloc[0]
        self.assertEqual(labeling.numbers_in(row), ["1234567"])
        shown = labeling.highlight(row["body"], labeling.numbers_in(row))
        self.assertEqual(shown, "Please send &lt;b&gt;POD&lt;/b&gt; <mark>1234567</mark>")
        self.assertEqual(labeling.highlight("a\nb", []), "a<br>b")


class JobsTest(unittest.TestCase):
    def test_progress_is_read_from_counts_in_output(self):
        self.assertEqual(jobs.progress_of("Progress: 5/20 (25%)"), 0.25)
        self.assertEqual(jobs.progress_of("Exporting newest emails: 3/3"), 1.0)
        self.assertIsNone(jobs.progress_of("Training logreg..."))
        self.assertIsNone(jobs.progress_of("0/0"))

    def test_every_job_names_a_module_that_exists(self):
        import importlib.util

        for job in jobs.JOBS.values():
            for command in job.commands:
                self.assertEqual(command[0], "-m")
                self.assertIsNotNone(importlib.util.find_spec(command[1]), command[1])

    def test_run_streams_lines_and_stops_at_a_failure(self):
        job = jobs.Job("t", "", (("-c", "print('1/2'); print('2/2')"), ("-c", "raise SystemExit(3)"), ("-c", "print('never')")))
        lines = []
        self.assertEqual(jobs.run(job, lines.append), 3)
        self.assertIn("1/2", lines)
        self.assertIn("Stopped: exit code 3", lines)
        self.assertNotIn("never", lines)


if __name__ == "__main__":
    unittest.main()
