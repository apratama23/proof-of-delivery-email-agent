"""History tab: every processed email, its processing, adding it to the training data."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from app import classifier, config, dashboard_data as dd, handoff, labeling, main, tracking, training_refresh
from app.training_refresh import MANUAL
from dashboard.common import (
    OPEN_POPUP, POPUP_BODY_HEIGHT, _emails, _load_training, _open_in_outlook_button, close_popup, open_popup,
)


def _processing_details(events: list[dict]) -> str:
    """What the agent did with one email, as Markdown: its statuses, then its working data."""
    lines = ["**Statuses**"]
    for event in events:
        at = dd._local(event["at_utc"]).strftime("%d %b %Y %H:%M")
        numbers = "; ".join(n for n in str(event["numbers"] or "").split(";") if n)
        # Numbers per status: the original extraction and a correction can be told apart (D-035).
        lines.append(f"- {at} · {event['status']}" + "".join(f" · {part}" for part in (event["reason"], numbers) if part))
    state = events[-1]["state"] or {}
    name = lambda path: Path(str(path)).name
    details = [
        ("Files found", ", ".join(f"{number}: {name(path)}" for number, path in state.get("found", {}).items())),
        ("Several files", ", ".join(f"{number}: {len(paths)} files" for number, paths in state.get("ambiguous", {}).items())),
        ("Chosen by the team", ", ".join(
            f"{number}: {', '.join(name(p) for p in ([paths] if isinstance(paths, str) else paths))}"
            for number, paths in state.get("chosen", {}).items()
        )),
        ("Not received (in Power BI)", ", ".join(state.get("not_received", {}))),
        ("Not in Power BI", ", ".join(state.get("unknown", []))),
        ("Reviewed as", state["review"]["intent"] if state.get("review") else ""),
        ("Numbers from", state.get("number_source", "")),  # an attachment, when the text had none (D-044)
        ("Attached", ", ".join(name(path) for path in state.get("attachments", []))),
        ("Draft outcome", state.get("pilot", "")),
    ]
    lines += ["", "**Working data**"] + [f"- {label}: {value}" for label, value in details if value]
    if state.get("reply"):
        lines += ["", "**Reply text**", "", "```", state["reply"], "```"]
    return "\n".join(lines)



@st.dialog("Email history", width="large", on_dismiss=close_popup)
def _history_popup(entry_id: str, training_label: str) -> None:
    events = tracking.history(entry_id)
    entry = events[-1]
    numbers = [n for n in str(entry["numbers"] or "").split(";") if n]
    received = dd.email_date(entry["received_time"])
    received_text = f" · Received {received:%d %b %Y %H:%M}" if pd.notna(received) else ""
    st.markdown(f"**{labeling.highlight(entry['subject'], numbers)}**", unsafe_allow_html=True)
    st.caption(f"From {entry['sender_email']}{received_text} · {entry['status']} · In the training data: {training_label}")
    _open_in_outlook_button(entry_id, f"history_{handoff.request_id(entry_id)}")
    email_tab, processing_tab = st.tabs(["Email", "Processing"])
    with email_tab:
        with st.container(height=POPUP_BODY_HEIGHT, border=True):
            st.markdown(labeling.highlight(entry["body"], numbers), unsafe_allow_html=True)
    with processing_tab:
        with st.container(height=POPUP_BODY_HEIGHT, border=True):
            st.markdown(_processing_details(events))

    if not dd.can_train_on([event["status"] for event in events]):
        st.info(
            "Skipped by the guard (own mailbox, auto-reply, bounce or no-reply sender): the intent model never "
            "sees such emails, so they are not added to the training data. If a real POD request was skipped, "
            "the guard rule needs changing.",
            icon=":material/block:",
        )
        return
    key = handoff.request_id(entry_id)
    first, second, third = st.columns(3)
    intent = first.selectbox("Intent", ["", *classifier.INTENT_CLASSES], key=f"history_intent_{key}")
    connotes = second.text_input("Connotes (; separated)", ";".join(numbers), key=f"history_connotes_{key}")
    references = third.text_input("References (; separated)", "", key=f"history_references_{key}")
    if st.button("Add to training data", type="primary", icon=":material/add:", disabled=not intent):
        review = {"intent": intent, "connotes": main.split_numbers(connotes), "references": main.split_numbers(references)}
        try:
            changed = training_refresh.add_review(config.TRAINING_DATA_PATH, entry, review, label_source=MANUAL)
        except Exception as error:
            st.error(f"Not added ({error}). Close {Path(config.TRAINING_DATA_PATH).name} if it is open in Excel, then try again.")
        else:
            st.success(f"Added to the training data as {intent} (Manual)." + (f" Label {changed}." if changed else ""))



def render_history() -> None:
    table = _emails()
    if table.empty:
        st.info("No emails processed yet.")
        return
    with st.expander("Filters", expanded=True):
        first, second, third, fourth = st.columns(4)
        statuses = first.multiselect("Status", sorted(table["status"].unique()), key="h_status")
        outcomes = second.multiselect("Outcome", list(dd.OUTCOMES), key="h_outcome")
        sender = first.text_input("Sender contains", key="h_sender")
        intent_options = sorted(table["intent"].fillna("").astype(str).str.strip().unique())
        intents = third.multiselect(
            "Intent", intent_options, format_func=lambda intent: intent or "No intent", key="h_intent",
        )
        dates = fourth.date_input("Email date between", value=(), key="h_dates")
    view = dd.history(
        table, statuses, outcomes, sender.strip(),
        pd.Timestamp(dates[0]) if len(dates) >= 1 else None,
        pd.Timestamp(dates[1]) if len(dates) == 2 else None,
        intents=intents,
    )
    marks = labeling.training_marks(_load_training())
    st.caption(
        f"{len(view)} of {len(table)} emails, newest first, {sum(entry in marks for entry in view['entry_id'])} "
        "of them in the training data. Select one and open it to see its processing."
    )
    shown = pd.DataFrame({
        "Email date": view["received"].dt.strftime("%d %b %Y %H:%M").fillna(""),
        "Sender": view["sender_email"],
        "Subject": view["subject"],
        "Status": view["status"],
        "Outcome": view["outcome"],
        "Intent": [
            f"{intent} ({confidence:.0%})" if intent and pd.notna(confidence) else (intent or "")
            for intent, confidence in zip(view["intent"], pd.to_numeric(view["confidence"], errors="coerce"))
        ],
        "Numbers": view["numbers"],
        "In training data": [marks.get(entry_id, "") for entry_id in view["entry_id"]],
        "Reason": view["reason"],
    })
    selection = st.dataframe(
        shown, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row", key="history_table",
        column_config={"In training data": st.column_config.TextColumn(help="Its label there and who set it; empty: not pulled yet")},
    )
    rows = selection.selection.rows if selection else []
    if st.button("Open selected", icon=":material/open_in_new:", disabled=not rows, type="primary"):
        st.session_state["history_entry"] = view.iloc[rows[0]]["entry_id"]
        open_popup("history")
    if st.session_state.get(OPEN_POPUP) == "history":
        entry_id = st.session_state["history_entry"]
        _history_popup(entry_id, marks.get(entry_id, "not yet"))
