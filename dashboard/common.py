"""Shared by the dashboard tabs: chart style, data loading, the pilot figures, pop-up helpers."""

from __future__ import annotations

from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from app import config, dashboard_data as dd, labeling, outlook_reader, pilot, tracking
from app.excel_io import read_table


_LIGHT = {
    "surface": "#ffffff",
    "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"],
    "sequential": ["#cde2fb", "#104281"],
}



_DARK = {
    "surface": "#0e1117",
    "series": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"],
    "sequential": ["#184f95", "#cde2fb"],
}



def _style() -> dict:
    theme = getattr(getattr(st.context, "theme", None), "type", None)
    return _DARK if theme == "dark" else _LIGHT



def _chart(chart: alt.Chart, data: pd.DataFrame, title: str) -> None:
    """Show a chart and, under it, the same numbers as a table."""
    st.altair_chart(
        chart.properties(height=260).configure_axis(grid=True, gridOpacity=0.3, domain=False)
        .configure_legend(orient="top", title=None)
        .configure_view(strokeWidth=0),
        width="stretch",
    )
    with st.expander(f"Data: {title}"):
        st.dataframe(data, hide_index=True, width="stretch")



def _bars(data: pd.DataFrame, x: str, y: str, x_title: str, y_title: str, by_size: bool = False) -> alt.Chart:
    """One series of horizontal bars (labels stay readable): first palette slot,
    rounded data ends, whole-number counts, tooltip. Categories keep their
    given order unless `by_size`."""
    style = _style()
    position = {
        "y": alt.Y(f"{x}:N", title=x_title, sort="-x" if by_size else None),
        "x": alt.X(f"{y}:Q", title=y_title, axis=alt.Axis(format="d", tickMinStep=1)),
    }
    return alt.Chart(data).mark_bar(
        color=style["series"][0], cornerRadiusEnd=4, stroke=style["surface"], strokeWidth=2
    ).encode(**position, tooltip=[alt.Tooltip(f"{x}:N", title=x_title), alt.Tooltip(f"{y}:Q", title=y_title)])



def _color(field: str, domain: tuple[str, ...]) -> alt.Color:
    """Categorical colour that follows the category, never its rank."""
    return alt.Color(f"{field}:N", scale=alt.Scale(domain=list(domain), range=_style()["series"][: len(domain)]))



def _load_training() -> pd.DataFrame:
    data = read_table(config.TRAINING_DATA_PATH)
    for column in (
        "entry_id", "received_time", "sender_email", "subject", "body", "intent_label",
        "connotes_manual", "reference_number_manual", "intent_label_pseudo", "pseudo_confidence",
        "connote_number_auto", "ref_number_auto", "label_source", "system_pulled_at", labeling.LABELED_AT,
    ):
        if column not in data.columns:
            data[column] = ""
        data[column] = data[column].astype(object)
    return data



def _save_training(data: pd.DataFrame) -> None:
    destination = Path(config.TRAINING_DATA_PATH)
    destination.parent.mkdir(parents=True, exist_ok=True)
    data.to_excel(destination, index=False)



def _emails() -> pd.DataFrame:
    return dd.emails(tracking.all_events())



def _render_pilot() -> None:
    """The pilot measure (D-014): share of the agent's drafts sent with the PODs it attached (D-053)."""
    result = pilot.summary(tracking.latest(tracking.REPLIED), config.pilot_target())
    st.caption(f"Pilot: drafts sent with the PODs the agent attached, of those sent or deleted; "
               f"the text may be changed (target {result['target']:.0%}, D-014, D-053)")
    if not result["drafts"]:
        st.info("No drafts made yet.")
        return
    columns = st.columns(5)
    share = result["same_pods_share"]
    if share is None:
        columns[0].metric("PODs unchanged", "-", "no draft sent or deleted yet", delta_color="off")
    else:
        columns[0].metric(
            "PODs unchanged",
            f"{share:.0%}",
            f"{'meets' if share >= result['target'] else 'below'} the {result['target']:.0%} target",
            delta_color="normal" if share >= result["target"] else "inverse",
            help=f"{result[pilot.SENT_SAME_PODS]} sent with the same PODs of {result['decided']} sent or deleted; "
            "drafts still waiting are not counted yet.",
        )
    for column, name in zip(columns[1:], pilot.OUTCOMES):
        column.metric(name.capitalize(), f"{result[name]} of {result['drafts']}")



def _move_buttons(previous, following, position, count, changed, pending_key, position_key="popup_position"):
    """Previous and Next for a pop-up; with unsaved changes they ask first (bug report item 4).

    Returns the direction ("next" or "previous") waiting for the person's
    answer, or None.
    """
    for column, label, icon, step, disabled in (
        (previous, "Previous", ":material/arrow_back:", -1, position == 0),
        (following, "Next", ":material/arrow_forward:", 1, position == count - 1),
    ):
        if column.button(label, icon=icon, disabled=disabled, width="stretch"):
            if changed:
                st.session_state[pending_key] = "next" if step > 0 else "previous"
            else:
                st.session_state.pop(pending_key, None)
                st.session_state[position_key] = position + step
                st.rerun(scope="fragment")
    return st.session_state.get(pending_key)



def _open_in_outlook_button(entry_id: str, key: str) -> None:
    """A pop-up's "Open in Outlook": the email in its own Outlook window on this PC (D-033)."""
    if st.button(
        "Open in Outlook", icon=":material/open_in_new:", key=f"outlook_{key}",
        help="Opens this email in Outlook Desktop on this PC. Outlook marks it as read.",
    ):
        try:
            outlook_reader.open_in_outlook(dd.current_entry_id(entry_id, tracking.history(entry_id)), config.OUTLOOK_MAILBOX_NAME)
        except Exception as error:
            st.error(
                "Could not open this email in Outlook: it may have been moved to another folder or deleted, "
                f"or Outlook is not running. ({type(error).__name__}: {error})"
            )


def _unsaved_warning(direction, field_keys, position_key, position, save_and_move, pending_key) -> None:
    """Ask what to do with unsaved changes before moving on: save them, or drop them."""
    st.warning("You have unsaved changes on this email.", icon=":material/warning:")
    keep, discard, _ = st.columns([1, 1, 2])
    if keep.button("Save and go on", type="primary", width="stretch", key=f"{pending_key}_save"):
        st.session_state.pop(pending_key, None)
        save_and_move(direction)
    if discard.button("Discard and go on", width="stretch", key=f"{pending_key}_discard"):
        st.session_state.pop(pending_key, None)
        for field in field_keys:
            st.session_state.pop(field, None)  # the email shows its saved values next time
        st.session_state[position_key] = position + (1 if direction == "next" else -1)
        st.rerun(scope="fragment")



# Height in pixels of the scrolling email body in the labeling pop-up: lower
# it if the buttons fall below the window, raise it if space is left under them.
POPUP_BODY_HEIGHT = 280


# Which pop-up is open. The auto-refresh is off while one is, or it would
# close it with what is typed in it (dashboard/app.py).
OPEN_POPUP = "open_popup"


def open_popup(name: str) -> None:
    """Open a pop-up through a full run, which switches the auto-refresh off; the tab then shows it."""
    st.session_state[OPEN_POPUP] = name
    st.rerun()


def close_popup() -> None:
    """When a pop-up is dismissed, or closes itself after its last Save: the auto-refresh resumes."""
    st.session_state.pop(OPEN_POPUP, None)
