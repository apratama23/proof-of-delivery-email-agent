"""Append-only status log of every email, in a local SQLite file (D-010).

One row per status event; the current state of an email is its latest row.
That rule lives in `latest()` and nowhere else.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id TEXT NOT NULL CHECK (entry_id <> ''),
    at_utc TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status <> ''),
    received_time TEXT NOT NULL DEFAULT '',
    sender_email TEXT NOT NULL DEFAULT '',
    to_recipients TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    numbers TEXT NOT NULL DEFAULT '',
    intent TEXT NOT NULL DEFAULT '',
    confidence REAL,
    reason TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_entry ON events (entry_id, id);
"""
# message_id: the Internet Message ID, which stays the same when the email is moved (D-041).
_EMAIL_FIELDS = ("received_time", "sender_email", "to_recipients", "subject", "message_id", "body")

# Every status an email can be in. One name, one meaning, everywhere.
SKIPPED_GUARD = "Skipped - Guard"  # own mailbox, auto-reply, bounce, no-reply
SKIPPED_NO_NUMBER = "Skipped - No Number"  # not a POD request (D-015)
MANUAL_REVIEW = "Manual Review"  # cold start or intent below threshold
OTHER_INTENT = "Manual Handling - Other Intent"
WAITING_PBI = "Waiting Power BI"  # some numbers not on the share (D-004)
WAITING_TEAM = "Waiting Team"  # a number matched several files (D-006)
HELD = "Held"  # the team must act; reason says why (D-006)
REPLIED = "Replied"  # reply drafted or sent; reason says which (D-008)
ERROR = "Error"
RESOLVED = "Resolved"  # a person handled it and cleared it in the dashboard
# A person asked for it to be processed again from scratch (dashboard
# "Process again"); the next cycle reads it from Outlook like a new email.
RESET = "Reset"
# A person reviewed it in the dashboard: POD request with these numbers, so
# the next cycle continues it (state["review"]); or not a POD request.
REVIEWED = "Reviewed"
REVIEWED_OTHER = "Reviewed - Other"
OPEN_STATUSES = (WAITING_PBI, WAITING_TEAM, REVIEWED)
NEEDS_PEOPLE = (MANUAL_REVIEW, OTHER_INTENT, HELD, ERROR)
# Statuses that may be processed again: nothing was replied and no Power BI
# request or Teams card is still open.
REPROCESSABLE = NEEDS_PEOPLE
# Statuses a person may review: the intent model was unsure or said OTHER, or
# the email is held and its numbers can be corrected (D-035).
REVIEWABLE = (MANUAL_REVIEW, OTHER_INTENT, HELD)


def _connect() -> sqlite3.Connection:
    path = Path(config.TRACKING_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(_SCHEMA)
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(events)")}
    for added in ("to_recipients", "message_id"):  # columns added after the first release
        if added not in columns:
            connection.execute(f"ALTER TABLE events ADD COLUMN {added} TEXT NOT NULL DEFAULT ''")
    return connection


def record(
    email: dict,
    status: str,
    *,
    numbers: list[str] | None = None,
    intent: str = "",
    confidence: float | None = None,
    reason: str = "",
    state: dict | None = None,
) -> None:
    """Append one status event for an email.

    `email` is the email dict (or the latest event of it); its received time,
    sender, subject and body are copied so every row stands alone. `state`
    is the working data of the email (found files, Power BI rows, team
    choices), carried forward by the caller.
    """
    row = {
        "entry_id": str(email["entry_id"]),
        "at_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        **{field: str(email.get(field) or "") for field in _EMAIL_FIELDS},
        "numbers": ";".join(numbers) if numbers is not None else str(email.get("numbers") or ""),
        "intent": intent or str(email.get("intent") or ""),
        "confidence": confidence if confidence is not None else email.get("confidence"),
        "reason": reason,
        "state": json.dumps(state if state is not None else email.get("state") or {}, default=str),
    }
    with closing(_connect()) as connection, connection:
        connection.execute(
            f"INSERT INTO events ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
            tuple(row.values()),
        )


def _as_dict(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["state"] = json.loads(item["state"] or "{}")
    return item


def latest(status: str | None = None) -> list[dict]:
    """The current (latest) event of every email, optionally only in `status`."""
    query = (
        "SELECT * FROM events WHERE id IN (SELECT MAX(id) FROM events GROUP BY entry_id)"
    )
    parameters: tuple = ()
    if status is not None:
        query += " AND status = ?"
        parameters = (status,)
    with closing(_connect()) as connection:
        return [_as_dict(row) for row in connection.execute(query + " ORDER BY id", parameters)]


def history(entry_id: str) -> list[dict]:
    """Every event of one email, oldest first."""
    with closing(_connect()) as connection:
        rows = connection.execute(
            "SELECT * FROM events WHERE entry_id = ? ORDER BY id", (entry_id,)
        )
        return [_as_dict(row) for row in rows]


def all_events() -> list[dict]:
    """Every event, oldest first (for the dashboard)."""
    with closing(_connect()) as connection:
        return [_as_dict(row) for row in connection.execute("SELECT * FROM events ORDER BY id")]


def card_problems(status: str, state: dict) -> dict[str, str]:
    """Open Teams cards (or a Power BI request, slot "power_bi") never answered after every resend: slot -> why (D-037)."""
    state = state or {}
    failed = {slot: record["failed"] for slot, record in state.get("card_delivery", {}).items() if record.get("failed")}
    if status == HELD:
        return {slot: why for slot, why in failed.items() if slot == "correction" and "correction_card" in state}
    if status == MANUAL_REVIEW:
        return {slot: why for slot, why in failed.items() if slot == "review" and "review_card" in state}
    if status == WAITING_PBI:
        return {slot: why for slot, why in failed.items() if slot == "power_bi"}  # Flow 1 (D-049)
    if status == WAITING_TEAM:
        return {
            slot: why for slot, why in failed.items()
            if slot in state.get("cards", {}) and slot not in state.get("chosen", {})
        }
    return {}
