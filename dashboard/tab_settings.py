"""Settings tab: threshold, automatic replies, finished emails, emergency stop."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from app import config, model_registry
from dashboard.common import _render_pilot


def render_settings() -> None:
    st.subheader("Dashboard auto-refresh")
    refresh_enabled = config.current_setting("POD_DASHBOARD_AUTO_REFRESH", "on")
    refresh_enabled_toggle = st.toggle(
        "Automatically refresh dashboard",
        value=refresh_enabled == "on",
        key="settings_dashboard_auto_refresh",
    )
    if refresh_enabled_toggle != (refresh_enabled == "on"):
        config.save_setting("POD_DASHBOARD_AUTO_REFRESH", "on" if refresh_enabled_toggle else "off")
        st.session_state["dashboard_refresh_notice"] = (
            "Dashboard auto-refresh enabled." if refresh_enabled_toggle else "Dashboard auto-refresh disabled."
        )
        st.rerun()

    refresh_interval_raw = config.current_setting("POD_DASHBOARD_REFRESH_INTERVAL_SECONDS", "30")
    try:
        refresh_interval = int(refresh_interval_raw)
        if not 10 <= refresh_interval <= 3600:
            raise ValueError
    except ValueError:
        refresh_interval = 30
    if notice := st.session_state.pop("dashboard_refresh_notice", None):
        st.success(notice)
    interval = st.number_input(
        "Refresh interval (seconds)",
        min_value=10,
        max_value=3600,
        step=10,
        value=refresh_interval,
        key="settings_dashboard_refresh_interval",
        help="Choose a value between 10 seconds and 1 hour.",
    )
    if st.button("Save refresh interval", key="save_dashboard_refresh_interval"):
        config.save_setting("POD_DASHBOARD_REFRESH_INTERVAL_SECONDS", str(interval))
        st.session_state["dashboard_refresh_notice"] = f"Refresh interval set to {interval} seconds."
        st.rerun()

    st.subheader("Intent confidence threshold")
    current = float(config.current_setting("POD_CONFIDENCE_THRESHOLD", config.DEFAULT_CONFIDENCE_THRESHOLD))
    threshold = st.slider("Act only at or above", 0.50, 0.99, current, 0.01, key="settings_threshold")
    active = model_registry.get_active("intent")
    runs = model_registry.list_history("intent")
    if active and runs:
        record = model_registry.load_history(runs[-1])
        if active in record["models"]:
            result = model_registry.at_threshold(record, active, threshold)
            precision = "n/a" if result["precision"] is None else f"{result['precision']:.1%}"
            target = config.precision_target()
            meets = "" if result["precision"] is None else (
                f", {'meets' if result['precision'] >= target else 'below'} the {target:.0%} precision target")
            st.caption(
                f"With {active} on the latest test set: precision {precision}{meets}, "
                f"coverage {result['coverage']:.1%} ({result['acted']} of {record['n_test']} test emails acted on)."
            )
    if threshold != current and st.button("Save threshold", type="primary"):
        config.save_setting("POD_CONFIDENCE_THRESHOLD", f"{threshold:.2f}")
        st.success("Saved. It applies from the next run.")
        st.rerun()

    st.subheader("Targets")
    st.caption("Your own aims, used to judge the figures; changing them changes nothing the agent does (D-054).")
    left, right = st.columns(2)
    for column, name, label, help_text, current_value in (
        (left, "POD_PILOT_TARGET", "Pilot: PODs unchanged target (%)",
         "Switch to sending once this share of drafts goes out with the PODs the agent attached (Overview, D-014).",
         config.pilot_target()),
        (right, "POD_PRECISION_TARGET", "Intent precision target (%)",
         "The share of the emails the agent acts on that must be POD requests; choose the threshold above by it.",
         config.precision_target()),
    ):
        percent = column.number_input(label, 50, 100, round(current_value * 100), 1, key=f"target_{name}", help=help_text)
        if percent != round(current_value * 100) and column.button("Save", key=f"save_{name}", type="primary"):
            config.save_setting(name, f"{percent / 100:.2f}")
            st.success("Saved.")
            st.rerun()

    st.subheader("Automatic replies")
    mode = config.current_setting("POD_SEND_MODE", config.SEND_MODE)
    saved_domains = config.current_setting(config.SEND_DOMAINS_SETTING, "")
    st.caption(
        "Sent replies go to customers without review; drafts wait in the POD mailbox's Drafts. Partial sends "
        "only replies to senders whose address ends in one of the domains below, exactly as after the @ (D-057)."
    )
    if notice := st.session_state.pop("send_mode_notice", None):
        st.success(notice)
    # A widget's value can only be reset before it is drawn, so Cancel sets this flag.
    if st.session_state.pop("reset_automatic_replies", False):
        st.session_state["settings_send_mode"] = mode
        st.session_state["settings_send_domains"] = saved_domains
    labels = {"draft": "Off: every reply is a draft", "partial": "Partial: sent to the domains below, drafts for the rest",
              "send": "Full: every reply is sent"}
    chosen = st.radio("Replies", list(labels), index=list(labels).index(mode), format_func=labels.get,
                      key="settings_send_mode")
    typed = st.text_input("Domains for partial sending (after the @, separated by ;)", saved_domains,
                          key="settings_send_domains", placeholder="example3.com; example4.com")
    if not config.EDITABLE_SETTINGS[config.SEND_DOMAINS_SETTING](typed):
        st.error("Write each domain as it is after the @, such as example3.com, separated by ;")
    else:
        before, after = config.split_domains(saved_domains), config.split_domains(typed)
        added = [domain for domain in after if domain not in before]
        order = list(labels)
        # More emails sent without review: confirm first, as one click by mistake must not do it.
        wider = order.index(chosen) > order.index(mode) or (chosen == "partial" and added)
        if not wider and (chosen != mode or after != before):
            # Back towards drafts, or a domain taken out, is the safe direction: no confirmation.
            config.save_setting(config.SEND_DOMAINS_SETTING, "; ".join(after))
            config.save_setting("POD_SEND_MODE", chosen)
            st.session_state["send_mode_notice"] = f"Saved: {labels[chosen]}, from the next run."
            st.rerun()
        if wider:
            who = ("**every reply**" if chosen == "send"
                   else f"**replies to {', '.join(added or after) or 'no domain yet'}**")
            st.warning(f"From the next run, {who} will be **sent to customers directly, without review**. "
                       "Are you sure?", icon=":material/warning:")
            confirm, cancel, _ = st.columns([1, 1, 4])
            if confirm.button("Yes, send automatically", type="primary", width="stretch"):
                config.save_setting(config.SEND_DOMAINS_SETTING, "; ".join(after))
                config.save_setting("POD_SEND_MODE", chosen)
                st.session_state["send_mode_notice"] = f"Saved: {labels[chosen]}, from the next run."
                st.rerun()
            if cancel.button("Cancel", key="cancel_automatic_replies", width="stretch"):
                st.session_state["reset_automatic_replies"] = True
                st.rerun()

    if mode == "draft":
        st.info(
            "Testing mode: replies are saved as Outlook drafts, and the automated-email "
            "notice is omitted."
        )
        st.caption("Enable automatic replies only when you are ready for emails to be sent without review (D-014).")
        _render_pilot()
    elif mode == "partial":
        st.warning(
            f"Partial sending is active: replies to {', '.join(config.split_domains(saved_domains)) or 'no domain'} "
            "are sent directly with the automated-email notice; the others are drafts, measured below."
        )
        _render_pilot()
    else:
        st.warning(
            "Automatic sending is active: qualifying replies will be sent directly "
            "and include the automated-email notice."
        )

    st.subheader("Finished emails")
    if notice := st.session_state.pop("move_finished_notice", None):
        st.success(notice)
    moving = config.current_setting("POD_MOVE_FINISHED", "off") == "on"
    since = config.current_setting("POD_MOVE_FINISHED_SINCE", "")
    move = st.toggle(
        f"Move finished emails to {config.FINISHED_FOLDER}",
        value=moving,
        key="settings_move_finished",
        help="Replied emails whose every requested POD was attached; a draft once it is sent. "
        "An email with a number not received stays in the mailbox.",
    )
    if move != moving:
        if move:
            # Only replies sent from now on move (product owner): earlier ones stay where they are.
            config.save_setting("POD_MOVE_FINISHED_SINCE", datetime.now(timezone.utc).isoformat())
        config.save_setting("POD_MOVE_FINISHED", "on" if move else "off")
        st.session_state["move_finished_notice"] = (
            f"From the next run, finished emails sent from now on move to {config.FINISHED_FOLDER}."
            if move else "Finished emails are no longer moved."
        )
        st.rerun()
    if moving and since:
        st.caption(
            f"On since {datetime.fromisoformat(since).astimezone(config.LOCAL_TIME):%d %b %Y %H:%M} local time (UTC+8): emails whose reply went out "
            f"after that move to {config.FINISHED_FOLDER}, directly under the mailbox folder in POD_OUTLOOK_MAILBOX (POD_FINISHED_FOLDER)."
        )

    st.subheader("Emergency stop")
    stop = Path(config.STOP_FILE)
    if stop.exists():
        st.error("STOP is on: the agent reads, drafts and sends nothing.")
        if st.button("Remove STOP"):
            stop.unlink()
            st.rerun()
    else:
        st.write("STOP is off.")
        if st.button("Stop the agent"):
            stop.write_text("Created from the dashboard.\n", encoding="utf-8")
            st.rerun()

    st.subheader("Other settings (change in .env)")
    st.dataframe(
        pd.DataFrame(
            [
                {"setting": name, "value": config.current_setting(name, "(default)")}
                for name in (
                    "POD_OUTLOOK_MAILBOX", "POD_SCAN_FOLDERS", "POD_SCAN_EXCLUDED_FOLDERS",
                    "POD_HANDOFF_DIR", "POD_MAX_ATTACHMENT_MB",
                    "POD_PBI_TABLE", "POD_OUTSTANDING_COLUMNS", "POD_MIN_TRAINING_SAMPLES_PER_CLASS",
                )
            ]
        ),
        hide_index=True,
        width="stretch",
    )
