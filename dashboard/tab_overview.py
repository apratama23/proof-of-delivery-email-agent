"""Overview tab: headline figures and charts (D-012)."""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from app import dashboard_data as dd
from dashboard.common import _bars, _chart, _color, _emails, _load_training, _render_pilot, _style


def render_overview() -> None:
    table = _emails()
    now = dd.local_now()
    for label, since in (("Today", now.normalize()), ("Last 7 days", (now - pd.Timedelta(days=6)).normalize())):
        st.caption(label)
        columns = st.columns(6)
        for column, (name, value) in zip(columns, dd.kpis(table, since).items()):
            column.metric(name, value)

    _render_pilot()

    st.subheader("Emails per day, last 30 days")
    st.caption("By the day the agent first saw each email; the colour is its outcome now.")
    volume = dd.daily_volume(table, now=now)
    if volume["emails"].sum() == 0:
        st.info("No emails processed in the last 30 days.")
    else:
        chart = alt.Chart(volume).mark_bar(stroke=_style()["surface"], strokeWidth=2).encode(
            # One band per calendar day, so each day is a readable bar.
            x=alt.X("yearmonthdate(day):T", title=None, axis=alt.Axis(format="%d %b")),
            y=alt.Y("emails:Q", title="Emails", axis=alt.Axis(format="d", tickMinStep=1)),
            color=_color("outcome", dd.OUTCOMES),
            order=alt.Order("outcome_order:Q"),
            tooltip=[alt.Tooltip("day:T", format="%d %b %Y"), "outcome:N", "emails:Q"],
        ).transform_calculate(
            outcome_order=f"indexof({list(dd.OUTCOMES)}, datum.outcome)"
        )
        _chart(chart, volume[volume["emails"] > 0], "emails per day")

    numbers = dd.number_outcomes(table)
    left, right = st.columns(2)
    with left:
        st.subheader("What happened to each requested number")
        if numbers.empty:
            st.info("No POD requests yet.")
        else:
            counts = numbers["outcome"].value_counts().reindex(dd.NUMBER_OUTCOMES, fill_value=0)
            data = pd.DataFrame({"outcome": counts.index, "numbers": counts.values})
            _chart(_bars(data, "outcome", "numbers", "Outcome", "Numbers"), data, "requested numbers")
    with right:
        st.subheader("Share of numbers found on the share, per week")
        rates = dd.weekly_hit_rate(numbers)
        if rates.empty:
            st.info("No decided numbers yet.")
        else:
            style = _style()
            chart = alt.Chart(rates).mark_line(color=style["series"][0], strokeWidth=2, point=alt.OverlayMarkDef(size=64, filled=True)).encode(
                x=alt.X("week:T", title="Week of", axis=alt.Axis(format="%d %b")),
                y=alt.Y("hit_rate:Q", title="Found on share", axis=alt.Axis(format="%"), scale=alt.Scale(domain=[0, 1])),
                tooltip=[alt.Tooltip("week:T", format="%d %b %Y"), alt.Tooltip("hit_rate:Q", format=".0%"), "numbers:Q"],
            )
            _chart(chart, rates, "weekly hit rate")

    left, right = st.columns(2)
    with left:
        st.subheader("Agent decisions you left unchanged")
        agreement = dd.system_agreement(_load_training())
        if agreement.empty:
            st.info("No reviewed System decision rows yet (Labeling tab).")
        else:
            style = _style()
            chart = alt.Chart(agreement).mark_line(color=style["series"][0], strokeWidth=2, point=alt.OverlayMarkDef(size=64, filled=True)).encode(
                x=alt.X("pulled:T", title="Pulled on", axis=alt.Axis(format="%d %b")),
                y=alt.Y("agreement:Q", title="Unchanged", axis=alt.Axis(format="%"), scale=alt.Scale(domain=[0, 1])),
                tooltip=[alt.Tooltip("pulled:T", format="%d %b %Y"), alt.Tooltip("agreement:Q", format=".0%"), "reviewed:Q"],
            )
            _chart(chart, agreement, "system-decision agreement")
    with right:
        st.subheader("Domains asking most")
        domains = dd.top_domains(table)
        if domains.empty:
            st.info("No POD requests yet.")
        else:
            _chart(_bars(domains, "domain", "requests", "Sender domain", "POD requests", by_size=True), domains, "top domains")

    st.subheader("How long POD emails have been waiting")
    waiting = dd.queue(table, now=now, other_intent=False)
    ages = dd.queue_age(waiting)
    if waiting.empty:
        st.info("Nothing is waiting.")
    else:
        _chart(_bars(ages, "age", "emails", "Waiting for", "Emails"), ages, "queue age")

