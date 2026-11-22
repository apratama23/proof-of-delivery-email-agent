"""Reliable small helpers for Excel files shared by the agent."""

import time
from pathlib import Path

import pandas as pd


_RETRY_COUNT = 3
_RETRY_DELAY_SECONDS = 0.5


def read_table(path: str | Path) -> pd.DataFrame:
    """Read an Excel file, retrying briefly when it is temporarily locked."""
    source = Path(path)
    for attempt in range(_RETRY_COUNT):
        try:
            if not source.exists():
                return pd.DataFrame()
            return pd.read_excel(source)
        except (PermissionError, OSError):
            if attempt == _RETRY_COUNT - 1:
                raise
            time.sleep(_RETRY_DELAY_SECONDS)


def append_row(path: str | Path, row_dict: dict) -> None:
    """Append one row to an Excel file, creating it when necessary."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(_RETRY_COUNT):
        try:
            existing = read_table(destination)
            updated = pd.concat([existing, pd.DataFrame([row_dict])], ignore_index=True)
            updated.to_excel(destination, index=False)
            return
        except (PermissionError, OSError):
            if attempt == _RETRY_COUNT - 1:
                raise
            time.sleep(_RETRY_DELAY_SECONDS)


def init_file_if_missing(path: str | Path, columns: list[str]) -> None:
    """Create an empty Excel file with the requested header when absent."""
    destination = Path(path)
    if destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(columns=columns).to_excel(destination, index=False)