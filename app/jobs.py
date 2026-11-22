"""Commands the dashboard can start, and reading their progress (D-012)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event

from . import config

CANCELLED = -1


@dataclass(frozen=True)
class Job:
    title: str
    explanation: str
    commands: tuple[tuple[str, ...], ...]


JOBS = {
    "run": Job(
        "Run once now",
        "Process unread POD emails now, as the scheduler does every 5 minutes.",
        (("-m", "app.main"),),
    ),
    "pull": Job(
        "Pull new data",
        "Add emails the agent processed (as System decision) and new mailbox emails to the training data.",
        (("-m", "app.training_refresh"), ("-m", "app.historical_pull", "--update")),
    ),
    "historical": Job(
        "Historical pull",
        "Read the last year of the mailbox into the training data, keeping every label.",
        (("-m", "app.historical_pull"),),
    ),
    "train_intent": Job(
        "Train intent models",
        "Train and compare every intent model; choose one in the Models tab.",
        (("-m", "app.classifier"),),
    ),
    "train_number": Job(
        "Train number model",
        "Train the CRF number model and compare it with regex, email by email; choose one in the Models tab.",
        (("-m", "app.train_number_classifier"),),
    ),
    "pseudo_intent": Job(
        "Intent pseudo-labels",
        "Let the active intent model suggest labels for unlabeled rows.",
        (("-m", "app.classifier", "--pseudo-label"),),
    ),
    "pseudo_number": Job(
        "Number pseudo-labels",
        "Let the active CRF suggest connotes and references for rows without a manual intent.",
        (("-m", "app.train_number_classifier", "--pseudo-label"),),
    ),
}

_PROGRESS = re.compile(r"(\d+)\s*/\s*(\d+)")


def progress_of(line: str) -> float | None:
    """The fraction done if a line reports "n/total", else None."""
    match = _PROGRESS.search(line)
    if not match or int(match.group(2)) == 0:
        return None
    return min(1.0, int(match.group(1)) / int(match.group(2)))


def run(
    job: Job,
    on_line: Callable[[str], None],
    cancel_event: Event | None = None,
    on_process: Callable[[subprocess.Popen | None], None] | None = None,
) -> int:
    """Run the job's commands in order with this Python; stop at the first failure.

    Every output line is passed to `on_line`. Returns the exit code, or CANCELLED.
    """
    environment = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    for arguments in job.commands:
        if cancel_event and cancel_event.is_set():
            return CANCELLED
        on_line(f"$ python {' '.join(arguments)}")
        with subprocess.Popen(
            [sys.executable, *arguments],
            cwd=config.PROJECT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        ) as process:
            if on_process:
                on_process(process)
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    # historical_pull redraws one line with "\r"; show each state.
                    for part in line.rstrip("\n").split("\r"):
                        if part.strip():
                            on_line(part)
                code = process.wait()
            finally:
                if on_process:
                    on_process(None)
        if cancel_event and cancel_event.is_set():
            return CANCELLED
        if code != 0:
            on_line(f"Stopped: exit code {code}")
            return code
    return 0
