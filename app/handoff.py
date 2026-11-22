"""Messages to and from Power Automate, one JSON file each (D-009).

Folders under `config.HANDOFF_DIR`:

| Folder | Written by | Read by | Content |
|---|---|---|---|
| pbi_requests/ | Python | Flow 1 | {request_id, entry_id, numbers, scan_missing_numbers, query} |
| pbi_results/ | Flow 1 | Python | {request_id, rows} or {request_id, error} |
| outstanding/ | Python | Flow 2 | {request_id, action, number, connote, reference, row} |
| teams/ | Python | Flow 3 | {request_id, kind, message, options, card (choice only)} |
| teams_results/ | Flow 3 | Python | {request_id, choice, responder, connotes, references} or {request_id, error} |
| archive/ | Python, flows | people | consumed files, kept as evidence |

Files are written under `_tmp/` first and then moved in, so a flow never
reads a half-written file. Flows carry no business logic (D-001): Python
builds the DAX query, the Outstanding row and the Teams text.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from urllib.parse import quote

from . import config
from .pod_files import clean

FOLDERS = ("pbi_requests", "pbi_results", "outstanding", "teams", "teams_results", "archive")
# A file that cannot be parsed this soon after it changed is still being written or synced (D-047).
PARTIAL_FILE_SECONDS = 300


def _folder(name: str) -> Path:
    path = Path(config.HANDOFF_DIR) / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def request_id(entry_id: str) -> str:
    """A short, file-name-safe id for an email (Outlook entry IDs are long)."""
    return hashlib.sha1(entry_id.encode("utf-8")).hexdigest()[:16]


def _write(folder: str, name: str, data: dict) -> Path:
    temporary = _folder("_tmp") / name
    temporary.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8", newline="\n")
    destination = _folder(folder) / name
    os.replace(temporary, destination)
    return destination


def _read(folder: str, name: str) -> dict | None:
    path = Path(config.HANDOFF_DIR) / folder / name
    if not path.exists():
        return None
    # utf-8-sig: files saved by Power Automate may start with a byte-order mark.
    text = path.read_text(encoding="utf-8-sig")
    try:
        try:
            return json.loads(text)
        except ValueError:
            # A "\" typed on a Teams card reaches the file as it is, which JSON refuses (D-049).
            return json.loads(re.sub(r'\\(?![\\"/bfnrtu])', r"\\\\", text))
    except ValueError:
        if time.time() - path.stat().st_mtime < PARTIAL_FILE_SECONDS:
            return None  # read again next run
        raise


def teams_request_pending(rid: str) -> bool:
    """Whether a Teams request file is still waiting for Flow 3, which moves it away when it takes it."""
    return (Path(config.HANDOFF_DIR) / "teams" / f"{rid}.json").exists()


def archive(name: str, folders: tuple[str, ...] = ("pbi_requests", "pbi_results", "teams_results")) -> None:
    """Move every handoff file called `name` in `folders` into archive/ (evidence is kept).

    An email processed again reuses its request ID, so an earlier archived
    file of the same name gets a number instead of being overwritten.
    """
    for folder in folders:
        path = Path(config.HANDOFF_DIR) / folder / name
        if path.exists():
            destination = _folder("archive") / f"{folder}__{name}"
            copy = 1
            while destination.exists():
                destination = destination.with_name(f"{folder}__{Path(name).stem}__{copy}{Path(name).suffix}")
                copy += 1
            shutil.move(str(path), str(destination))


# --- Power BI (Flow 1) -------------------------------------------------------


def _dax_string(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _dax_column(table: str, column: str) -> str:
    return "'" + table.replace("'", "''") + "'[" + column.replace("]", "]]") + "]"


def build_pbi_query(numbers: list[str]) -> str:
    """DAX returning dataset rows whose connote or reference is one of `numbers`.

    Each number is sent as written and cleaned (D-003), since the dataset may
    store "AB10234" for a customer's "AB-10234". Columns are compared as text
    (`& ""`) in case the dataset stores numbers. The reference column may hold
    several references, so it is searched with CONTAINSSTRING and the exact
    match is checked in Python (`match_rows`).
    """
    table = config.required(config.PBI_TABLE_SETTING)
    connote_name = config.required(config.PBI_CONNOTE_COLUMN_SETTING)
    reference_name = config.required(config.PBI_REFERENCE_COLUMN_SETTING)
    connote, reference = _dax_column(table, connote_name), _dax_column(table, reference_name)
    # As written, cleaned, and without leading zeros (0086055559 and 86055559 are
    # the same number, as on the share); match_rows picks the row. References are
    # searched as text, so the zero-less form also finds a zero-padded one.
    variants = sorted({
        value
        for number in numbers
        for value in (number.strip().upper(), clean(number), _without_zeros(clean(number)))
        if value
    })
    # A connote is compared whole, so it also needs the padded forms.
    # shortcut: up to two added zeros, widen if TMS pads connotes further.
    connote_variants = sorted(set(variants) | {
        zeros + _without_zeros(value) for value in variants if value.isdigit() for zeros in ("0", "00")
    })
    in_list = "{" + ", ".join(_dax_string(value) for value in connote_variants) + "}"

    def matches_in(table: str, connote: str, reference: str | None) -> str:
        tests = [f"{connote} & \"\" IN {in_list}"]
        if reference:
            tests.extend(
                # A padded reference contains the number without zeros, so no padded forms.
                f"CONTAINSSTRING({reference} & \"\", {_dax_string(value)})"
                for value in variants
            )
        quoted_table = "'" + table.replace("'", "''") + "'"
        return (
            f"FILTER(\n        {quoted_table},\n"
            f"        {' || '.join(tests)}\n    )"
        )

    columns = config.pbi_columns()
    # PickUp rows come back too: match_rows prefers the other row of a connote (D-027).
    matches = matches_in(table, connote, reference)
    second = config.pbi_second_table()
    if not columns:
        return f"EVALUATE\n{matches}"

    # One row per matching row of the main table: its connote and reference,
    # then each requested column. A column of another table is looked up
    # through its link (POD_PBI_LINKS); MAXX picks one value if several rows
    # match, and the comparison is as text in case one side stores numbers.
    # This does not depend on how relationships are set up in the model (D-027).
    links = config.pbi_links()
    output = [(connote_name, connote), (reference_name, reference)]
    for name, column_table, column in columns:
        if column_table == table:
            output.append((name, _dax_column(table, column)))
        else:
            other_key, main_key = links[column_table]
            quoted_other = "'" + column_table.replace("'", "''") + "'"
            output.append((
                name,
                f"MAXX(FILTER({quoted_other}, {_dax_column(column_table, other_key)} & \"\" = "
                f"{_dax_column(table, main_key)} & \"\"), {_dax_column(column_table, column)})",
            ))
    selected = ",\n    ".join(f"{_dax_string(name)}, {expression}" for name, expression in output)
    query = f"SELECTCOLUMNS(\n    {matches},\n    {selected}\n)"
    if second is None:
        return f"EVALUATE\n{query}"

    # The second table's matches, with the same columns in the same order, are
    # added under the main table's rows; UNION keeps the main table's column
    # names, so match_rows and the Outstanding row read both alike.
    second_table, second_connote, second_reference, second_columns = second
    second_output = [
        (connote_name, _dax_column(second_table, second_connote)),
        (reference_name, _dax_column(second_table, second_reference) if second_reference else "BLANK()"),
    ] + [
        (name, _dax_column(second_table, second_columns[name]) if name in second_columns else "BLANK()")
        for name, _, _ in columns
    ]
    second_matches = matches_in(
        second_table,
        second_output[0][1],
        _dax_column(second_table, second_reference) if second_reference else None,
    )
    second_selected = ",\n    ".join(f"{_dax_string(name)}, {expression}" for name, expression in second_output)
    return f"EVALUATE\nUNION(\n{query},\nSELECTCOLUMNS(\n    {second_matches},\n    {second_selected}\n)\n)"


def write_pbi_request(
    rid: str, entry_id: str, numbers: list[str], scan_missing_numbers: list[str]
) -> Path:
    if not scan_missing_numbers:
        raise ValueError("A Power BI request needs at least one number missing from the scan folders")
    return _write(
        "pbi_requests",
        f"{rid}.json",
        {
            "request_id": rid,
            "entry_id": entry_id,
            "numbers": numbers,
            "scan_missing_numbers": scan_missing_numbers,
            "query": build_pbi_query(scan_missing_numbers),
        },
    )


def _column_name(key: str) -> str:
    # Power BI returns keys like "Table[Column]"; keep the column name.
    match = re.search(r"\[([^\]]*)\]$", key)
    return match.group(1) if match else key


def read_pbi_result(rid: str) -> dict | None:
    """None while waiting; else {"rows": [...]} with plain column names, or {"error": ...}."""
    data = _read("pbi_results", f"{rid}.json")
    if data is None:
        return None
    if isinstance(data, list):
        data = {"rows": data}
    if data.get("error"):
        return {"error": str(data["error"])}
    rows = [{_column_name(key): value for key, value in row.items()} for row in data.get("rows") or []]
    return {"rows": rows}


def same_number(first: str, second: str) -> bool:
    """Whether two numbers are one, written with or without "-", spaces or leading zeros."""
    return bool(clean(first)) and _without_zeros(clean(first)) == _without_zeros(clean(second))


def matching_rows(numbers: list[str], rows: list[dict]) -> dict[str, list[dict]]:
    """Matching dataset rows for each number: by connote (DoNumber) if any, else by reference (product owner, D-044).

    Within each, a row with the number exactly as written wins over one that
    only matches without leading zeros (D-003 note).
    """
    connote_column = config.required(config.PBI_CONNOTE_COLUMN_SETTING)
    reference_column = config.required(config.PBI_REFERENCE_COLUMN_SETTING)
    connote_of = lambda row: {clean(str(row.get(connote_column) or ""))} - {""}
    references_of = lambda row: {clean(value) for value in re.split(r"[,;]", str(row.get(reference_column) or ""))} - {""}

    def holding(wanted: str, values_of) -> list[dict]:
        exact = [row for row in rows if wanted in values_of(row)]
        return exact or [row for row in rows if _without_zeros(wanted) in {_without_zeros(value) for value in values_of(row)}]

    matched: dict[str, list[dict]] = {}
    for number in numbers:
        wanted = clean(number)
        matched[number] = holding(wanted, connote_of) or holding(wanted, references_of)
    return matched


def preferred_row(rows: list[dict]) -> dict | None:
    """The row to use among matches: not PickUp, unless it is the only kind (product owner, D-027)."""
    status_column = os.getenv(config.PBI_STATUS_COLUMN_SETTING, "").strip()
    is_pickup = lambda row: bool(status_column) and str(row.get(status_column) or "").strip().casefold() == "pickup"
    return next((row for row in rows if not is_pickup(row)), rows[0] if rows else None)


def match_rows(numbers: list[str], rows: list[dict]) -> dict[str, dict | None]:
    """The preferred dataset row for each number, or None if there is no match.

    TMS can hold two rows for one connote, one with POD status PickUp; the
    other row counts, and a PickUp row only when it is the only one (product owner, D-027).
    """
    return {number: preferred_row(candidates) for number, candidates in matching_rows(numbers, rows).items()}


def _without_zeros(value: str) -> str:
    """A cleaned number without its leading zeros; a number of only zeros stays as it is."""
    return value.lstrip("0") or value


# --- POD Outstanding (Flow 2) ------------------------------------------------


NOT_IN_SYSTEM = "Not in The System"


def outstanding_row(number: str, pbi_row: dict | None, email: dict, requested_at: str) -> dict:
    """The Outstanding row for a number, laid out by the column map.

    `pbi_row` None means the number is not in Power BI (D-023): the row gets
    the requested number as its connote and the status "Not in The System",
    for the team to check.
    """
    in_system = pbi_row is not None
    pbi_row = pbi_row or {}
    status_column = os.getenv(config.PBI_STATUS_COLUMN_SETTING, "").strip()
    values = {
        "number": number,
        "connote": pbi_row.get(os.getenv(config.PBI_CONNOTE_COLUMN_SETTING, "").strip(), "") if in_system else number,
        "reference": pbi_row.get(os.getenv(config.PBI_REFERENCE_COLUMN_SETTING, "").strip(), ""),
        "pod_status": (pbi_row.get(status_column, "") if status_column else "") if in_system else NOT_IN_SYSTEM,
        "sender_email": email.get("sender_email", ""),
        "subject": email.get("subject", ""),
        "received_time": email.get("received_time", ""),
        "requested_at": requested_at,
    }
    row = {}
    for column, source in config.outstanding_columns().items():
        if source.startswith("pbi:"):
            row[column] = pbi_row.get(source[4:], "")
        elif source in values:
            row[column] = values[source]
        else:
            raise config.ConfigError(
                f"{config.OUTSTANDING_COLUMNS_SETTING}: unknown source '{source}' for column '{column}'"
            )
    return row


def write_outstanding(rid: str, action: str, number: str, connote: str = "", reference: str = "", row: dict | None = None) -> Path:
    """Ask Flow 2 to add (only if absent) or remove (if present) a number."""
    if action not in {"add", "remove"}:
        raise ValueError(f"Outstanding action must be add or remove, not {action!r}")
    return _write(
        "outstanding",
        f"{rid}_{action}_{clean(number)}.json",
        {"request_id": rid, "action": action, "number": number, "connote": connote, "reference": reference, "row": row or {}},
    )


def outstanding_adds_archived(rid: str, numbers: list[str]) -> bool:
    """Whether every requested add operation has left outstanding for archive."""
    if not numbers:
        return False
    pending_folder = Path(config.HANDOFF_DIR) / "outstanding"
    archive_folder = Path(config.HANDOFF_DIR) / "archive"
    for number in numbers:
        filename = f"{rid}_add_{clean(number)}.json"
        if (pending_folder / filename).exists():
            return False
        archived = False
        for path in archive_folder.glob(f"{Path(filename).stem}*{Path(filename).suffix}"):
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue  # still syncing from SharePoint or unreadable: not confirmed yet
            if (
                str(data.get("request_id") or "") == rid
                and data.get("action") == "add"
                and clean(str(data.get("number") or "")) == clean(number)
            ):
                archived = True
                break
        if not archived:
            return False
    return True


# --- Teams (Flow 3) ----------------------------------------------------------


def choice_card(message: str, options: list[dict]) -> dict:
    """The Teams adaptive card for a choice: the message, one option list, Submit.

    Built here so Flow 3 only passes it on. The answer comes back in the
    card's `choice` field as the chosen option id.
    """
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {"type": "TextBlock", "text": message, "wrap": True},
            {
                "type": "Input.ChoiceSet",
                "id": "choice",
                "style": "expanded",
                "isRequired": True,
                "errorMessage": "Choose one option.",
                "choices": [{"title": option["label"], "value": option["id"]} for option in options],
            },
        ],
        "actions": [{"type": "Action.Submit", "title": "Submit"}],
    }


def correction_card(message: str, numbers: str) -> dict:
    """The Teams adaptive card for a held email: its numbers to correct, Save or Handle manually (D-036).

    The answer comes back as `choice` ("correct" or "manual") and the
    corrected numbers in `connotes`.
    """
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {"type": "TextBlock", "text": message, "wrap": True},
            {"type": "Input.Text", "id": "connotes", "label": "Connote and reference numbers (; separated)", "value": numbers},
        ],
        "actions": [
            {"type": "Action.Submit", "title": "Save numbers", "data": {"choice": "correct"}},
            {"type": "Action.Submit", "title": "Handle manually", "data": {"choice": "manual"}, "associatedInputs": "none"},
        ],
    }


def review_card(message: str, connotes: str) -> dict:
    """The Teams adaptive card for a Manual Review email, as the dashboard's review (D-048).

    The connote and reference fields, then POD request, Not a POD request or
    Handle manually. The answer comes back as `choice` ("pod", "other" or
    "manual") with the numbers in `connotes` and `references`.
    """
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {"type": "TextBlock", "text": message, "wrap": True},
            {"type": "Input.Text", "id": "connotes", "label": "Connotes (; separated)", "value": connotes},
            {"type": "Input.Text", "id": "references", "label": "References (; separated)", "value": ""},
        ],
        "actions": [
            {"type": "Action.Submit", "title": "POD request", "data": {"choice": "pod"}},
            {"type": "Action.Submit", "title": "Not a POD request", "data": {"choice": "other"}, "associatedInputs": "none"},
            {"type": "Action.Submit", "title": "Handle manually", "data": {"choice": "manual"}, "associatedInputs": "none"},
        ],
    }


def pod_mailbox_url() -> str:
    """The POD mailbox in Outlook on the web, where a card's button goes (D-051, D-061)."""
    return f"https://outlook.office.com/mail/{quote(config.shared_mailbox_address(), safe='@')}/"


def write_teams(rid: str, kind: str, message: str, options: list[dict] | None = None, card: dict | None = None) -> Path:
    """Post to the team chat: `info` is a plain message, `choice` a card to answer (`card`, or one option list).

    A card also gets an "Open the POD mailbox" button (D-051, D-061).
    """
    if kind not in {"info", "choice"}:
        raise ValueError(f"Teams message kind must be info or choice, not {kind!r}")
    data = {"request_id": rid, "kind": kind, "message": message, "options": options or []}
    if kind == "choice":
        card = dict(card or choice_card(message, options or []))
        card["actions"] = [*card.get("actions", []),
                           {"type": "Action.OpenUrl", "title": "Open the POD mailbox", "url": pod_mailbox_url()}]
        data["card"] = card
    return _write("teams", f"{rid}.json", data)


def read_teams_result(rid: str) -> dict | None:
    """None while waiting; else {"choice": option id, "responder": name, "connotes", "references": numbers typed, "error": ...}.

    `error` is set, and the rest empty, when Flow 3 could not post the card (D-037).
    `references` is None when Flow 3 does not pass that field yet (D-048).
    """
    data = _read("teams_results", f"{rid}.json")
    if data is None:
        return None
    return {
        "choice": str(data.get("choice") or ""),
        "responder": str(data.get("responder") or ""),
        "connotes": str(data.get("connotes") or ""),
        "references": str(data.get("references") or "") if "references" in data else None,
        "error": str(data.get("error") or ""),
    }
