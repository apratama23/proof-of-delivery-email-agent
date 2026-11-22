"""The pilot measure (D-014): how many of the agent's drafts went out with the PODs it attached.

A draft counts as sent with the same PODs when a reply in the same
conversation, sent after the draft was made, carries exactly the PDFs the
agent attached; its sentences may be changed, added to or cut (product owner,
D-053). Other attachments, such as a signature's logo, are not the agent's
and are not compared. A draft sent with other PDFs counts as PODs changed;
one that is gone with no such reply counts as deleted; one still in Drafts
is not decided yet (D-052).
"""

from __future__ import annotations

from datetime import datetime

SENT_SAME_PODS = "sent, same PODs"
SENT_OTHER_PODS = "sent, PODs changed"
DELETED = "deleted"
STILL_DRAFT = "still a draft"
OUTCOMES = (SENT_SAME_PODS, SENT_OTHER_PODS, DELETED, STILL_DRAFT)
# Recorded before D-053, when the text was compared too: an unedited draft
# had the same PDFs; an edited one is counted as changed, as which part was
# edited was not kept.
_EARLIER = {"sent unedited": SENT_SAME_PODS, "sent edited": SENT_OTHER_PODS}
SENT = (SENT_SAME_PODS, SENT_OTHER_PODS, *_EARLIER)  # any outcome of a draft that was sent
TARGET = 0.90  # D-014, D-054: the default; the product owner sets it in the dashboard (POD_PILOT_TARGET)


def compare(draft_files: list[str], sent_files: list[str]) -> str:
    """SENT_SAME_PODS if the reply carried exactly the draft's PDFs."""
    pdfs = lambda names: sorted(name.casefold() for name in names if name.casefold().endswith(".pdf"))
    return SENT_SAME_PODS if pdfs(draft_files) == pdfs(sent_files) else SENT_OTHER_PODS


def outcome(draft: dict, sent_items: list[dict], draft_still_exists: bool) -> str | None:
    """What became of one draft, or None while it is still waiting in Drafts.

    `draft` holds `conversation_id`, `drafted_at` (ISO, UTC) and
    `attachments` (paths). `sent_items` are sent emails with
    `conversation_id`, `sent_on` (ISO, UTC) and `attachments` (names).
    """
    reply = first_reply(draft, sent_items)
    if reply:
        files = [path.replace("\\", "/").rsplit("/", 1)[-1] for path in draft.get("attachments", [])]
        return compare(files, reply["attachments"])
    return None if draft_still_exists else DELETED


def first_reply(draft: dict, sent_items: list[dict]) -> dict | None:
    """The first email sent in the draft's conversation after the draft was made, or None."""
    drafted_at = datetime.fromisoformat(draft["drafted_at"])
    replies = sorted(
        (item for item in sent_items
         if item["conversation_id"] == draft["conversation_id"]
         and datetime.fromisoformat(item["sent_on"]) >= drafted_at),
        key=lambda item: item["sent_on"],
    )
    return replies[0] if replies else None


def summary(replied_events: list[dict], target: float = TARGET) -> dict:
    """Counts per outcome over the drafts the agent made, and the share sent with the same PODs.

    The share is of the drafts a person has decided on: sent (same or
    changed PODs) or deleted (a deleted draft is one the team did not use,
    so it counts against it, as D-014 counts a draft never sent). A draft
    still waiting is not decided yet, so it does not count (D-052). Drafts
    made without their attachments (D-050) are left out.
    """
    counts = {name: 0 for name in OUTCOMES}
    for event in replied_events:
        state = event.get("state") or {}
        # A draft whose files are attached by hand is not the agent's measure (D-050).
        if "draft_entry_id" in state and not state.get("attach_by_hand"):
            result = state.get("pilot", STILL_DRAFT)
            counts[_EARLIER.get(result, result)] += 1
    drafts = sum(counts.values())
    decided = drafts - counts[STILL_DRAFT]
    return {
        **counts,
        "drafts": drafts,
        "decided": decided,
        "same_pods_share": counts[SENT_SAME_PODS] / decided if decided else None,
        "target": target,
    }
