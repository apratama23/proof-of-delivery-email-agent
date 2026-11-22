"""Local Streamlit dashboard: overview, review queue, history, labeling, models, jobs, settings.

Run with `streamlit run dashboard/app.py`. The figures come from
`app.dashboard_data`, the labeling rules from `app.labeling`; each tab is laid
out in its own `dashboard/tab_*.py`, and what they share in `dashboard/common.py`.
This file is the page: sidebar and tabs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
project_root = str(PROJECT_ROOT)
if project_root in sys.path:
    sys.path.remove(project_root)
sys.path.insert(0, project_root)

from app import config, model_registry
from dashboard.common import OPEN_POPUP
from dashboard.tab_history import render_history
from dashboard.tab_jobs import render_jobs
from dashboard.tab_labeling import render_labeling
from dashboard.tab_models import render_models
from dashboard.tab_overview import render_overview
from dashboard.tab_review import render_review_queue
from dashboard.tab_settings import render_settings

st.set_page_config(page_title="POD Email Agent", page_icon="📦", layout="wide")


# --- Page

st.title("POD Email Agent")
try:
    config.training_data_path()
except config.ConfigError as error:
    st.error(f"{error}: the file name of the training workbook in POD_SHAREPOINT_DATA_DIR (D-060).")
    st.stop()
auto_refresh = config.current_setting("POD_DASHBOARD_AUTO_REFRESH", "on")
if auto_refresh not in {"on", "off"}:
    st.error("POD_DASHBOARD_AUTO_REFRESH must be 'on' or 'off'; automatic refresh is disabled.")
    auto_refresh = "off"
refresh_interval_raw = config.current_setting("POD_DASHBOARD_REFRESH_INTERVAL_SECONDS", "30")
try:
    refresh_interval = int(refresh_interval_raw)
    if not 10 <= refresh_interval <= 3600:
        raise ValueError
except ValueError:
    st.error(
        "POD_DASHBOARD_REFRESH_INTERVAL_SECONDS must be between 10 and 3600; "
        "using 30 seconds until it is corrected in Settings."
    )
    refresh_interval = 30

with st.sidebar:
    if Path(config.STOP_FILE).exists():
        st.error("STOP is on")
    send_mode = config.current_setting("POD_SEND_MODE", config.SEND_MODE)
    domains = config.current_setting(config.SEND_DOMAINS_SETTING, "")
    st.write(f"Send mode: **{send_mode}**" + (f" ({', '.join(config.split_domains(domains)) or 'no domain'})"
                                                if send_mode == "partial" else ""))
    for task in model_registry.TASKS:
        st.write(f"{task.title()} model: **{model_registry.get_active(task) or 'none'}**")
    if auto_refresh == "on":
        st.caption(f"Dashboard tabs refresh automatically every {refresh_interval} seconds.")
    else:
        st.caption("Dashboard auto-refresh is off.")
    if st.button("Refresh"):
        st.rerun()


@st.fragment(run_every=f"{refresh_interval}s" if auto_refresh == "on" and OPEN_POPUP not in st.session_state else None)
def _render_tabs() -> None:
    tabs = st.tabs(["Overview", "Review queue", "History", "Labeling", "Models", "Jobs", "Settings"])
    for tab, render in zip(
        tabs,
        (
            render_overview,
            render_review_queue,
            render_history,
            render_labeling,
            render_models,
            render_jobs,
            render_settings,
        ),
    ):
        with tab:
            render()


_render_tabs()
