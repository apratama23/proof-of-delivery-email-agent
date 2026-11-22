"""Jobs tab: start, monitor and cancel dashboard jobs (D-012)."""

from __future__ import annotations

import logging
import subprocess
import threading

import streamlit as st

from app import jobs

logger = logging.getLogger(__name__)


class _JobController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._status = "idle"
        self._title = ""
        self._code: int | None = None
        self._lines: list[str] = []
        self._progress = 0.0

    def start(self, job: jobs.Job) -> bool:
        with self._lock:
            if self._status in {"running", "cancelling"}:
                return False
            self._cancel = threading.Event()
            self._process = None
            self._status = "running"
            self._title = job.title
            self._code = None
            self._lines = []
            self._progress = 0.0
            self._thread = threading.Thread(target=self._run, args=(job, self._cancel), daemon=True)
            self._thread.start()
            return True

    def cancel(self) -> None:
        with self._lock:
            if self._status != "running":
                return
            self._status = "cancelling"
            self._cancel.set()
            process = self._process
        if process and process.poll() is None:
            process.terminate()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self._status,
                "title": self._title,
                "code": self._code,
                "lines": list(self._lines),
                "progress": self._progress,
            }

    def _append(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)
            self._lines = self._lines[-200:]
            fraction = jobs.progress_of(line)
            if fraction is not None:
                self._progress = fraction

    def _set_process(self, process: subprocess.Popen | None) -> None:
        with self._lock:
            self._process = process
            cancel_requested = self._cancel.is_set()
        if process and cancel_requested and process.poll() is None:
            process.terminate()

    def _run(self, job: jobs.Job, cancel_event: threading.Event) -> None:
        try:
            code = jobs.run(job, self._append, cancel_event, self._set_process)
        except Exception as error:
            logger.exception("Dashboard job %s failed", job.title)
            self._append(f"Job failed: {error}")
            with self._lock:
                self._status = "failed"
                self._code = None
            return
        with self._lock:
            self._code = code
            if code == jobs.CANCELLED:
                self._status = "cancelled"
            elif code == 0:
                self._status = "finished"
            else:
                self._status = "failed"


@st.cache_resource
def _job_controller() -> _JobController:
    return _JobController()


@st.fragment(run_every="1s")
def _render_job_status(controller: _JobController) -> None:
    state = controller.snapshot()
    if state["status"] == "idle":
        return
    title = state["title"]
    if state["status"] in {"running", "cancelling"}:
        st.progress(state["progress"], text=f"{title}: {state['status']}")
        if state["status"] == "running" and st.button("Cancel running job", type="secondary", key="cancel_dashboard_job"):
            controller.cancel()
        elif state["status"] == "cancelling":
            st.info(f"Cancelling {title}…")
    elif state["status"] == "finished":
        st.success(f"{title} finished.")
    elif state["status"] == "cancelled":
        st.warning(f"{title} cancelled. Changes already saved by the job are kept.")
    else:
        st.error(f"{title} failed" + (f" with exit code {state['code']}." if state["code"] is not None else "."))
    if state["lines"]:
        st.code("\n".join(state["lines"]))


def render_jobs() -> None:
    controller = _job_controller()
    active = controller.snapshot()["status"] in {"running", "cancelling"}
    st.caption("One job at a time. Progress appears below; cancel a running job if you started it by mistake.")
    for key, job in jobs.JOBS.items():
        left, right = st.columns([1, 3])
        if left.button(job.title, key=f"job_{key}", width="stretch", disabled=active):
            if not controller.start(job):
                st.warning("Another job is running. Wait for it to finish.")
            st.rerun()
        right.caption(job.explanation)
    _render_job_status(controller)
