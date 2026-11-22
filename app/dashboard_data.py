"""Numbers behind the dashboard, computed from tracking events and training data.

Pure functions (no Streamlit), so every figure on the Overview can be tested.
Times are stored in UTC and shown in local time, UTC+8 (`config.LOCAL_TIME`).
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from . import config, tracking

# Outcome of one email, in the fixed order the charts use (colour follows it).
FILES_SENT = "Files sent"
NOT_RECEIVED = "Replied, no POD attached"  # its numbers not received yet, or not in our system
WAITING = "Held or waiting"
HANDLED = "Handled by team"
NOT_POD = "Not a POD request"
OUTCOMES = (FILES_SENT, NOT_RECEIVED, WAITING, HANDLED, NOT_POD)

# Outcome of one requested number.
NUMBER_FOUND = "Found on share"
NUMBER_CHOSEN = "Chosen by team"
NUMBER_NOT_RECEIVED = "Not received"
NUMBER_UNKNOWN = "Not in Power BI"  # waiting for the team, or confirmed not in our system (D-045)
NUMBER_HANDLED = "Handled by team"  # the email was resolved by a person
NUMBER_PENDING = "Still waiting"
NUMBER_OUTCOMES = (NUMBER_FOUND, NUMBER_CHOSEN, NUMBER_NOT_RECEIVED, NUMBER_UNKNOWN, NUMBER_HANDLED, NUMBER_PENDING)
# Numbers with a result: the weekly share found is worked out over these.
NUMBER_DECIDED = (NUMBER_FOUND, NUMBER_CHOSEN, NUMBER_NOT_RECEIVED, NUMBER_UNKNOWN)
# Still open for the agent, Power BI, the team or a person.
_OPEN = (*tracking.OPEN_STATUSES, *tracking.NEEDS_PEOPLE)

AGE_BUCKETS = ("< 1 hour", "1-4 hours", "4-24 hours", "1-3 days", "> 3 days")


def local_now() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(config.LOCAL_TIME))


def _local(value) -> pd.Timestamp:
    return pd.Timestamp(value).tz_convert(config.LOCAL_TIME)


def email_date(value) -> pd.Timestamp:
    """When the email arrived, as Outlook's local wall-clock time; NaT if unknown.

    pywin32 may label Outlook's local time as UTC, so a time zone on the
    stored value is dropped, not converted (as in `outlook_reader._utc_iso`).
    """
    stamp = pd.to_datetime(str(value or ""), errors="coerce")
    if pd.isna(stamp):
        return pd.NaT
    return stamp.tz_localize(None) if stamp.tzinfo is not None else stamp


def current_entry_id(entry_id: str, events: list[dict]) -> str:
    """The email's Outlook entry ID now: the new one if the agent moved it (D-031)."""
    moved = [event["state"]["moved_entry_id"] for event in events if (event.get("state") or {}).get("moved_entry_id")]
    return moved[-1] if moved else entry_id


def email_outcome(status: str, state: dict) -> str:
    """Which of OUTCOMES an email is in now."""
    if status == tracking.REPLIED:
        return FILES_SENT if state.get("found") or state.get("chosen") else NOT_RECEIVED
    if status == tracking.RESOLVED:
        return HANDLED
    if status in (tracking.SKIPPED_GUARD, tracking.SKIPPED_NO_NUMBER, tracking.OTHER_INTENT, tracking.REVIEWED_OTHER):
        return NOT_POD
    return WAITING


def emails(events: list[dict]) -> pd.DataFrame:
    """One row per email: received (email date), first seen (local time), current status, outcome."""
    columns = [
        "entry_id", "received", "first_seen", "status", "reason", "sender_email", "subject",
        "numbers", "intent", "confidence", "state", "outcome", "teams_request_ids", "teams_problem",
    ]
    if not events:
        return pd.DataFrame(columns=columns)
    frame = pd.DataFrame(events)
    first_seen = frame.groupby("entry_id")["at_utc"].min().map(_local)
    latest = frame.sort_values("id").groupby("entry_id").tail(1).set_index("entry_id")
    latest["first_seen"] = first_seen
    if "confidence" not in latest.columns:
        latest["confidence"] = None
    received = latest["received_time"] if "received_time" in latest.columns else pd.Series("", index=latest.index)
    latest["received"] = pd.to_datetime(received.map(email_date))
    latest["outcome"] = [email_outcome(s, st) for s, st in zip(latest["status"], latest["state"])]
    latest["teams_request_ids"] = [
        ", ".join(str(request_id) for request_id in (state or {}).get("cards", {}).values())
        if status == tracking.WAITING_TEAM
        else str((state or {}).get("correction_card", "")) if status == tracking.HELD
        else str((state or {}).get("review_card", "")) if status == tracking.MANUAL_REVIEW
        else str((state or {}).get("pbi_request", "")) if status == tracking.WAITING_PBI
        else ""
        for status, state in zip(latest["status"], latest["state"])
    ]
    # Cards that never reached Teams after every resend (D-037).
    latest["teams_problem"] = [
        "; ".join(f"{slot}: {why}" for slot, why in tracking.card_problems(status, state).items())
        for status, state in zip(latest["status"], latest["state"])
    ]
    return latest.reset_index()[columns].sort_values("first_seen").reset_index(drop=True)


def history(
    table: pd.DataFrame,
    statuses: list[str] | None = None,
    outcomes: list[str] | None = None,
    sender: str = "",
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    intents: list[str] | None = None,
) -> pd.DataFrame:
    """Every processed email matching the filters, newest email first (the History tab).

    Dates are the email's own (received), local; an email without one is
    kept unless a date filter is set.
    """
    keep = pd.Series(True, index=table.index)
    if statuses:
        keep &= table["status"].isin(statuses)
    if outcomes:
        keep &= table["outcome"].isin(outcomes)
    if intents:
        keep &= table["intent"].fillna("").astype(str).str.strip().isin(intents)
    if sender:
        keep &= table["sender_email"].fillna("").str.casefold().str.contains(sender.casefold(), regex=False)
    if start is not None:
        keep &= table["received"] >= start
    if end is not None:
        keep &= table["received"] < end + pd.Timedelta(days=1)
    return table[keep].sort_values("received", ascending=False, na_position="last")


def can_train_on(status_history: list[str]) -> bool:
    """Whether a person may add the email to the training data from History.

    Not the guard's emails (own mailbox, auto-replies, bounces, no-reply):
    the intent model never sees them, so they teach it nothing it will meet.
    """
    return tracking.SKIPPED_GUARD not in status_history


def is_pod_request(row) -> bool:
    """A POD request as far as the agent or a person knows: not an email the model called OTHER (D-052)."""
    return row["outcome"] != NOT_POD and str(row.get("intent") or "") != "OTHER"


def _pod_requests(table: pd.DataFrame) -> pd.DataFrame:
    return table[table.apply(is_pod_request, axis=1)] if not table.empty else table


def kpis(table: pd.DataFrame, since: pd.Timestamp) -> dict[str, int]:
    """Headline counts for emails first seen at or after `since` (D-052)."""
    recent = table[table["first_seen"] >= since] if not table.empty else table
    if recent.empty:
        return {name: 0 for name in ("Emails received", "POD requests", "Replied with files",
                                     "Replies with a POD missing", "Held for the team", "Manual Review")}
    replied = recent[recent["status"] == tracking.REPLIED]
    return {
        "Emails received": len(recent),
        "POD requests": len(_pod_requests(recent)),
        "Replied with files": int((replied["outcome"] == FILES_SENT).sum()),
        # Not received yet, or not in our system (D-045): the customer still lacks one.
        "Replies with a POD missing": int(
            sum(bool(state.get("not_received") or state.get("not_in_system")) for state in replied["state"])),
        "Held for the team": int(recent["status"].isin([tracking.HELD, tracking.ERROR]).sum()),
        "Manual Review": int((recent["status"] == tracking.MANUAL_REVIEW).sum()),
    }


def daily_volume(table: pd.DataFrame, days: int = 30, now: pd.Timestamp | None = None) -> pd.DataFrame:
    """Emails per day by outcome for the last `days` days, zeros included."""
    # Days are local calendar days, the same zone as `first_seen`.
    now = _local(now if now is not None else local_now())
    start = (now - pd.Timedelta(days=days - 1)).normalize()
    day_index = pd.date_range(start, now.normalize(), freq="D")
    grid = pd.MultiIndex.from_product([day_index, OUTCOMES], names=["day", "outcome"])
    if table.empty:
        counts = pd.Series(0, index=grid)
    else:
        recent = table[table["first_seen"] >= start]
        counts = (
            recent.assign(day=recent["first_seen"].dt.normalize())
            .groupby(["day", "outcome"]).size()
            .reindex(grid, fill_value=0)
        )
    return counts.rename("emails").reset_index()


def number_outcomes(table: pd.DataFrame) -> pd.DataFrame:
    """One row per requested number of POD-request emails, with its outcome (D-052)."""
    rows = []
    for _, email in _pod_requests(table).iterrows():
        state = email["state"] or {}
        decided = set()
        for key, outcome in (
            ("found", NUMBER_FOUND),
            ("same_as", NUMBER_FOUND),  # a reference attached as another number's scan (D-044)
            ("chosen", NUMBER_CHOSEN),
            ("not_received", NUMBER_NOT_RECEIVED),
            ("unknown", NUMBER_UNKNOWN),
            ("not_in_system", NUMBER_UNKNOWN),
        ):
            for number in state.get(key, []):
                if number not in decided:
                    rows.append((email["first_seen"], number, outcome))
                    decided.add(number)
        for number in [n for n in str(email["numbers"] or "").split(";") if n and n not in decided]:
            # Resolved by a person; still open; or, once replied, a number read from an
            # attachment that another made unneeded (D-044), which is not counted.
            if email["status"] == tracking.RESOLVED:
                rows.append((email["first_seen"], number, NUMBER_HANDLED))
            elif email["status"] in _OPEN:
                rows.append((email["first_seen"], number, NUMBER_PENDING))
    return pd.DataFrame(rows, columns=["first_seen", "number", "outcome"])


def weekly_hit_rate(numbers: pd.DataFrame) -> pd.DataFrame:
    """Per week: share of the numbers with a result whose scan was on the share."""
    decided = numbers[numbers["outcome"].isin(NUMBER_DECIDED)] if not numbers.empty else numbers
    if decided.empty:
        return pd.DataFrame(columns=["week", "hit_rate", "numbers"])
    week = decided["first_seen"].dt.tz_localize(None).dt.to_period("W").dt.start_time
    found = decided["outcome"].isin([NUMBER_FOUND, NUMBER_CHOSEN])
    grouped = pd.DataFrame({"week": week, "found": found}).groupby("week")["found"]
    return pd.DataFrame(
        {"hit_rate": grouped.mean(), "numbers": grouped.size()}
    ).reset_index()


def system_agreement(training: pd.DataFrame) -> pd.DataFrame:
    """Per pull: share of reviewed agent decisions the product owner left unchanged."""
    needed = {"label_source", "system_pulled_at", "intent_label", "intent_label_pseudo"}
    if training.empty or not needed.issubset(training.columns):
        return pd.DataFrame(columns=["pulled", "agreement", "reviewed"])
    pulled = training[training["system_pulled_at"].fillna("").astype(str).str.strip() != ""]
    reviewed = pulled[
        (pulled["label_source"].fillna("") == "Manual")
        & (pulled["intent_label_pseudo"].fillna("").astype(str).str.strip() != "")
    ]
    if reviewed.empty:
        return pd.DataFrame(columns=["pulled", "agreement", "reviewed"])
    same = reviewed["intent_label"].astype(str).str.strip() == reviewed["intent_label_pseudo"].astype(str).str.strip()
    day = pd.to_datetime(reviewed["system_pulled_at"], utc=True).dt.tz_convert(config.LOCAL_TIME).dt.normalize().dt.tz_localize(None)
    grouped = pd.DataFrame({"pulled": day, "same": same}).groupby("pulled")["same"]
    return pd.DataFrame({"agreement": grouped.mean(), "reviewed": grouped.size()}).reset_index()


def top_domains(table: pd.DataFrame, limit: int = 10) -> pd.DataFrame:
    """Sender domains with the most POD requests."""
    requests = _pod_requests(table)
    if requests.empty:
        return pd.DataFrame(columns=["domain", "requests"])
    domain = requests["sender_email"].fillna("").str.split("@").str[-1].str.lower().replace("", "(unknown)")
    counts = domain.value_counts().head(limit)
    return pd.DataFrame({"domain": counts.index, "requests": counts.values})


def queue(table: pd.DataFrame, now: pd.Timestamp | None = None, *, other_intent: bool = True) -> pd.DataFrame:
    """Emails that wait for Power BI, the team or a person, with their age.

    `other_intent` off leaves out emails that are not POD requests, which
    the team handles in Outlook (the Overview's waiting chart, D-052).
    """
    open_statuses = [status for status in _OPEN if other_intent or status != tracking.OTHER_INTENT]
    waiting = table[table["status"].isin(open_statuses)].copy() if not table.empty else table.copy()
    if waiting.empty:
        return waiting.assign(age_hours=[], age=[])
    now = now or local_now()
    waiting["age_hours"] = (now - waiting["first_seen"]).dt.total_seconds() / 3600
    waiting["age"] = pd.cut(
        waiting["age_hours"], bins=[-1, 1, 4, 24, 72, float("inf")], labels=AGE_BUCKETS
    ).astype(str)
    return waiting.sort_values("first_seen")


def queue_age(waiting: pd.DataFrame) -> pd.DataFrame:
    """Waiting emails per age bucket, every bucket shown."""
    counts = (
        waiting["age"].value_counts().reindex(AGE_BUCKETS, fill_value=0)
        if not waiting.empty
        else pd.Series(0, index=AGE_BUCKETS)
    )
    return pd.DataFrame({"age": list(AGE_BUCKETS), "emails": counts.values})
