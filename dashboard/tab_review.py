"""Review queue tab: waiting and held emails, resolve, process again, review pop-up."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from app import classifier, config, dashboard_data as dd, handoff, labeling, main, tracking, training_refresh
from dashboard.common import (
    OPEN_POPUP, POPUP_BODY_HEIGHT, _emails, _move_buttons, _open_in_outlook_button, _unsaved_warning, close_popup,
    open_popup,
)


@st.dialog("Review email", width="large", on_dismiss=close_popup)
def _review_popup() -> None:
    """Decide an email the intent model was unsure of; the agent continues it (main.record_review)."""
    rows = st.session_state["review_rows"]
    position = st.session_state["review_position"]
    if notice := st.session_state.pop("review_notice", None):
        st.toast(notice)
    if problem := st.session_state.pop("review_problem", None):
        st.error(problem)
    latest = {event["entry_id"]: event for event in tracking.latest()}
    entry = latest[rows[position]]
    numbers = [n for n in str(entry["numbers"] or "").split(";") if n]
    confidence = f" ({entry['confidence']:.0%})" if entry["confidence"] is not None else ""
    received = dd.email_date(entry["received_time"])
    st.markdown(f"**{labeling.highlight(entry['subject'], numbers)}**", unsafe_allow_html=True)
    received_text = f" · Received {received:%d %b %Y %H:%M}" if pd.notna(received) else ""
    st.caption(f"Email {position + 1} of {len(rows)} · From {entry['sender_email']}{received_text}")
    _open_in_outlook_button(entry["entry_id"], f"review_{handoff.request_id(entry['entry_id'])}")
    st.caption(f"{entry['status']}: {entry['reason'] or '-'} · Model says {entry['intent'] or '-'}{confidence}")
    with st.container(height=POPUP_BODY_HEIGHT, border=True):
        st.markdown(labeling.highlight(entry["body"], numbers), unsafe_allow_html=True)

    key = handoff.request_id(entry["entry_id"])
    intents = ["", *classifier.INTENT_CLASSES]
    # A held email already passed the model as a POD request; only its numbers need a look (D-035).
    default_intent = "POD_REQUEST" if entry["status"] == tracking.HELD else ""
    first, second, third = st.columns(3)
    intent = first.selectbox(
        "Intent", intents, index=intents.index(default_intent), key=f"review_intent_{key}",
        help="POD_REQUEST: the agent continues with these numbers on its next run, or with none typed, "
             "with the numbers in its attachments. OTHER: nothing is sent.",
    )
    connotes = second.text_input("Connotes (; separated)", ";".join(numbers), key=f"review_connotes_{key}")
    references = third.text_input("References (; separated)", "", key=f"review_references_{key}")
    if intent == "POD_REQUEST":
        # The final list as the agent will use it, before Save (D-035).
        typed = main.split_numbers(connotes)
        typed += [number for number in main.split_numbers(references) if number not in typed]
        try:
            main.check_numbers(typed)
            st.caption(f"Will look up: {', '.join(typed) or 'the numbers in its attachments (none typed)'}")
        except ValueError as error:
            st.warning(str(error))

    changed = (intent, connotes, references) != (default_intent, ";".join(numbers), "")

    def save_and_move(direction: str) -> None:
        """Record the review, then show the next ("next") or previous ("previous") email."""
        if not intent:
            st.error("Choose an intent to save.")
            return
        try:
            status, review = main.record_review(entry, intent, connotes, references)
        except main.AlreadyRunning:
            st.error("The agent is running now; nothing saved. Try again in a minute.")
        except ValueError as error:
            st.error(str(error))
        else:
            # The review is a manual label too (product owner): into the training data, by entry ID.
            try:
                changed = training_refresh.add_review(config.TRAINING_DATA_PATH, entry, review)
                training_note = f" Training data: label {changed}." if changed else " Added to the training data."
            except Exception as error:
                training_note = ""
                st.session_state["review_problem"] = (
                    f"The review is saved, but it could not be added to the training data ({error}). "
                    f"Close {Path(config.TRAINING_DATA_PATH).name} if it is open in Excel, then label this email in the Labeling tab."
                )
            new_rows, new_position = labeling.step_after_save(rows, position, False, direction)
            st.session_state["review_rows"], st.session_state["review_position"] = new_rows, new_position
            notice = (
                "Saved: the agent continues it on its next run (within 5 minutes)."
                if status == tracking.REVIEWED else "Saved as OTHER: nothing is sent."
            ) + training_note
            if new_position < 0:
                st.session_state["queue_notice"] = notice + " No emails left to review."
                close_popup()
                st.rerun()  # closes the pop-up and refreshes the queue
            st.session_state["review_notice"] = notice
            st.rerun(scope="fragment")

    previous, save, following = st.columns(3)
    move = _move_buttons(previous, following, position, len(rows), changed, f"review_pending_{key}", "review_position")
    if save.button("Save", type="primary", icon=":material/save:", width="stretch", disabled=not intent):
        st.session_state.pop(f"review_pending_{key}", None)
        save_and_move("next")
    if move:
        _unsaved_warning(
            move, [f"review_{name}_{key}" for name in ("intent", "connotes", "references")],
            "review_position", position, save_and_move, f"review_pending_{key}",
        )


def _selected_queue_rows(view: pd.DataFrame, positions: list[int]) -> pd.DataFrame:
    return view.iloc[[position for position in positions if 0 <= position < len(view)]]



def render_review_queue() -> None:
    if notice := st.session_state.pop("queue_notice", None):
        st.toast(notice)
    if st.session_state.get(OPEN_POPUP) == "review":
        _review_popup()
    waiting = dd.queue(_emails())
    if waiting.empty:
        st.info("No emails are waiting or held.")
        return
    statuses = sorted(waiting["status"].unique())
    chosen = st.multiselect("Status", statuses, default=statuses, key="queue_status")
    view = waiting[waiting["status"].isin(chosen)]
    shown = view.assign(
        received=view["received"].dt.strftime("%d %b %Y %H:%M").fillna(""),
        first_seen=view["first_seen"].dt.strftime("%d %b %H:%M"),
        age_hours=view["age_hours"].round(1),
        teams_request_ids=view["teams_request_ids"],
    )[
        [
            "received", "first_seen", "age", "status", "teams_request_ids",
            "reason", "sender_email", "subject", "numbers",
        ]
    ].rename(columns={
        "received": "Email date", "first_seen": "First seen", "age": "Waiting", "status": "Status", "reason": "Reason",
        "teams_request_ids": "Request ID", "sender_email": "Sender", "subject": "Subject", "numbers": "Numbers",
    })
    st.caption(
        f"{len(view)} emails. Held emails stay unread in Outlook. Select rows and mark them "
        "resolved once the team has handled them; the history is kept."
    )
    unanswered = view[(view["teams_problem"] != "") & (view["status"] == tracking.WAITING_PBI)]
    if not unanswered.empty:
        # D-049: the agent stopped writing these requests again; a person checks Flow 1.
        st.warning(
            f"{len(unanswered)} email(s) have a Power BI request Flow 1 has not answered, after "
            f"{main.CARD_RESENDS} resends. Look up the request ID in Flow 1's run history. Once Flow 1 "
            "works, select the emails and use **Reprocess in Power BI**.\n\n"
            + "\n".join(f"- {row.subject} · {row.teams_request_ids}" for row in unanswered.itertuples()),
            icon=":material/warning:",
        )
    stuck = view[(view["teams_problem"] != "") & (view["status"] != tracking.WAITING_PBI)]
    if not stuck.empty:
        # D-037: the agent stopped posting these cards; a person checks Flow 3.
        st.warning(
            f"{len(stuck)} email(s) have a Teams card that never reached Teams, after "
            f"{main.CARD_RESENDS} resends. Look up the Teams request ID in Flow 3's run history. Once Flow 3 "
            "works, select the emails and use **Send Teams card again**, or answer them here.\n\n"
            + "\n".join(f"- {row.subject} · {row.teams_request_ids} · {row.teams_problem}" for row in stuck.itertuples()),
            icon=":material/warning:",
        )
    selection = st.dataframe(
        shown,
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="multi-row",
        key=f"queue_table_{hash(tuple(view['entry_id']))}",
    )
    rows = selection.selection.rows if selection else []
    selected = _selected_queue_rows(view, rows)
    selected_ids = selected["entry_id"].tolist()
    selected_statuses = set(selected["status"])
    can_reprocess = bool(selected_ids) and selected_statuses <= set(tracking.REPROCESSABLE)
    can_reprocess_pbi = bool(selected_ids) and selected_statuses == {tracking.WAITING_PBI}
    can_send_cards = bool(selected_ids) and ((selected["teams_problem"] != "") & (selected["status"] != tracking.WAITING_PBI)).all()
    resolve, again, reprocess_pbi, send_cards = st.columns(4)
    if resolve.button(
        f"Mark {len(selected_ids)} selected as resolved",
        disabled=not selected_ids,
        type="primary",
        width="stretch",
    ):
        st.session_state["queue_pending"] = {"action": "resolve", "ids": selected_ids}
    if again.button(
        f"Process {len(selected_ids)} selected again",
        icon=":material/replay:",
        disabled=not can_reprocess,
        width="stretch",
        help="The agent reads these emails again from Outlook on its next run, from scratch. Only for "
        + ", ".join(tracking.REPROCESSABLE) + "; the email must still be unread in Outlook.",
    ):
        st.session_state["queue_pending"] = {"action": "again", "ids": selected_ids}
    if reprocess_pbi.button(
        f"Reprocess {len(selected_ids)} in Power BI",
        icon=":material/refresh:",
        disabled=not can_reprocess_pbi,
        width="stretch",
        help="Rebuild the Power BI request with the current code, archive the old request/result, "
        "and create a fresh request for the existing scan-missing numbers.",
    ):
        st.session_state["queue_pending"] = {"action": "pbi", "ids": selected_ids}
    if send_cards.button(
        f"Send Teams card again ({len(selected_ids)})",
        icon=":material/send:",
        disabled=not can_send_cards,
        width="stretch",
        help="For cards that never reached Teams: posts each again with a new Teams request ID, "
        f"and up to {main.CARD_RESENDS} more resends.",
    ):
        latest = {event["entry_id"]: event for event in tracking.latest()}
        try:
            posted = main.send_cards_again([latest[entry_id] for entry_id in selected_ids])
            st.session_state["queue_notice"] = f"{posted} Teams card(s) posted again."
        except main.AlreadyRunning:
            st.session_state["queue_notice"] = "The agent is running now; nothing changed. Try again in a minute."
        except ValueError as error:
            st.session_state["queue_notice"] = f"Nothing changed: {error}."
        st.rerun()

    reviewable =[entry_id for entry_id, status in zip(view["entry_id"], view["status"]) if status in tracking.REVIEWABLE]
    first_selected = next((entry_id for entry_id in selected_ids if entry_id in reviewable), None)
    if st.button(
        f"Review {len(reviewable)} emails one by one" + (" from the first selected" if first_selected else ""),
        icon=":material/rate_review:",
        disabled=not reviewable,
        help="Decide the intent and numbers of " + ", ".join(tracking.REVIEWABLE) + " emails; "
        "a POD request is then continued by the agent. For a held email, correct its numbers: "
        "POD Outstanding rows the agent added for numbers taken out are removed.",
    ):
        st.session_state["review_rows"] = reviewable
        st.session_state["review_position"] = reviewable.index(first_selected) if first_selected else 0
        open_popup("review")

    pending = st.session_state.get("queue_pending")
    if pending:
        count = len(pending["ids"])
        if pending["action"] == "resolve":
            st.warning(f"{count} emails will leave the review queue as Resolved. Their history is kept.")
        elif pending["action"] == "pbi":
            st.warning(
                f"{count} Power BI request(s) will be rebuilt and resubmitted. Existing request/result "
                "files will be archived so Flow 1 receives a fresh file and Python cannot read a stale result."
            )
        else:
            st.warning(
                f"{count} emails will leave the review queue and be processed again on the next agent run "
                "(within 5 minutes), with the models and settings in use then. Their history is kept."
            )
        confirm, cancel, _ = st.columns([1, 1, 4])
        if confirm.button("Confirm", type="primary", width="stretch"):
            st.session_state.pop("queue_pending")
            latest = {event["entry_id"]: event for event in tracking.latest()}
            entries = [latest[entry_id] for entry_id in pending["ids"] if entry_id in latest]
            if pending["action"] == "resolve":
                try:
                    st.session_state["queue_notice"] = f"{main.mark_resolved(entries)} emails marked resolved."
                except main.AlreadyRunning:
                    st.session_state["queue_notice"] = "The agent is running now; nothing changed. Try again in a minute."
            elif pending["action"] == "pbi":
                try:
                    done = main.reprocess_power_bi(entries)
                    st.session_state["queue_notice"] = f"{done} Power BI request(s) resubmitted."
                except main.AlreadyRunning:
                    st.session_state["queue_notice"] = "The agent is running now; nothing changed. Try again in a minute."
                except ValueError as error:
                    st.session_state["queue_notice"] = f"Nothing changed: {error}."
            else:
                try:
                    done = main.process_again(entries)
                    st.session_state["queue_notice"] = f"{done} emails will be processed again on the next run."
                except main.AlreadyRunning:
                    st.session_state["queue_notice"] = "The agent is running now; nothing changed. Try again in a minute."
                except ValueError as error:
                    st.session_state["queue_notice"] = f"Nothing changed: {error}."
            st.rerun()
        if cancel.button("Cancel", width="stretch"):
            st.session_state.pop("queue_pending")
            st.rerun()
