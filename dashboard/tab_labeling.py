"""Labeling tab: the training data table, filters and the one-by-one pop-up."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app import classifier, labeling
from app.training_refresh import MANUAL, MANUAL_REVIEW, SYSTEM_DECISION
from dashboard.common import (
    OPEN_POPUP, POPUP_BODY_HEIGHT, _load_training, _move_buttons, _open_in_outlook_button, _save_training,
    _unsaved_warning, close_popup, open_popup,
)


@st.dialog("Label email", width="large", on_dismiss=close_popup)
def _label_popup(data: pd.DataFrame, filters: labeling.Filters) -> None:
    rows = st.session_state["popup_rows"]
    position = st.session_state["popup_position"]
    if notice := st.session_state.pop("popup_notice", None):
        st.toast(notice)
    index = rows[position]
    row = data.loc[index]
    numbers = labeling.numbers_in(row)

    st.markdown(f"**{labeling.highlight(labeling.as_text(row['subject']), numbers)}**", unsafe_allow_html=True)
    st.caption(
        f"Email {position + 1} of {len(rows)} · From {labeling.as_text(row['sender_email'])} · "
        f"Received {labeling.as_text(row['received_time'])} · Labeled by {labeling.source_of(row) or 'nobody yet'} · "
        f"Numbers in any column: {', '.join(numbers) or '-'}"
    )
    if labeling.as_text(row.get("entry_id")):  # rows typed in by hand have none
        _open_in_outlook_button(labeling.as_text(row["entry_id"]), f"label_{index}")
    # Only the email body scrolls, so the label fields and buttons stay on screen.
    with st.container(height=POPUP_BODY_HEIGHT, border=True):
        st.markdown(labeling.highlight(labeling.as_text(row["body"]), numbers), unsafe_allow_html=True)

    # Keys carry the row index, so each email's inputs start from its own values.
    keys = {name: f"popup_{name}_{index}" for name in ("intent", "connotes", "references")}
    suggested = labeling.recommendation(row)
    has_suggestion = bool(suggested["intent"] or suggested["connotes"] or suggested["references"])

    def use_recommendation() -> None:
        # Only fills the fields; nothing is saved until Save.
        for name in keys:
            if suggested[name] and (name != "intent" or suggested[name] in classifier.INTENT_CLASSES):
                st.session_state[keys[name]] = suggested[name]

    intents = ["", *classifier.INTENT_CLASSES]
    current = labeling.as_text(row["intent_label"])
    first, second, third = st.columns(3)
    if has_suggestion:
        # Each pseudo-label above its own field, so it reads next to what it fills.
        confidence = (
            f" ({float(suggested['confidence']):.0%})" if suggested["intent"] and suggested["confidence"] else ""
        )
        source = f" · {suggested['numbers_source']}" if suggested["numbers_source"] else ""
        shown = (
            (first, suggested["intent"], confidence),
            (second, suggested["connotes_shown"], source if suggested["connotes"] else ""),
            (third, suggested["references_shown"], source if suggested["references"] else ""),
        )
        for column, value, extra in shown:
            column.caption(f":material/lightbulb: Recommended: **{value or '-'}**{extra}")
    intent = first.selectbox("Intent", intents, index=intents.index(current) if current in intents else 0, key=keys["intent"])
    connotes = second.text_input("Connotes (; separated)", labeling.as_text(row["connotes_manual"]), key=keys["connotes"])
    references = third.text_input(
        "References (; separated)", labeling.as_text(row["reference_number_manual"]), key=keys["references"]
    )

    changed = (intent, connotes, references) != (
        current, labeling.as_text(row["connotes_manual"]), labeling.as_text(row["reference_number_manual"])
    )

    def save_and_move(direction: str) -> None:
        """Save this email's fields, then show the next ("next") or previous ("previous") one."""
        edit = pd.DataFrame(
            [{"intent_label": intent, "connotes_manual": connotes, "reference_number_manual": references}],
            index=[index],
        )
        if not labeling.apply_edits(data, edit):
            st.toast("Nothing changed.")
            return
        _save_training(data)
        # Positions in the table may shift once the page reloads; drop its
        # Select ticks so they cannot land on other rows.
        st.session_state.pop("label_editor", None)
        still_matches = not labeling.filter_rows(data.loc[[index]], filters).empty
        new_rows, new_position = labeling.step_after_save(rows, position, still_matches, direction)
        st.session_state["popup_rows"], st.session_state["popup_position"] = new_rows, new_position
        if new_position < 0:
            st.session_state["popup_notice"] = "Saved. No emails left in these filters."
            close_popup()
            st.rerun()  # closes the pop-up and refreshes the table
        st.session_state["popup_notice"] = "Saved (marked Manual)."
        st.rerun(scope="fragment")

    previous, use, save, following = st.columns(4)
    move = _move_buttons(previous, following, position, len(rows), changed, f"popup_pending_{index}")
    use.button(
        "Use recommendation", icon=":material/lightbulb:", on_click=use_recommendation,
        disabled=not has_suggestion, width="stretch", help="Fills the fields; check them, then Save.",
    )
    if save.button("Save", type="primary", icon=":material/save:", width="stretch"):
        st.session_state.pop(f"popup_pending_{index}", None)
        save_and_move("next")
    if move:
        _unsaved_warning(move, keys.values(), "popup_position", position, save_and_move, f"popup_pending_{index}")



def render_labeling() -> None:
    if notice := st.session_state.pop("popup_notice", None):
        st.toast(notice)  # left by the pop-up when it closed itself
    data = _load_training()
    if data.empty:
        st.info("No training data yet. Run a historical pull or Pull new data in the Jobs tab.")
        return
    sources = data.apply(labeling.source_of, axis=1)
    label = data["intent_label"].fillna("").astype(str).str.strip()
    columns = st.columns(5)
    columns[0].metric("Rows", len(data))
    columns[1].metric("POD_REQUEST", int(label.eq("POD_REQUEST").sum()))
    columns[2].metric("OTHER", int(label.eq("OTHER").sum()))
    columns[3].metric("Unlabeled", int((label.eq("") & sources.ne(SYSTEM_DECISION)).sum()))
    columns[4].metric("System decision to review", int(sources.eq(SYSTEM_DECISION).sum()))

    with st.expander("Filters", expanded=True):
        first, second, third = st.columns(3)
        search = first.text_input("Search subject, body, sender", key="f_search")
        sender = first.text_input("Sender contains", key="f_sender")
        sort = first.selectbox("Sort", labeling.SORTS, key="f_sort")
        label_filter = second.selectbox("Label", labeling.LABEL_FILTERS, key="f_label")
        source_filter = second.selectbox("Labeled by", ["All", MANUAL, MANUAL_REVIEW, SYSTEM_DECISION, "(none)"], key="f_source")
        pseudo_filter = second.selectbox(
            "Pseudo-label", labeling.PSEUDO_FILTERS, key="f_pseudo",
            help="A suggested intent, connote or reference from the Intent or Number pseudo-labels job.",
        )
        dates = third.date_input("Received between", value=(), key="f_dates")
        labeled_dates = third.date_input(
            "Labeled between", value=(), key="f_labeled",
            help="When the label was saved here. Labels made before this was kept have no date.",
        )
        confidence = third.slider("Agent confidence", 0.0, 1.0, (0.0, 1.0), 0.05, key="f_confidence")
    filters = labeling.Filters(
        search=search.strip(),
        sender=sender.strip(),
        label=label_filter,
        source=source_filter,
        start=pd.Timestamp(dates[0]) if len(dates) >= 1 else None,
        end=pd.Timestamp(dates[1]) if len(dates) == 2 else None,
        min_confidence=confidence[0],
        max_confidence=confidence[1],
        pseudo=pseudo_filter,
        labeled_start=pd.Timestamp(labeled_dates[0]) if len(labeled_dates) >= 1 else None,
        labeled_end=pd.Timestamp(labeled_dates[1]) if len(labeled_dates) == 2 else None,
    )
    view = labeling.sort_rows(labeling.filter_rows(data, filters), sort)
    if st.session_state.get(OPEN_POPUP) == "label":
        _label_popup(data, filters)
    if view.empty:
        st.info("Nothing matches these filters.")
        return

    table = view[["received_time", "sender_email", "subject", *labeling.EDITABLE_COLUMNS, "intent_label_pseudo", "pseudo_confidence"]].copy()
    table.insert(0, "select", False)
    table["source"] = sources[view.index]
    table["labeled"] = labeling.labeled_local(view)
    for column in ("intent_label", "connotes_manual", "reference_number_manual", "intent_label_pseudo"):
        table[column] = table[column].fillna("").astype(str)
    table["pseudo_confidence"] = pd.to_numeric(table["pseudo_confidence"], errors="coerce")
    st.caption(f"{len(view)} of {len(data)} rows. Edit labels in the table, or select rows for the actions below.")
    edited = st.data_editor(
        table,
        hide_index=True,
        width="stretch",
        key="label_editor",
        column_config={
            "select": st.column_config.CheckboxColumn("Select"),
            "received_time": st.column_config.TextColumn("Received", disabled=True),
            "sender_email": st.column_config.TextColumn("Sender", disabled=True),
            "subject": st.column_config.TextColumn("Subject", disabled=True),
            "intent_label": st.column_config.SelectboxColumn("Intent", options=["", *classifier.INTENT_CLASSES]),
            "connotes_manual": st.column_config.TextColumn("Connotes (; separated)"),
            "reference_number_manual": st.column_config.TextColumn("References (; separated)"),
            "intent_label_pseudo": st.column_config.TextColumn("Agent / model says", disabled=True),
            "pseudo_confidence": st.column_config.NumberColumn("Confidence", format="%.2f", disabled=True),
            "source": st.column_config.TextColumn("Labeled by", disabled=True),
            "labeled": st.column_config.DatetimeColumn("Labeled at", format="D MMM YYYY, HH:mm", disabled=True),
        },
    )
    selected = list(edited.index[edited["select"]])

    save, bulk_label, bulk_apply, confirm = st.columns([1, 1, 1, 1.4])
    if save.button("Save edits", type="primary"):
        changed = labeling.apply_edits(data, edited)
        if changed:
            _save_training(data)
            # The editor remembers edits by row position; after a save the rows
            # can move (filters, label-date sort), so start it afresh.
            st.session_state.pop("label_editor", None)
            st.success(f"Saved {changed} rows (marked Manual).")
            st.rerun()
        else:
            st.info("No edits to save.")
    intent = bulk_label.selectbox("Intent for selected", classifier.INTENT_CLASSES, key="bulk_intent", label_visibility="collapsed")
    if bulk_apply.button(f"Label {len(selected)} selected", disabled=not selected):
        labeling.set_intent(data, selected, intent)
        _save_training(data)
        st.session_state.pop("label_editor", None)
        st.success(f"Labeled {len(selected)} rows {intent}.")
        st.rerun()
    if confirm.button(f"Confirm agent decision for {len(selected)} selected", disabled=not selected):
        confirmed = labeling.confirm_system_decisions(data, selected)
        _save_training(data)
        st.session_state.pop("label_editor", None)
        st.success(f"Confirmed {confirmed} System decision rows.")
        st.rerun()

    # The pop-up saves rows the table may also hold edits for; saving one
    # would overwrite the other, so table edits are saved first.
    pending = labeling.has_label_edits(st.session_state.get("label_editor"))
    start = selected[0] if selected else view.index[0]
    if st.button(
        "Label one by one" + (" from the first selected row" if selected else ""),
        icon=":material/open_in_new:",
        disabled=pending,
        help="Save or undo the edits in the table first." if pending else None,
    ):
        # The list is fixed when the pop-up opens; Save drops an email that
        # no longer matches the filters and moves on (labeling.after_save).
        st.session_state["popup_rows"] = list(view.index)
        st.session_state["popup_position"] = list(view.index).index(start)
        open_popup("label")

    st.subheader("Email")
    options = list(view.index)
    shown = st.selectbox(
        "Show email",
        options,
        format_func=lambda index: f"{view.at[index, 'subject']} | {view.at[index, 'sender_email']}",
        key="detail_row",
    )
    row = view.loc[shown]
    st.caption(
        f"Received {row['received_time']} from {row['sender_email']}. "
        f"Labeled by: {labeling.source_of(row) or 'nobody yet'}. "
        f"Numbers found by any column are highlighted."
    )
    st.markdown(
        f"**{labeling.highlight(str(row['subject'] or ''), labeling.numbers_in(row))}**<br><br>"
        + labeling.highlight(str(row["body"] or ""), labeling.numbers_in(row)),
        unsafe_allow_html=True,
    )

