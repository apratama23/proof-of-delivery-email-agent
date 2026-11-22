"""Pull historical Outlook emails into the initial training workbook."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Any
import re

import pandas as pd

from . import config, entity_extractor, outlook_reader


TRAINING_COLUMNS = [
    "entry_id",
    "received_time",
    "sender_email",
    "subject",
    "body",
    "to_recipients",
    "outlook_folder",
    "outlook_categories",
    "intent_label",
    "connotes_manual",
    "reference_number_manual",
    "intent_label_auto",
    "connote_number_auto",
    "ref_number_auto",
    "intent_label_pseudo",
    "pseudo_confidence",
    "pseudo_verified",
    "connotes_pseudo",
    "connotes_pseudo_confidence",
    "reference_number_pseudo",
    "reference_pseudo_confidence",
    # Who set the intent label: "System decision" (pulled from the agent,
    # not yet reviewed) or "Manual" (D-013), and when it was pulled.
    "label_source",
    "system_pulled_at",
    # When a person last set the labels (labeling.LABELED_AT).
    "labeled_at",
]
EXCLUDED_FOLDER_NAMES = {"drafts", "deleted items", "sent items", "junk email"}
OL_MAIL_ITEM = 0  # Outlook's OlItemType for a folder of emails
MASTER_CANDIDATE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z0-9][A-Za-z0-9-]{4,19}(?![A-Za-z0-9])"
)


def _load_master_number_lookup(path: str | Path | None) -> dict[str, str]:
    if path is None:
        return {}  # POD_MASTER_NUMBER_DATA not set (D-060)
    source = Path(path)
    if not source.exists():
        return {}
    if source.suffix.lower() in {".xlsx", ".xls"}:
        master = pd.read_excel(source, dtype="string").fillna("")
    else:
        master = pd.read_csv(source, dtype="string").fillna("")
    required_columns = {"DoNumber", "Reference"}
    missing = required_columns.difference(master.columns)
    if missing:
        raise ValueError(
            f"Master number file is missing columns: {', '.join(sorted(missing))}"
        )

    lookup: dict[str, str] = {}
    for _, row in master.iterrows():
        connote = str(row["DoNumber"]).strip().upper()
        if connote:
            lookup[connote] = "CONNOTE"
        for reference in str(row["Reference"]).split(","):
            reference = reference.strip().upper()
            if reference and reference not in lookup:
                lookup[reference] = "REFERENCE"
    return lookup


def _find_master_matches(
    subject: str, body: str, lookup: dict[str, str]
) -> tuple[str, str]:
    text = f"{subject}\n{body}"
    matches: dict[str, list[str]] = {"CONNOTE": [], "REFERENCE": []}
    seen: set[tuple[str, str]] = set()
    for match in MASTER_CANDIDATE_PATTERN.finditer(text):
        candidate = match.group(0).upper()
        label = lookup.get(candidate)
        if label and (label, candidate) not in seen:
            seen.add((label, candidate))
            matches[label].append(candidate)

    return ";".join(matches["CONNOTE"]), ";".join(matches["REFERENCE"])


# A person's labels: two rows of one email that disagree on these are never merged silently.
_PERSON_LABELS = ("intent_label", "connotes_manual", "reference_number_manual")


def _filled(value) -> bool:
    return value is not None and not pd.isna(value) and str(value).strip() != ""


def _one_row_per_email(existing: pd.DataFrame) -> pd.DataFrame:
    """The workbook with one row per non-empty `entry_id` (fix plan).

    Rows of one email are merged, each column taking its first filled value,
    so no label is lost. Rows that disagree on a person's label stop the pull
    before anything is written, naming the email and the column.
    """
    repeated = existing["entry_id"].ne("") & existing["entry_id"].duplicated(keep=False)
    if not repeated.any():
        return existing
    rows = existing[repeated]
    for entry_id, group in rows.groupby("entry_id", sort=False):
        for column in (column for column in _PERSON_LABELS if column in group.columns):
            values = {str(value).strip() for value in group[column] if _filled(value)}
            if len(values) > 1:
                raise ValueError(
                    f"training data has {len(group)} rows for email {entry_id} with different {column} "
                    f"({', '.join(sorted(values))}); keep one row in {Path(config.TRAINING_DATA_PATH).name}, "
                    "then pull again. Nothing was written."
                )
    first_filled = lambda column: next((value for value in column if _filled(value)), column.iloc[0])
    merged = rows.groupby("entry_id", sort=False, as_index=False).agg(first_filled)
    return pd.concat([existing[~repeated], merged], ignore_index=True)[existing.columns]


def _merge_training_records(
    records: list[dict], existing: pd.DataFrame
) -> pd.DataFrame:
    current = pd.DataFrame(records, columns=TRAINING_COLUMNS).drop_duplicates("entry_id")
    if existing.empty or "entry_id" not in existing.columns:
        return current

    existing = existing.copy()
    existing["entry_id"] = existing["entry_id"].fillna("").astype(str)
    existing = _one_row_per_email(existing)
    manual_columns = (
        "intent_label",
        "connotes_manual",
        "reference_number_manual",
        "intent_label_pseudo",
        "pseudo_confidence",
        "pseudo_verified",
        "connotes_pseudo",
        "connotes_pseudo_confidence",
        "reference_number_pseudo",
        "reference_pseudo_confidence",
        "label_source",
        "system_pulled_at",
        "labeled_at",
    )
    existing_by_id = existing.set_index("entry_id", drop=False)
    for column in manual_columns:
        if column not in current.columns:
            current[column] = ""
        current[column] = current[column].astype(object)
        if column in existing_by_id.columns:
            current_values = current["entry_id"].map(existing_by_id[column])
            has_existing_value = current_values.notna() & (
                current_values.astype(str).str.strip() != ""
            )
            current.loc[has_existing_value, column] = current_values[has_existing_value]

    new_ids = set(current["entry_id"])
    historical_only = existing[~existing["entry_id"].isin(new_ids)].copy()
    for column in TRAINING_COLUMNS:
        if column not in historical_only.columns:
            historical_only[column] = ""
    historical_only = historical_only[TRAINING_COLUMNS]
    merged = pd.concat([current, historical_only], ignore_index=True)[TRAINING_COLUMNS]
    ids = merged["entry_id"].fillna("").astype(str)
    if (ids.ne("") & ids.duplicated()).any():
        # The one rule every writer relies on; stop here rather than save a broken workbook.
        raise ValueError(f"training data would hold an email twice: {ids[ids.ne('') & ids.duplicated()].iloc[0]}")
    return merged


def _iter_folders(folder):
    folder_name = str(getattr(folder, "Name", "") or "").strip().casefold()
    if folder_name in EXCLUDED_FOLDER_NAMES:
        return
    yield folder
    for index in range(1, folder.Folders.Count + 1):
        yield from _iter_folders(folder.Folders.Item(index))


def _normalize_received_time(value) -> datetime | None:
    """Convert Outlook date values to a comparable naive datetime."""
    if value is None:
        return None
    try:
        normalized = pd.Timestamp(value).to_pydatetime()
    except (TypeError, ValueError):
        return None
    if normalized.tzinfo is not None:
        normalized = normalized.astimezone(timezone.utc).replace(tzinfo=None)
    return normalized


def _read_item_value(item, attribute: str, default: Any = "") -> Any:
    """Read one Outlook property without aborting the whole export."""
    try:
        return getattr(item, attribute, default)
    except Exception:
        return default


def _is_actioned_finished_folder(folder) -> bool:
    """Return whether the folder path contains Actioned%2FFinished."""
    folder_path = str(
        getattr(folder, "FolderPath", getattr(folder, "Name", "")) or ""
    )
    return "actioned%2ffinished" in folder_path.casefold()


def _contains_exact_pod_recipient(to_recipients: str) -> bool:
    """Match POD as a standalone recipient name, but not AU-POD."""
    return re.search(r"(?<![A-Za-z0-9-])POD(?![A-Za-z0-9-])", to_recipients, re.IGNORECASE) is not None


def _collect_item(
    item, folder, cutoff: datetime, master_lookup: dict[str, str]
) -> dict | None:
    received_time = _normalize_received_time(
        _read_item_value(item, "ReceivedTime", None)
    )
    if received_time is None or received_time < cutoff:
        return None

    categories = str(_read_item_value(item, "Categories") or "")
    subject = str(_read_item_value(item, "Subject") or "")
    body = entity_extractor.extract_latest_email_body(
        str(_read_item_value(item, "Body") or "")
    )
    to_recipients = str(_read_item_value(item, "To") or "")
    delivered_to = str(_read_item_value(item, "DeliveredTo") or "")
    # The SMTP address, as the live agent reads it; internal senders would
    # otherwise be stored as Exchange paths (/O=...).
    try:
        sender_email = outlook_reader.smtp_address(item)
    except Exception:
        sender_email = str(_read_item_value(item, "SenderEmailAddress") or "")
    recipient_text = f"{to_recipients};{delivered_to}".casefold()
    sender_is_exception = sender_email.casefold() in config.POD_SKIPPED_SENDER_EMAILS
    recipient_is_pod_request = (
        config.POD_SHARED_MAILBOX_ADDRESS.casefold() in recipient_text
    )
    is_exact_pod_recipient = _contains_exact_pod_recipient(to_recipients)
    is_check_recipient = (
        is_exact_pod_recipient and not _is_actioned_finished_folder(folder)
    )
    if sender_is_exception:
        auto_label = "OTHER"
    elif recipient_is_pod_request:
        auto_label = "POD_REQUEST"
    elif is_check_recipient:
        auto_label = "CHECK"
    else:
        auto_label = "OTHER"
    if master_lookup:
        connote_number_auto, ref_number_auto = _find_master_matches(
            subject, body, master_lookup
        )
        if not connote_number_auto and not ref_number_auto:
            connote_number_auto = ";".join(
                entity_extractor.extract_email_connotes(subject, body)
            )
            ref_number_auto = ""
    else:
        connote_number_auto = ";".join(
            entity_extractor.extract_email_connotes(subject, body)
        )
        ref_number_auto = ""
    if not connote_number_auto and not ref_number_auto:
        return None
    return {
        "entry_id": str(_read_item_value(item, "EntryID") or ""),
        "received_time": received_time,
        "sender_email": sender_email,
        "subject": subject,
        "body": body,
        "to_recipients": to_recipients,
        "outlook_folder": str(
            getattr(folder, "FolderPath", getattr(folder, "Name", ""))
        ),
        "outlook_categories": categories,
        "intent_label": "",
        "connotes_manual": "",
        "reference_number_manual": "",
        "intent_label_auto": auto_label,
        "connote_number_auto": connote_number_auto,
        "ref_number_auto": ref_number_auto,
        "intent_label_pseudo": "",
        "pseudo_confidence": "",
        "pseudo_verified": False,
        "connotes_pseudo": "",
        "connotes_pseudo_confidence": "",
        "reference_number_pseudo": "",
        "reference_pseudo_confidence": "",
    }


def pull(
    output_path: str | Path = config.TRAINING_DATA_PATH,
    show_progress: bool = True,
    max_emails: int = config.HISTORICAL_EMAIL_LIMIT,
) -> int:
    """Export the newest historical Outlook messages and return the row count."""
    config.shared_mailbox_address()
    namespace = outlook_reader.connect_outlook()
    mailbox = outlook_reader.get_folder(namespace, config.OUTLOOK_MAILBOX_NAME)
    master_lookup = _load_master_number_lookup(config.MASTER_NUMBER_DATA_PATH)
    if show_progress and not master_lookup:
        print(
            f"Master number file not set, not found or empty: {config.MASTER_NUMBER_DATA_PATH or 'POD_MASTER_NUMBER_DATA'}. "
            "Using the existing connote extractor."
        )
    cutoff = datetime.now() - timedelta(days=365)
    folders = list(_iter_folders(mailbox))
    dated_items = []
    for position, folder in enumerate(folders, start=1):
        name = str(getattr(folder, "Name", "") or "unknown")
        if getattr(folder, "DefaultItemType", OL_MAIL_ITEM) != OL_MAIL_ITEM:
            continue  # a calendar, contacts or tasks: no emails (D-059)
        if show_progress:
            # Before the folder is read, so a slow one shows where the pull is.
            print(f"Scanning folder {position}/{len(folders)}: {name}...", flush=True)
        try:
            # Outlook's table reads these two columns only, without opening each
            # email, which was slow over a year of mail (D-059).
            table = folder.GetTable()
            table.Sort("[ReceivedTime]", True)
            table.Columns.RemoveAll()
            table.Columns.Add("EntryID")
            table.Columns.Add("ReceivedTime")
        except Exception as error:
            if show_progress:
                print(f"  skipped, its emails cannot be listed ({error})", flush=True)
            continue
        found = 0
        while not table.EndOfTable:
            entry_id, received = table.GetNextRow().GetValues()
            received_time = _normalize_received_time(received)
            if received_time is None:
                continue
            if received_time < cutoff:
                break  # newest first: the rest are older than a year
            # By its ID, not its place: places shift when an email arrives or is moved (D-058).
            # The table gives Outlook's short ID, good for fetching it in this run; the
            # workbook keeps the email's own long `EntryID`, as tracking does (`_collect_item`).
            dated_items.append((received_time, str(entry_id or ""), folder))
            found += 1
        if show_progress:
            print(f"  {found} email(s) from the last year", flush=True)

    selected_items = sorted(dated_items, key=lambda value: value[0], reverse=True)
    total_items = len(selected_items)
    processed_items = 0
    records = []
    collected: set[str] = set()
    store_id = str(mailbox.Store.StoreID)
    for _, entry_id, folder in selected_items:
        if len(records) >= max(0, max_emails):
            break
        if not entry_id or entry_id in collected:
            processed_items += 1  # seen twice while the folder changed: taken once
            continue
        collected.add(entry_id)
        item = None
        try:
            try:
                item = namespace.GetItemFromID(entry_id, store_id)
            except Exception:
                if show_progress:
                    sys.stdout.write(
                        f"\rExporting newest emails: {processed_items}/{total_items} "
                        f"({processed_items / total_items * 100 if total_items else 100:5.1f}%) "
                        f"| collected: {len(records)} | skipped unavailable item"
                    )
                    sys.stdout.flush()
                processed_items += 1
                continue
            record = _collect_item(item, folder, cutoff, master_lookup)
            if record and record["entry_id"]:
                records.append(record)
        finally:
            item = None
        processed_items += 1
        if show_progress:
            percentage = processed_items / total_items * 100 if total_items else 100
            sys.stdout.write(
                f"\rExporting newest emails: {processed_items}/{total_items} "
                f"({percentage:5.1f}%) | collected: {len(records)}"
            )
            sys.stdout.flush()

    if show_progress:
        sys.stdout.write("\n")

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    existing = pd.read_excel(destination) if destination.exists() else pd.DataFrame()
    merged = _merge_training_records(records, existing)
    merged.to_excel(destination, index=False)
    return len(records)


def historical_pull_update(
    output_path: str | Path = config.TRAINING_DATA_PATH,
    show_progress: bool = True,
    max_emails: int = config.HISTORICAL_EMAIL_LIMIT,
) -> int:
    """Refresh historical email fields while preserving manual training fields."""
    return pull(
        output_path=output_path,
        show_progress=show_progress,
        max_emails=max_emails,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update",
        action="store_true",
        help="Refresh automatic email fields and preserve manual training fields",
    )
    args = parser.parse_args()
    config.training_data_path()  # stops naming POD_TRAINING_DATA_FILE when it is not set (D-060)
    action = historical_pull_update if args.update else pull
    print(f"Pulled {action()} historical emails")