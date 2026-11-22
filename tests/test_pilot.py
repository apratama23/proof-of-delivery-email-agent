"""The pilot measure (D-014, D-053): sent with the same PODs, with PODs changed, deleted or still a draft."""

import unittest

from app import pilot, reply_generator

DRAFT = {
    "conversation_id": "C1",
    "drafted_at": "2020-01-06T08:00:00+00:00",
    "reply": reply_generator.build_reply(["1234567"], []),
    "attachments": [r"\\server\share\POD\2026\1234567_a.pdf"],
    "draft_entry_id": "D1",
}


def _sent(body=DRAFT["reply"], files=("1234567_a.pdf",), conversation="C1", at="2020-01-06T08:30:00+00:00"):
    return {"conversation_id": conversation, "sent_on": at, "body": body, "attachments": list(files)}


class OutcomeTest(unittest.TestCase):
    def test_sent_with_the_drafts_pdfs_is_the_same_pods_whatever_the_text(self):
        # Only the PDFs count (product owner, D-053): sentences added, cut or changed are fine.
        for body in (DRAFT["reply"], "Hi, here you go. Thanks!", DRAFT["reply"] + "\n\nAlso see our new hours."):
            with self.subTest(body=body):
                self.assertEqual(pilot.outcome(DRAFT, [_sent(body)], draft_still_exists=False), pilot.SENT_SAME_PODS)

    def test_the_signature_logo_is_not_one_of_the_agents_attachments(self):
        sent = _sent(files=("1234567_A.pdf", "image001.png"))
        self.assertEqual(pilot.outcome(DRAFT, [sent], draft_still_exists=False), pilot.SENT_SAME_PODS)

    def test_another_pdf_added_removed_or_swapped_is_pods_changed(self):
        for files in ((), ("1234567_a.pdf", "x.pdf"), ("x.pdf",)):
            with self.subTest(files=files):
                self.assertEqual(pilot.outcome(DRAFT, [_sent(files=files)], False), pilot.SENT_OTHER_PODS)

    def test_replies_in_other_conversations_or_before_the_draft_do_not_count(self):
        others = [_sent(conversation="C2"), _sent(at="2020-01-06T07:59:00+00:00")]
        self.assertIsNone(pilot.outcome(DRAFT, others, draft_still_exists=True))
        self.assertEqual(pilot.outcome(DRAFT, others, draft_still_exists=False), pilot.DELETED)

    def test_the_first_reply_after_the_draft_is_the_one_judged(self):
        later = _sent(files=("x.pdf",), at="2020-01-06T09:00:00+00:00")
        self.assertEqual(pilot.outcome(DRAFT, [later, _sent()], False), pilot.SENT_SAME_PODS)


class SummaryTest(unittest.TestCase):
    def test_share_is_of_drafts_sent_or_deleted_and_sent_mode_replies_are_left_out(self):
        events = [
            {"state": {**DRAFT, "pilot": pilot.SENT_SAME_PODS}},
            {"state": {**DRAFT, "pilot": "sent unedited"}},  # recorded before D-053: the same PODs
            {"state": {**DRAFT, "pilot": "sent edited"}},  # recorded before D-053: counted as changed
            {"state": {**DRAFT, "pilot": pilot.DELETED}},  # not used by the team: counts against
            {"state": dict(DRAFT)},  # still a draft: not decided yet (D-052)
            {"state": {"reply": "sent directly"}},  # send mode: not a draft
            {"state": {**DRAFT, "attach_by_hand": True}},  # files attached by hand (D-050)
        ]
        result = pilot.summary(events)
        self.assertEqual((result["drafts"], result["decided"]), (5, 4))
        self.assertEqual(result[pilot.STILL_DRAFT], 1)
        self.assertEqual((result[pilot.SENT_SAME_PODS], result[pilot.SENT_OTHER_PODS]), (2, 1))
        self.assertEqual(result["same_pods_share"], 0.5)
        self.assertEqual(result["target"], 0.90)
        self.assertEqual(pilot.summary(events, 0.8)["target"], 0.8)  # the product owner's own (D-054)

    def test_no_draft_decided_yet(self):
        self.assertIsNone(pilot.summary([{"state": dict(DRAFT)}])["same_pods_share"])

    def test_no_drafts_yet(self):
        self.assertIsNone(pilot.summary([])["same_pods_share"])


if __name__ == "__main__":
    unittest.main()
