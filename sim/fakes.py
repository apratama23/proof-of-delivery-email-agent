"""The fake world the simulation runs the real agent in: settings, Outlook and the three flows.

Everything is written under `SIM_ROOT`, outside the project, and recreated on
each run; nothing here touches the real data, `.env`, Outlook or SharePoint.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from app import attachments, classifier, config, handoff, model_registry, outlook_reader

SIM_ROOT = Path(tempfile.gettempdir()) / "pod-email-agent-sim"
AGENT_DIR = SIM_ROOT / "agent"  # stands in for the project folder: tracking, logs, models, .env
SCAN_ROOT = SIM_ROOT / "scans" / "2026"
UPLOAD_DIR = SIM_ROOT / "sharepoint" / "Manual upload"
UPLOAD_URL = "https://example.sharepoint.com/sites/POD/Shared Documents/Manual upload"
STORE_ID = "SIM-POD-STORE"

# The Power BI settings of the simulated dataset, read when the agent needs them.
ENV = {
    "POD_PBI_TABLE": "POD Data",
    "POD_PBI_CONNOTE_COLUMN": "Connote",
    "POD_PBI_REFERENCE_COLUMN": "Reference",
    "POD_PBI_STATUS_COLUMN": "POD Status",
    "POD_PBI_LINKS": "Scans[Key]=POD Data[Key]",
    "POD_PBI_COLUMNS": "Sender=POD Data[Sender];POD Status=POD Data[POD Status];Scan Path=Scans[Path]",
    # A customer whose Power BI rows must name it, with its own scan folder (D-044).
    "POD_PBI_SENDER_CHECK": "example1.com=example1",
    "POD_PBI_SENDER_COLUMN": "Sender",
    "POD_PBI_SCAN_PATH_COLUMN": "Scan Path",
    "POD_OUTSTANDING_COLUMNS": "Connote=connote;Status=pod_status;Requested By=sender_email",
    "POD_MANUAL_UPLOAD_DIR": str(UPLOAD_DIR),
    "POD_MANUAL_UPLOAD_URL": UPLOAD_URL,
    "POD_SENDER_FOLDERS": "example1.com=Example1",
    "POD_SEND_DOMAINS": "sendnow.example",  # partial mode: only S34's sender is sent to (D-057)
    "POD_SIGNATURE_FILE": "",
    **{name: "" for name in ("POD_PBI_SECOND_TABLE", "POD_PBI_SECOND_CONNOTE_COLUMN",
                             "POD_PBI_SECOND_REFERENCE_COLUMN", "POD_PBI_SECOND_COLUMNS")},
}


def apply_settings() -> None:
    """Point the agent at the simulation folders; the real ones are never read or written."""
    os.environ.update(ENV)
    for name, value in {
        "PROJECT_DIR": AGENT_DIR,
        "TRACKING_DB_PATH": AGENT_DIR / "tracking.db",
        "PROCESSED_IDS_PATH": AGENT_DIR / "processed_ids.txt",
        "LOG_PATH": AGENT_DIR / "run_log.txt",
        "STOP_FILE": AGENT_DIR / "STOP",
        "LOCK_FILE": AGENT_DIR / "run.lock",
        "MODELS_DIR": AGENT_DIR / "models",
        "TRAINING_DATA_PATH": AGENT_DIR / "training_data.xlsx",
        "HANDOFF_DIR": SIM_ROOT / "sharepoint" / "handoff",
        "SCAN_FOLDERS": [SCAN_ROOT],
        "SCAN_EXCLUDED_FOLDERS": [],
        "SEND_MODE": "partial",
        "MAX_ATTACHMENT_MB": 1.0,  # small, so a 2 MB dummy scan is "too large"
        "POD_SHARED_MAILBOX_ADDRESS": "pod@example.com",
        "OUTLOOK_MAILBOX_NAME": "POD",
        "CONFIDENCE_THRESHOLD": 0.9,
        "MOVE_FINISHED": False,
    }.items():
        setattr(config, name, value)


class KeywordModel:
    """Stands in for the intent model: a remittance email is OTHER, anything else a POD request; "maybe" makes it unsure."""

    def predict(self, texts):
        return ["OTHER" if "remittance" in text.lower() else "POD_REQUEST" for text in texts]

    def predict_proba(self, texts):
        return [[0.4, 0.6] if "maybe" in text.lower() else [0.03, 0.97] for text in texts]


def use_fake_models() -> None:
    model_registry.load_active = lambda task: KeywordModel() if task == "intent" else None  # numbers: regex
    classifier.load_training_data = lambda path: pd.DataFrame()
    classifier.has_enough_training_data = lambda data, minimum: True


class FakeMailbox:
    """The POD mailbox: unread emails, the drafts the agent makes, and what people do by hand.

    People reply (Outlook's "You replied" mark), and move or delete emails.
    A moved email usually gets a new entry ID in Outlook; `move` says whether
    it keeps its old one.
    """

    def __init__(self):
        self.emails: dict[str, dict] = {}
        self.unread: set[str] = set()
        self.drafts: dict[str, dict] = {}  # entry id -> {"body", "attachments"}
        self.sent: list[dict] = []  # Sent Items: drafts a person sent, as the pilot measure reads them
        self.sent_drafts: set[str] = set()
        self.manual: dict[str, dict] = {}  # entry id -> what Outlook's "You replied" mark says
        self.moved: dict[str, tuple[str | None, bool]] = {}  # entry id -> (folder, None if deleted; entry ID kept)
        # entry id -> [{"name", "text"}]: what OCR or the PDF reader finds; {"name", "rows"}: a real spreadsheet
        self.attached: dict[str, list[dict]] = {}
        self.home = SimpleNamespace(Name="Inbox", FolderPath="\\\\POD\\Inbox", Store=SimpleNamespace(StoreID=STORE_ID))

    def move(self, entry_id: str, folder: str | None, keep_entry_id: bool = False) -> None:
        self.moved[entry_id] = (folder, keep_entry_id)

    def add(self, email: dict) -> None:
        self.emails[email["entry_id"]] = email
        self.unread.add(email["entry_id"])

    def install(self) -> None:
        """Replace every Outlook call the agent makes with this mailbox."""
        outlook_reader.connect_outlook = lambda: SimpleNamespace()
        outlook_reader.get_folder = lambda namespace, name: self.home
        outlook_reader.mailbox_store_id = lambda name: STORE_ID
        outlook_reader.get_unread_emails = lambda folder, processed: [
            {**self.emails[entry_id], "full_body": self.emails[entry_id]["body"]}
            for entry_id in self.emails if entry_id in self.unread and entry_id not in processed
        ]
        outlook_reader.reply = self._reply
        outlook_reader.mark_read = lambda entry_id, store_id="": self.unread.discard(entry_id)
        outlook_reader.replied_manually = self._replied_manually
        outlook_reader.email_status = self._email_status
        outlook_reader.in_child_folder = lambda parent, name, message_id: any(
            folder == name and self.emails[entry_id]["message_id"] == message_id
            for entry_id, (folder, _) in self.moved.items()
        )
        outlook_reader.sent_replies_since = lambda folder, since: list(self.sent)
        outlook_reader.draft_exists = lambda entry_id, store_id="": entry_id not in self.sent_drafts
        outlook_reader.add_categories = lambda entry_id, categories, store_id="": list(categories)
        outlook_reader.move_to_child_folder = lambda entry_id, parent, name: f"{entry_id}-moved"
        outlook_reader.save_attachments = self._save_attachments
        # The text Windows' OCR or the PDF reader would find, by the file's own name.
        text_of = lambda path: self._texts.get(Path(path).name.split("_", 1)[1], "")
        attachments.ocr_image = text_of
        attachments.pdf_text = text_of

    _texts: dict[str, str] = {}

    def _save_attachments(self, entry_id, store_id, folder):
        saved = []
        for position, attachment in enumerate(self.attached.get(entry_id, [])):
            path = Path(folder) / f"{position}_{attachment['name']}"
            if "rows" in attachment:
                from openpyxl import Workbook

                book = Workbook()
                for row in attachment["rows"]:
                    book.active.append(row)
                book.save(path)
            else:
                path.write_bytes(b"0" * 50_000)  # big enough not to count as a signature logo
            self._texts[attachment["name"]] = attachment.get("text", "")
            saved.append({"name": attachment["name"], "path": path, "size": 50_000})
        return saved

    def _email_status(self, entry_id, store_id=""):
        folder, kept = self.moved.get(entry_id, ("", True))
        if folder is None or not kept:
            return None  # deleted, or moved under a new entry ID: Outlook cannot open it by the old one
        here = self.home if not folder else SimpleNamespace(
            Name=folder, FolderPath=f"{self.home.FolderPath}\\{folder}", Parent=self.home)
        return {"folder": here, "manual": self.manual.get(entry_id)}

    def _replied_manually(self, entry_id, store_id=""):
        if self._email_status(entry_id) is None:
            raise RuntimeError("The message could not be found")  # as Outlook does
        return self.manual.get(entry_id)

    def send_draft(self, entry_id: str, added_files: list[str]) -> None:
        """A person sends the agent's draft, with files they attached by hand."""
        from datetime import datetime, timezone

        draft = self.drafts[entry_id]
        self.sent.append({"conversation_id": f"C-{entry_id}", "sent_on": datetime.now(timezone.utc).isoformat(),
                          "body": draft["body"], "attachments": [*draft["attachments"], *added_files]})
        self.sent_drafts.add(f"D-{entry_id}")

    def _reply(self, entry_id, body, attachments, mode, send_as="", store_id=""):
        assert store_id == STORE_ID, "a reply must be made in the POD mailbox"
        self.drafts[entry_id] = {"body": body, "attachments": [Path(path).name for path in attachments], "mode": mode}
        if mode == "send":
            return {"conversation_id": f"C-{entry_id}"}  # sent: no draft to measure
        return {"draft_entry_id": f"D-{entry_id}", "draft_store_id": STORE_ID, "conversation_id": f"C-{entry_id}"}


def _handoff(folder: str) -> Path:
    return Path(config.HANDOFF_DIR) / folder


def _move_to_archive(path: Path, flow: str) -> None:
    archive = _handoff("archive")
    archive.mkdir(parents=True, exist_ok=True)
    target, copy = archive / f"{flow}__{path.name}", 1
    while target.exists():
        target, copy = archive / f"{flow}__{path.stem}__{copy}{path.suffix}", copy + 1
    shutil.move(str(path), str(target))


class FakeFlows:
    """Power Automate Flows 1 to 3, as `docs/POWER_AUTOMATE_FLOW.md` describes them.

    Flow 1 answers a Power BI request from `pbi_rows`, after `pbi_delay`
    rounds for some emails; for emails in `stale_first` its run for the first
    request finishes a round late and finds nothing, as an old query would,
    arriving before the answer to the newer request. Flow 2 keeps POD Outstanding as a dict. Flow 3
    moves a Teams file away as soon as it takes it, "posts" it, and answers a
    card with `answers` (by email); for emails in `flow3_drops` it leaves the
    file, as when Flow 3 does not run.
    """

    def __init__(self, pbi_rows: list[dict], answers: dict, pbi_delay: dict[str, int]):
        self.pbi_rows = pbi_rows
        self.answers = answers  # entry id -> callable(card) -> answer dict
        self.pbi_delay = pbi_delay  # entry id -> rounds before Flow 1 answers
        self.flow3_drops: set[str] = set()
        self.stale_first: set[str] = set()
        self._first_seen: set[str] = set()
        self.outstanding: dict[str, dict] = {}
        self.posted: list[dict] = []  # every Teams message and card, in order
        self._pbi_queue: list[tuple[int, str, list[dict]]] = []
        self._seen_requests: set[tuple[str, float]] = set()
        self.by_request = {}

    def entry_of(self, request_id: str) -> str:
        return self.by_request.get(request_id.split("-")[0], "")

    def run(self, round_number: int) -> None:
        self._flow1(round_number)
        self._flow2()
        self._flow3()

    def _flow1(self, round_number: int) -> None:
        folder = _handoff("pbi_requests")
        for path in sorted(folder.glob("*.json")) if folder.exists() else []:
            key = (path.name, path.stat().st_mtime)
            if key in self._seen_requests:
                continue  # Flow 1 is triggered once per new file
            self._seen_requests.add(key)
            request = json.loads(path.read_text(encoding="utf-8"))
            wanted = {number.lstrip("0") for number in request["scan_missing_numbers"]}
            rows = [
                {f"POD Data[{column}]": value for column, value in row.items()}
                for row in self.pbi_rows
                if {str(row.get("Connote", "")).lstrip("0"), str(row.get("Reference", "")).lstrip("0")} & wanted
            ]
            entry_id = self.entry_of(request["request_id"])
            if entry_id in self.stale_first and entry_id not in self._first_seen:
                self._first_seen.add(entry_id)
                self._pbi_queue.append((round_number + 1, request["request_id"], []))
                continue
            delay = self.pbi_delay.get(entry_id, 0)
            self._pbi_queue.append((round_number + delay, request["request_id"], rows))
        due = [item for item in self._pbi_queue if item[0] <= round_number]
        self._pbi_queue = [item for item in self._pbi_queue if item[0] > round_number]
        for _, request_id, rows in due:
            results = _handoff("pbi_results")
            results.mkdir(parents=True, exist_ok=True)
            (results / f"{request_id}.json").write_text(json.dumps({"rows": rows}), encoding="utf-8")

    def _flow2(self) -> None:
        folder = _handoff("outstanding")
        for path in sorted(folder.glob("*.json")) if folder.exists() else []:
            item = json.loads(path.read_text(encoding="utf-8"))
            if item["action"] == "add":
                self.outstanding.setdefault(item["number"], item["row"])  # add only if absent
            else:
                self.outstanding.pop(item["number"], None)
            _move_to_archive(path, "outstanding")

    def _flow3(self) -> None:
        folder = _handoff("teams")
        for path in sorted(folder.glob("*.json")) if folder.exists() else []:
            message = json.loads(path.read_text(encoding="utf-8"))
            entry_id = self.entry_of(message["request_id"])
            if entry_id in self.flow3_drops:
                continue  # Flow 3 did not run for it: the file stays in teams/
            _move_to_archive(path, "teams")  # first, as the guide now says (D-037)
            self.posted.append({"entry_id": entry_id, **message})
            if message["kind"] == "choice" and entry_id in self.answers:
                answer = {"request_id": message["request_id"], "responder": "Sim Team", "connotes": "", "references": "",
                          **self.answers[entry_id](message)}
                results = _handoff("teams_results")
                results.mkdir(parents=True, exist_ok=True)
                (results / f"{message['request_id']}.json").write_text(json.dumps(answer), encoding="utf-8")


def choose_file(name: str):
    """A card answer that picks the option whose label holds this file name."""
    def answer(card: dict) -> dict:
        option = next(option for option in card["options"] if option["label"].startswith(name))
        return {"choice": option["id"]}
    return answer


def write_pdf(path: Path, size_bytes: int = 200) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4 simulated scan\n" + b"0" * max(0, size_bytes - 24))
    return path


def request_id(entry_id: str) -> str:
    return handoff.request_id(entry_id)
