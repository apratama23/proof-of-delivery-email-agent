"""Bring emails the agent processed into the training data (D-013).

Each new email arrives pre-labeled with the agent's decision, marked
`System decision`, in the pseudo-label columns. It counts for training only
after the product owner confirms or changes it in the dashboard, which sets
`intent_label` and marks it `Manual` (answer 45: the model must not learn
from its own unchecked guesses).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import config, tracking
from .historical_pull import TRAINING_COLUMNS

SYSTEM_DECISION = "System decision"
MANUAL = "Manual"
# Labeled in the Review queue: an email the intent model was unsure of.
MANUAL_REVIEW = "Manual review"
# Automatic mail and number-less emails are never POD requests (D-002, D-015),
# so they teach the intent model nothing it will meet.
_NOT_TRAINING_MATERIAL = (tracking.SKIPPED_GUARD, tracking.SKIPPED_NO_NUMBER)


def pull_system_decisions(training_path: str | Path, now: datetime | None = None) -> int:
    """Append processed emails not yet in the training file; return how many."""
    source = Path(training_path)
    existing = pd.read_excel(source, dtype=str) if source.exists() else pd.DataFrame(columns=TRAINING_COLUMNS)
    known = set(existing["entry_id"].fillna("").astype(str)) if "entry_id" in existing else set()
    pulled_at = (now or datetime.now(timezone.utc)).isoformat()

    rows = []
    for event in tracking.latest():
        if event["status"] in _NOT_TRAINING_MATERIAL or event["entry_id"] in known:
            continue
        rows.append(
            {
                "entry_id": event["entry_id"],
                "received_time": event["received_time"],
                "sender_email": event["sender_email"],
                "to_recipients": event["to_recipients"],
                "subject": event["subject"],
                "body": event["body"],
                "intent_label": "",
                "intent_label_pseudo": event["intent"],
                "pseudo_confidence": event["confidence"] if event["confidence"] is not None else "",
                "pseudo_verified": False,
                "connote_number_auto": event["numbers"],
                "label_source": SYSTEM_DECISION,
                "system_pulled_at": pulled_at,
            }
        )
    if not rows:
        return 0
    updated = pd.concat([existing, pd.DataFrame(rows)], ignore_index=True)
    for column in TRAINING_COLUMNS:
        if column not in updated.columns:
            updated[column] = ""
    source.parent.mkdir(parents=True, exist_ok=True)
    updated.to_excel(source, index=False)
    return len(rows)


def add_review(training_path: str | Path, entry: dict, review: dict, label_source: str = MANUAL_REVIEW) -> str:
    """Write a person's review of an email into the training data, by entry ID.

    The intent and numbers become the row's manual labels, marked `label_source`
    (`Manual review` from the Review queue, `Manual` from History) with the time; other columns of an existing row
    (pseudo-labels, numbers found automatically) are kept. A new row gets
    the email and the agent's view, as `pull_system_decisions` would.
    Returns "" or, when it replaced an earlier label, "changed from X".
    """
    source = Path(training_path)
    data = pd.read_excel(source) if source.exists() else pd.DataFrame(columns=TRAINING_COLUMNS)
    for column in TRAINING_COLUMNS:
        if column not in data.columns:
            data[column] = ""
        data[column] = data[column].astype(object)
    entry_id = str(entry["entry_id"])
    matches = data.index[data["entry_id"].fillna("").astype(str) == entry_id]
    if len(matches):
        index = matches[0]
    else:
        index = len(data)
        data.loc[index] = {column: "" for column in data.columns}
        for column in ("received_time", "sender_email", "to_recipients", "subject", "body"):
            data.at[index, column] = entry.get(column, "")
        data.at[index, "entry_id"] = entry_id
        data.at[index, "intent_label_pseudo"] = entry.get("intent", "")
        data.at[index, "pseudo_confidence"] = entry.get("confidence") if entry.get("confidence") is not None else ""
        data.at[index, "pseudo_verified"] = False
        data.at[index, "connote_number_auto"] = entry.get("numbers", "")
    previous = data.at[index, "intent_label"]
    previous = "" if previous is None or pd.isna(previous) else str(previous).strip()
    data.at[index, "intent_label"] = review["intent"]
    data.at[index, "connotes_manual"] = ";".join(review["connotes"])
    data.at[index, "reference_number_manual"] = ";".join(review["references"])
    data.at[index, "label_source"] = label_source
    data.at[index, "labeled_at"] = datetime.now(timezone.utc).isoformat()
    source.parent.mkdir(parents=True, exist_ok=True)
    data.to_excel(source, index=False)
    return f"changed from {previous}" if previous and previous != review["intent"] else ""


if __name__ == "__main__":
    added = pull_system_decisions(config.training_data_path())
    print(f"Added {added} processed emails as '{SYSTEM_DECISION}' rows.", flush=True)
