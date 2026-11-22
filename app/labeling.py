"""Labeling rules used by the dashboard (D-012, D-013), free of Streamlit."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from . import config
from .training_refresh import MANUAL, SYSTEM_DECISION

EDITABLE_COLUMNS = ("intent_label", "connotes_manual", "reference_number_manual")
# When a person last set a row's labels (UTC, ISO); empty for labels made before it was kept.
LABELED_AT = "labeled_at"
SORTS = ("File order", "Labeled, newest first", "Labeled, oldest first")
NUMBER_COLUMNS = (
    "connotes_manual",
    "reference_number_manual",
    "connote_number_auto",
    "ref_number_auto",
    "connotes_pseudo",
    "reference_number_pseudo",
)
LABEL_FILTERS = ("All", "Unlabeled", "System decision to review", "POD_REQUEST", "OTHER")
PSEUDO_FILTERS = ("All", "Has pseudo-label", "No pseudo-label")
# A row has a pseudo-label when a model suggested any of these.
PSEUDO_COLUMNS = ("intent_label_pseudo", "connotes_pseudo", "reference_number_pseudo")


def as_text(value) -> str:
    """A cell as stripped text; empty for blank or NaN cells (never \"nan\")."""
    return "" if value is None or pd.isna(value) else str(value).strip()


def source_of(row) -> str:
    """Who labeled a row: the stored mark, or Manual for older hand labels."""
    stored = as_text(row.get("label_source"))
    if stored:
        return stored
    return MANUAL if as_text(row.get("intent_label")) else ""


@dataclass
class Filters:
    search: str = ""
    sender: str = ""
    label: str = "All"
    source: str = "All"
    start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None
    min_confidence: float = 0.0
    max_confidence: float = 1.0
    pseudo: str = "All"
    labeled_start: pd.Timestamp | None = None
    labeled_end: pd.Timestamp | None = None


def filter_rows(data: pd.DataFrame, filters: Filters) -> pd.DataFrame:
    """The rows matching every filter; the original index is kept for saving."""
    keep = pd.Series(True, index=data.index)
    if filters.search:
        needle = filters.search.casefold()
        haystack = (
            data.get("subject", "").fillna("").astype(str) + " "
            + data.get("body", "").fillna("").astype(str) + " "
            + data.get("sender_email", "").fillna("").astype(str)
        ).str.casefold()
        keep &= haystack.str.contains(needle, regex=False)
    if filters.sender:
        keep &= data["sender_email"].fillna("").astype(str).str.casefold().str.contains(
            filters.sender.casefold(), regex=False
        )
    label = data["intent_label"].map(as_text)
    sources = data.apply(source_of, axis=1) if not data.empty else pd.Series(dtype=str)
    if filters.label == "Unlabeled":
        keep &= label.eq("") & sources.ne(SYSTEM_DECISION)
    elif filters.label == "System decision to review":
        keep &= sources.eq(SYSTEM_DECISION)
    elif filters.label in ("POD_REQUEST", "OTHER"):
        keep &= label.eq(filters.label)
    if filters.source != "All":
        keep &= sources.eq("" if filters.source == "(none)" else filters.source)
    if filters.start is not None or filters.end is not None:
        received = pd.to_datetime(data["received_time"], errors="coerce", utc=True).dt.tz_localize(None)
        if filters.start is not None:
            keep &= received >= filters.start
        if filters.end is not None:
            keep &= received < filters.end + pd.Timedelta(days=1)
    if filters.min_confidence > 0 or filters.max_confidence < 1:
        confidence = pd.to_numeric(data["pseudo_confidence"], errors="coerce")
        keep &= confidence.between(filters.min_confidence, filters.max_confidence)
    if filters.pseudo != "All":
        has_pseudo = pd.Series(False, index=data.index)
        for column in PSEUDO_COLUMNS:
            if column in data.columns:
                has_pseudo |= data[column].map(as_text).ne("")
        keep &= has_pseudo if filters.pseudo == "Has pseudo-label" else ~has_pseudo
    if filters.labeled_start is not None or filters.labeled_end is not None:
        labeled = labeled_local(data)
        if filters.labeled_start is not None:
            keep &= labeled >= filters.labeled_start
        if filters.labeled_end is not None:
            keep &= labeled < filters.labeled_end + pd.Timedelta(days=1)
    return data[keep]


def labeled_local(data: pd.DataFrame) -> pd.Series:
    """When each row was labeled, in local time (UTC+8) without a zone; NaT when not known."""
    if LABELED_AT not in data.columns:
        return pd.Series(pd.NaT, index=data.index, dtype="datetime64[ns]")
    stamps = pd.to_datetime(data[LABELED_AT].map(as_text), utc=True, errors="coerce", format="ISO8601")
    return stamps.dt.tz_convert(config.LOCAL_TIME).dt.tz_localize(None)


def sort_rows(view: pd.DataFrame, sort: str) -> pd.DataFrame:
    """The rows in the chosen order; rows without a label date go last."""
    if sort == "File order":
        return view
    labeled = labeled_local(view)
    order = labeled.sort_values(ascending=sort == "Labeled, oldest first", na_position="last").index
    return view.loc[order]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def apply_edits(data: pd.DataFrame, edited: pd.DataFrame) -> int:
    """Copy changed label cells from the editor back; mark those rows Manual."""
    changed = 0
    for index, row in edited.iterrows():
        new = {column: as_text(row.get(column)) for column in EDITABLE_COLUMNS if column in edited.columns}
        old = {column: as_text(data.at[index, column]) for column in new}
        if new != old:
            for column, value in new.items():
                data.at[index, column] = value
            data.at[index, "label_source"] = MANUAL
            data.at[index, LABELED_AT] = _now()
            changed += 1
    return changed


def recommendation(row) -> dict[str, str]:
    """The suggestions for an email: intent with its confidence, connotes, references ("" when none).

    Numbers come from the CRF's pseudo-labels; when it suggests none, from the
    numbers found automatically (master list or regex), as the live agent
    falls back to regex when the CRF finds nothing.
    """
    confidence = pd.to_numeric(row.get("pseudo_confidence"), errors="coerce")
    connotes, references = as_text(row.get("connotes_pseudo")), as_text(row.get("reference_number_pseudo"))
    source = "CRF" if connotes or references else ""
    if not source:
        connotes, references = as_text(row.get("connote_number_auto")), as_text(row.get("ref_number_auto"))
        source = "master list or regex" if connotes or references else ""
    return {
        "intent": as_text(row.get("intent_label_pseudo")),
        "confidence": f"{confidence:.2f}" if pd.notna(confidence) else "",
        "connotes": connotes,
        "references": references,
        "numbers_source": source,
        # Per-number CRF confidence as shown text, e.g. "7336608 (87%)"; plain numbers otherwise.
        "connotes_shown": _with_confidence(connotes, row.get("connotes_pseudo_confidence") if source == "CRF" else ""),
        "references_shown": _with_confidence(references, row.get("reference_pseudo_confidence") if source == "CRF" else ""),
    }


def _with_confidence(numbers: str, confidences) -> str:
    """ "A;B" with "A:0.873;B:0.6" -> "A (87%); B (60%)"; a number seen twice keeps its highest."""
    best: dict[str, float] = {}
    for part in as_text(confidences).split(";"):
        token, _, value = part.rpartition(":")
        try:
            best[token] = max(best.get(token, 0.0), float(value))
        except ValueError:
            continue
    shown = [
        f"{number} ({best[number]:.0%})" if number in best else number
        for number in (part.strip() for part in numbers.split(";")) if number
    ]
    return "; ".join(shown)


def after_save(rows: list, position: int, still_matches: bool) -> tuple[list, int]:
    """The labeling pop-up's rows and position once the email at `position` is saved.

    An email that no longer matches the filters (say "Unlabeled") leaves the
    list, so the next one takes its place; one that still matches is stepped
    over. Position -1 means no email is left.
    """
    if still_matches:
        return rows, min(position + 1, len(rows) - 1)
    rows = rows[:position] + rows[position + 1:]
    return rows, min(position, len(rows) - 1)


def training_marks(training: pd.DataFrame) -> dict[str, str]:
    """For each entry ID in the training data, its label and who set it, e.g. "POD_REQUEST (Manual)"."""
    if training.empty or "entry_id" not in training.columns:
        return {}
    return {
        as_text(row.get("entry_id")): f"{as_text(row.get('intent_label')) or 'no label'} ({source_of(row) or 'nobody'})"
        for _, row in training.iterrows()
        if as_text(row.get("entry_id"))
    }


def step_after_save(rows: list, position: int, still_matches: bool, direction: str) -> tuple[list, int]:
    """Like `after_save`, then on to the next or the previous email ("next" or "previous")."""
    new_rows, new_position = after_save(rows, position, still_matches)
    if direction == "previous" and new_position >= 0:
        new_position = min(max(position - 1, 0), len(new_rows) - 1)
    return new_rows, new_position


def has_label_edits(editor_state) -> bool:
    """True when the labeling table holds unsaved label edits (ticks in Select do not count)."""
    edited_rows = (editor_state or {}).get("edited_rows", {})
    return any(set(changes) - {"select"} for changes in edited_rows.values())


def set_intent(data: pd.DataFrame, rows, intent: str) -> int:
    """Bulk label: give every row in `rows` the same intent, marked Manual."""
    for index in rows:
        data.at[index, "intent_label"] = intent
        data.at[index, "label_source"] = MANUAL
        data.at[index, LABELED_AT] = _now()
    return len(rows)


def confirm_system_decisions(data: pd.DataFrame, rows) -> int:
    """Accept the agent's intent for the given rows that still await review."""
    confirmed = 0
    for index in rows:
        decision = as_text(data.at[index, "intent_label_pseudo"])
        if source_of(data.loc[index]) == SYSTEM_DECISION and decision:
            data.at[index, "intent_label"] = decision
            data.at[index, "label_source"] = MANUAL
            data.at[index, LABELED_AT] = _now()
            confirmed += 1
    return confirmed


def numbers_in(row) -> list[str]:
    """Every number any column says is in this email, longest first."""
    values = {
        part.strip()
        for column in NUMBER_COLUMNS
        for part in re.split(r"[;,\n]+", as_text(row.get(column)))
        if part.strip()
    }
    return sorted(values, key=len, reverse=True)


def highlight(text: str, numbers: list[str]) -> str:
    """Email text as safe HTML with each number wrapped in <mark>."""
    escaped = html.escape(text or "")
    if numbers:
        pattern = re.compile("|".join(re.escape(html.escape(number)) for number in numbers), re.IGNORECASE)
        escaped = pattern.sub(lambda match: f"<mark>{match.group(0)}</mark>", escaped)
    return escaped.replace("\n", "<br>")
