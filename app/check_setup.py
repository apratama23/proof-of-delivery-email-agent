"""Read-only check of the newer settings and the handoff folders: `python -m app.check_setup`.

Run it before testing. It prints which settings are set,
whether they are allowed, and whether Power Automate takes its files. It
reads only: no email, file or setting is changed, and Outlook is not used.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from . import config
from .main import CARD_RESEND_AFTER

# Files waiting longer than this for a flow mean the flow does not take them.
STALE_MINUTES = CARD_RESEND_AFTER.total_seconds() / 60


def _line(ok: bool | None, text: str) -> None:
    print(f"  [{'OK' if ok else '--' if ok is None else '!!'}] {text}")


def _scan_path() -> None:
    print("Power BI scan path and customer checks (D-044)")
    names = {name for name, _, _ in config.pbi_columns()}

    def column(setting: str, needed: bool) -> None:
        value = os.getenv(setting, "").strip()
        if not value:
            _line(False if needed else None, f"{setting} is empty" + ("" if needed else ": off"))
        else:
            _line(value in names, f"{setting}={value}" + ("" if value in names else " is not a name in POD_PBI_COLUMNS"))

    column(config.PBI_SCAN_PATH_COLUMN_SETTING, needed=False)
    customers = os.getenv(config.PBI_SENDER_CHECK_SETTING, "").strip()
    if not customers:
        _line(None, f"{config.PBI_SENDER_CHECK_SETTING} empty: no customer's rows are checked by sender")
        return
    config.sender_check_word("")  # a malformed setting raises here, named
    _line(True, f"{config.PBI_SENDER_CHECK_SETTING}={customers}")
    column(config.PBI_SENDER_COLUMN_SETTING, needed=True)


def _manual_upload() -> None:
    print("Files to attach by hand (POD_MANUAL_UPLOAD_DIR)")
    try:
        folder = config.manual_upload_dir()
    except config.ConfigError as error:
        _line(False, str(error))
        return
    if folder is None:
        _line(None, "off: a reply too large to attach is a draft without its files, listed in Teams without a folder (D-050)")
        return
    _line(folder.is_dir(), f"{folder}" + ("" if folder.is_dir() else " does not exist"))
    if folder.is_dir():
        _line(os.access(folder, os.W_OK), "the agent can write there" if os.access(folder, os.W_OK) else "not writable")
    url = os.getenv(config.MANUAL_UPLOAD_URL_SETTING, "").strip()
    if not url:
        _line(None, "POD_MANUAL_UPLOAD_URL empty: Teams shows the folder path, not a link")
    else:
        direct = url.startswith("https://") and "AllItems.aspx" not in url and "?id=" not in url
        _line(direct, url if direct else f"{url}: use the folder's direct path, https://<tenant>.sharepoint.com/sites/<site>/Shared Documents/<folder>")
    _line(None, f"attachment limit POD_MAX_ATTACHMENT_MB={config.MAX_ATTACHMENT_MB:g}")


def _handoff() -> None:
    print(f"Handoff folders ({config.HANDOFF_DIR})")
    now = time.time()
    for folder, flow in (("pbi_requests", "Flow 1"), ("outstanding", "Flow 2"), ("teams", "Flow 3")):
        path = Path(config.HANDOFF_DIR) / folder
        files = list(path.glob("*.json")) if path.is_dir() else []
        stale = [file for file in files if (now - file.stat().st_mtime) / 60 > STALE_MINUTES]
        text = f"{folder}: {len(files)} waiting"
        if stale:
            oldest = max((now - file.stat().st_mtime) / 60 for file in stale)
            text += f", {len(stale)} for over {STALE_MINUTES:.0f} minutes (oldest {oldest:.0f}): {flow} does not take them"
        # Flow 1 leaves requests for Python to archive, so only Flows 2 and 3 should empty theirs.
        _line(None if folder == "pbi_requests" else not stale, text)


def _git() -> None:
    print("Git")
    try:
        email = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True,
                               cwd=config.PROJECT_DIR, check=False).stdout.strip()
    except OSError as error:
        _line(None, f"git not available ({error})")
        return
    _line(None, f"commits are made as {email or '(no user.email set)'}")


def main() -> None:
    for check in (_scan_path, _manual_upload, _handoff, _git):
        try:
            check()
        except Exception as error:  # a check must report, not stop
            _line(False, f"could not check: {type(error).__name__}: {error}")
        print()


if __name__ == "__main__":
    main()
