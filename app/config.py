"""Central configuration for the POD email agent."""

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
# Times are stored in UTC and shown in local time, UTC+8, whatever the PC's zone (product owner).
# UTC+8 has no daylight saving, so a fixed offset is exact.
LOCAL_TIME = timezone(timedelta(hours=8), "UTC+8")


def _load_local_env(path: Path) -> None:
    """Load simple KEY=VALUE settings without requiring an extra package."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env(PROJECT_DIR / ".env")
SHAREPOINT_DATA_DIR = Path(
    os.getenv("POD_SHAREPOINT_DATA_DIR", PROJECT_DIR / "data")
)
# Trained models, the active choice (active.json) and training history.
MODELS_DIR = PROJECT_DIR / "models"
PROCESSED_IDS_PATH = PROJECT_DIR / "data" / "processed_ids.txt"
LOG_PATH = PROJECT_DIR / "logs" / "run_log.txt"
TRACKING_DB_PATH = PROJECT_DIR / "data" / "tracking.db"
# A file with this name in the project folder stops all drafting and sending.
STOP_FILE = PROJECT_DIR / "STOP"
# Held open by the running cycle so the scheduler and the dashboard never
# run two cycles at once.
LOCK_FILE = PROJECT_DIR / "data" / "run.lock"

# The training workbook: a file name in SHAREPOINT_DATA_DIR (product owner, D-060). Not set,
# the path is that folder, which no run accepts (`training_data_path`).
TRAINING_DATA_FILE_SETTING = "POD_TRAINING_DATA_FILE"
TRAINING_DATA_PATH = SHAREPOINT_DATA_DIR / os.getenv(TRAINING_DATA_FILE_SETTING, "").strip()
# The master number list (.xlsx or .csv), a path; not set: not used (D-060).
_master_number_data = os.getenv("POD_MASTER_NUMBER_DATA", "").strip()
MASTER_NUMBER_DATA_PATH = Path(_master_number_data) if _master_number_data else None


def training_data_path() -> Path:
    """The training workbook's path; fails naming the setting when it is not set (D-060)."""
    if Path(TRAINING_DATA_PATH) == SHAREPOINT_DATA_DIR:
        raise ConfigError(f"Setting {TRAINING_DATA_FILE_SETTING} is missing in .env")
    return Path(TRAINING_DATA_PATH)


# One JSON file per message to and from Power Automate (D-009).
HANDOFF_DIR = Path(os.getenv("POD_HANDOFF_DIR", SHAREPOINT_DATA_DIR / "handoff"))

# Must match the shared mailbox/store name shown at the top level in Outlook.
OUTLOOK_MAILBOX_NAME = os.getenv("POD_OUTLOOK_MAILBOX", "POD")
# No default: the guard and the reply sender depend on it (shared_mailbox_address).
POD_SHARED_MAILBOX_ADDRESS = os.getenv("POD_SHARED_MAILBOX_ADDRESS", "").strip()
# Senders whose messages are treated as POD requests only in Actioned/Finished.
POD_SKIPPED_SENDER_EMAILS = {
    email.strip().casefold()
    for email in os.getenv("POD_SKIPPED_SENDER_EMAILS", "").split(",")
    if email.strip()
}
# Intent confidence the agent acts on; 0.95 meets the 95% precision target
# where 0.90 did not (D-025). The product owner can change it in the dashboard.
DEFAULT_CONFIDENCE_THRESHOLD = "0.95"
CONFIDENCE_THRESHOLD = float(os.getenv("POD_CONFIDENCE_THRESHOLD", DEFAULT_CONFIDENCE_THRESHOLD))
# A token counts as a number when the CRF is at least this sure (D-026).
NUMBER_CONFIDENCE_THRESHOLD = float(
    os.getenv("POD_NUMBER_CONFIDENCE_THRESHOLD", "0.5")
)
MIN_TRAINING_SAMPLES_PER_CLASS = int(
    os.getenv("POD_MIN_TRAINING_SAMPLES_PER_CLASS", "150")
)
HISTORICAL_EMAIL_LIMIT = int(os.getenv("POD_HISTORICAL_EMAIL_LIMIT", "2000"))


class ConfigError(RuntimeError):
    """A setting needed for this step is missing or not allowed."""


def required(name: str) -> str:
    """Return a setting from the environment, or fail naming it."""
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Setting {name} is missing in .env")
    _refuse_placeholder(name, value)
    return value


def _refuse_placeholder(setting: str, value: str) -> None:
    # .env.example marks names to fill in as <...>.
    if "<" in value and ">" in value:
        raise ConfigError(f"{setting}: '{value}' is still a <...> placeholder from .env.example")


def _split_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(";") if part.strip()]


def shared_mailbox_address() -> str:
    """The shared mailbox's own address; fails naming the setting if unset.

    Checked before mail is read: without it the guard would not recognise
    the agent's own replies, and replies would have no sender.
    """
    if not POD_SHARED_MAILBOX_ADDRESS:
        raise ConfigError("Setting POD_SHARED_MAILBOX_ADDRESS is missing in .env")
    _refuse_placeholder("POD_SHARED_MAILBOX_ADDRESS", POD_SHARED_MAILBOX_ADDRESS)
    return POD_SHARED_MAILBOX_ADDRESS


# Root folders of the POD scans, one per year, separated by ";" (D-003).
SCAN_FOLDERS = [Path(folder) for folder in _split_list(os.getenv("POD_SCAN_FOLDERS", ""))]
# Folders excluded from every scan root (relative to each root or absolute).
SCAN_EXCLUDED_FOLDERS = [
    Path(folder) for folder in _split_list(os.getenv("POD_SCAN_EXCLUDED_FOLDERS", ""))
]

# "draft" saves replies as Outlook drafts (pilot, D-008); "send" sends them.
# draft: every reply a draft; partial: sent to the senders of POD_SEND_DOMAINS, drafts
# for the rest; send: every reply sent (product owner, D-057).
SEND_MODES = ("draft", "partial", "send")
SEND_MODE = os.getenv("POD_SEND_MODE", "draft").strip().lower()
if SEND_MODE not in SEND_MODES:
    raise ConfigError(f"POD_SEND_MODE must be draft, partial or send, not {SEND_MODE!r}")
SEND_DOMAINS_SETTING = "POD_SEND_DOMAINS"
_DOMAIN = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+")

# Customers whose scans sit in their own folder: "sender domain=folder" pairs
# separated by ";" (bug report item 17). The folder is a path under each scan
# folder ("Customer" means <year folder>\Customer). Their files are looked for
# there; a match only outside it goes to the team instead of being sent.
SENDER_FOLDERS_SETTING = "POD_SENDER_FOLDERS"


# Letters a connote may start with, such as AB in AB-10234 or AB10234, separated
# by ";" and tried in that order (product owner, D-060). Empty: digits-only connotes.
CONNOTE_PREFIXES_SETTING = "POD_CONNOTE_PREFIXES"


def connote_prefixes() -> tuple[str, ...]:
    """The connote prefixes from POD_CONNOTE_PREFIXES, upper-cased, in the order given (D-060)."""
    prefixes = tuple(part.upper() for part in _split_list(os.getenv(CONNOTE_PREFIXES_SETTING, "")))
    bad = [prefix for prefix in prefixes if not prefix.isalpha()]
    if bad:
        raise ConfigError(f"{CONNOTE_PREFIXES_SETTING}: '{bad[0]}' is not letters only, such as AB")
    return prefixes


def _domain(sender_email: str) -> str:
    """The part of an address after its @, lower-cased."""
    return str(sender_email or "").strip().lower().rpartition("@")[2]


def split_domains(value: str) -> list[str]:
    """The domains of a "domain;domain" setting, lower-cased, without a leading @."""
    return [part.lower().lstrip("@") for part in _split_list(value or "")]


def send_domains() -> list[str]:
    """The senders' domains replies are sent to in partial mode (POD_SEND_DOMAINS, D-057)."""
    domains = split_domains(os.getenv(SEND_DOMAINS_SETTING, ""))
    bad = [domain for domain in domains if not _DOMAIN.fullmatch(domain)]
    if bad:
        raise ConfigError(f"{SEND_DOMAINS_SETTING}: '{bad[0]}' is not a domain such as example.com")
    return domains


def reply_mode(sender_email: str) -> str:
    """draft or send for a reply to this sender: partial sends only to POD_SEND_DOMAINS (product owner, D-057)."""
    if SEND_MODE == "partial":
        return "send" if _domain(sender_email) in send_domains() else "draft"
    return SEND_MODE


def _by_sender_domain(setting: str, sender_email: str, what: str) -> str | None:
    """The value set for this sender's domain, the part after its @ exactly, in a "domain=value;..." setting (D-057)."""
    domain = _domain(sender_email)
    for pair in _split_list(os.getenv(setting, "")):
        configured, separator, value = (part.strip() for part in pair.partition("="))
        if not separator or not configured or not value:
            raise ConfigError(f"{setting}: '{pair}' is not 'domain={what}'")
        _refuse_placeholder(setting, pair)
        configured = configured.lower().lstrip("@")
        if domain and domain == configured:  # not its subdomains (product owner)
            return value
    return None


def sender_folder(sender_email: str) -> str | None:
    """The folder that holds this sender's scans, from POD_SENDER_FOLDERS, or None."""
    return _by_sender_domain(SENDER_FOLDERS_SETTING, sender_email, "folder")


def sender_check_word(sender_email: str) -> str | None:
    """The word this sender's Power BI rows must hold in their sender column, from POD_PBI_SENDER_CHECK, or None."""
    word = _by_sender_domain(PBI_SENDER_CHECK_SETTING, sender_email, "word")
    return word.casefold() if word else None


# The reply's signature as an HTML file, kept outside the repository because
# it holds the company's details (name, address, phone, website). A relative
# path is under the project folder. Empty: a plain "Kind Regards, POD Teams".
SIGNATURE_FILE_SETTING = "POD_SIGNATURE_FILE"


def signature_html() -> str:
    """The signature HTML from POD_SIGNATURE_FILE, or "" when unset; fails naming the setting."""
    value = os.getenv(SIGNATURE_FILE_SETTING, "").strip().strip('"')
    if not value:
        return ""
    _refuse_placeholder(SIGNATURE_FILE_SETTING, value)
    path = Path(value) if Path(value).is_absolute() else PROJECT_DIR / value
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"{SIGNATURE_FILE_SETTING}: cannot read {path} ({error})") from error


# The files of an email held for their size are copied here for the team to
# send by hand (D-038): a folder synced to SharePoint, outside the project and
# the scan folders. The URL is the same folder on SharePoint, for the link in
# Teams. Both empty: no copy, the Teams message lists the files only.
MANUAL_UPLOAD_DIR_SETTING = "POD_MANUAL_UPLOAD_DIR"
MANUAL_UPLOAD_URL_SETTING = "POD_MANUAL_UPLOAD_URL"


def manual_upload_dir() -> Path | None:
    """The folder for files to send by hand, or None; fails naming the setting if it is not allowed."""
    value = os.getenv(MANUAL_UPLOAD_DIR_SETTING, "").strip().strip('"')
    if not value:
        return None
    _refuse_placeholder(MANUAL_UPLOAD_DIR_SETTING, value)
    folder = Path(value).resolve()
    # Inside a scan folder the copies would be found as scans; inside the project they could reach git.
    for root, what in [(PROJECT_DIR, "the project"), *[(Path(root), "POD_SCAN_FOLDERS") for root in SCAN_FOLDERS]]:
        if folder.is_relative_to(Path(root).resolve()):
            raise ConfigError(f"{MANUAL_UPLOAD_DIR_SETTING}: '{value}' is inside {what}; use a folder outside it")
    return folder


# Larger replies are held for the team instead of failing in Outlook. Files
# grow by about a third when sent, and many receiving servers refuse emails
# over 25 MB: 20 MB of files stays under both.
MAX_ATTACHMENT_MB = float(os.getenv("POD_MAX_ATTACHMENT_MB", "20"))

# Move replied emails whose every POD was attached into "Actioned/Finished"
# (D-031): "on" or "off", set in the dashboard, which also records when it was
# switched on; only replies sent after that moment are moved.
MOVE_FINISHED = os.getenv("POD_MOVE_FINISHED", "off").strip().lower() == "on"
MOVE_FINISHED_SINCE = os.getenv("POD_MOVE_FINISHED_SINCE", "").strip()
# The folder's exact name, directly under POD_OUTLOOK_MAILBOX; it may hold a "/".
FINISHED_FOLDER = os.getenv("POD_FINISHED_FOLDER", "").strip() or "Actioned/Finished"

# Power BI dataset names (D-004). Read with required() where the query is built.
PBI_TABLE_SETTING = "POD_PBI_TABLE"
PBI_CONNOTE_COLUMN_SETTING = "POD_PBI_CONNOTE_COLUMN"
PBI_REFERENCE_COLUMN_SETTING = "POD_PBI_REFERENCE_COLUMN"
PBI_STATUS_COLUMN_SETTING = "POD_PBI_STATUS_COLUMN"
# The column (a name from POD_PBI_COLUMNS) with the path of a row's scan,
# used for every sender when it names an indexed PDF (D-044).
PBI_SCAN_PATH_COLUMN_SETTING = "POD_PBI_SCAN_PATH_COLUMN"
# Customers whose Power BI rows count only when their sender column holds a
# word: "email domain=word;...". A row without it is not what the customer
# asked for (product owner, D-044). The column is a name from POD_PBI_COLUMNS.
PBI_SENDER_CHECK_SETTING = "POD_PBI_SENDER_CHECK"
PBI_SENDER_COLUMN_SETTING = "POD_PBI_SENDER_COLUMN"
# Other tables, linked to the main table, and the columns to bring back
# (D-027). Both are "Table[Column]" pairs separated by ";", edited in .env
# to match the dataset:
#   POD_PBI_LINKS=POD Scans[Connote]=TMS[Connote]
#   POD_PBI_COLUMNS=Sender=TMS[Sender];POD Status=POD Scans[POD Status]
PBI_LINKS_SETTING = "POD_PBI_LINKS"
PBI_COLUMNS_SETTING = "POD_PBI_COLUMNS"
_TABLE_COLUMN = re.compile(r"^\s*(.+?)\[(.+)\]\s*$")


def _table_column(text: str, setting: str) -> tuple[str, str]:
    _refuse_placeholder(setting, text.strip())
    match = _TABLE_COLUMN.match(text)
    if not match:
        raise ConfigError(f"{setting}: '{text.strip()}' is not 'Table[Column]'")
    return match.group(1).strip(), match.group(2).strip()


def pbi_links() -> dict[str, tuple[str, str]]:
    """{other table: (its key column, main table's key column)} from POD_PBI_LINKS."""
    main = required(PBI_TABLE_SETTING)
    links = {}
    for pair in _split_list(os.getenv(PBI_LINKS_SETTING, "")):
        left, separator, right = pair.partition("=")
        if not separator:
            raise ConfigError(f"{PBI_LINKS_SETTING}: '{pair}' is not 'Table[Column]=Table[Column]'")
        (table_a, column_a), (table_b, column_b) = _table_column(left, PBI_LINKS_SETTING), _table_column(right, PBI_LINKS_SETTING)
        if table_b == main and table_a != main:
            links[table_a] = (column_a, column_b)
        elif table_a == main and table_b != main:
            links[table_b] = (column_b, column_a)
        else:
            raise ConfigError(f"{PBI_LINKS_SETTING}: '{pair}' must link another table to {main}")
    return links


def pbi_columns() -> list[tuple[str, str, str]]:
    """[(output name, table, column)] from POD_PBI_COLUMNS; every table must be the main one or linked."""
    main = required(PBI_TABLE_SETTING)
    links = pbi_links()
    columns = []
    for pair in _split_list(os.getenv(PBI_COLUMNS_SETTING, "")):
        name, separator, reference = pair.partition("=")
        if not separator or not name.strip():
            raise ConfigError(f"{PBI_COLUMNS_SETTING}: '{pair}' is not 'Name=Table[Column]'")
        table, column = _table_column(reference, PBI_COLUMNS_SETTING)
        if table != main and table not in links:
            raise ConfigError(f"{PBI_COLUMNS_SETTING}: table '{table}' is not {main} and has no link in {PBI_LINKS_SETTING}")
        columns.append((name.strip(), table, column))
    return columns


# A second table searched for the numbers, for consignments that are not in
# the main table (some customers' consignments are only in DeliveryApp, D-027). Its columns are
# "Name=Column" with the same names as POD_PBI_COLUMNS; a name it leaves
# out comes back empty for its rows.
PBI_SECOND_TABLE_SETTING = "POD_PBI_SECOND_TABLE"
PBI_SECOND_CONNOTE_COLUMN_SETTING = "POD_PBI_SECOND_CONNOTE_COLUMN"
PBI_SECOND_REFERENCE_COLUMN_SETTING = "POD_PBI_SECOND_REFERENCE_COLUMN"
PBI_SECOND_COLUMNS_SETTING = "POD_PBI_SECOND_COLUMNS"


def pbi_second_table() -> tuple[str, str, str, dict[str, str]] | None:
    """(table, connote column, reference column, {name: column}), or None when not set."""
    table = os.getenv(PBI_SECOND_TABLE_SETTING, "").strip()
    if not table:
        return None
    _refuse_placeholder(PBI_SECOND_TABLE_SETTING, table)
    names = {name for name, _, _ in pbi_columns()}
    if not names:
        raise ConfigError(f"{PBI_SECOND_TABLE_SETTING} needs {PBI_COLUMNS_SETTING}, so both tables return the same columns")
    columns = {}
    for pair in _split_list(os.getenv(PBI_SECOND_COLUMNS_SETTING, "")):
        name, separator, column = pair.partition("=")
        name, column = name.strip(), column.strip()
        if not separator or not column:
            raise ConfigError(f"{PBI_SECOND_COLUMNS_SETTING}: '{pair}' is not 'Name=Column'")
        if "[" in column or "]" in column:
            column_table, column = _table_column(column, PBI_SECOND_COLUMNS_SETTING)
            if column_table.casefold() != table.casefold():
                raise ConfigError(
                    f"{PBI_SECOND_COLUMNS_SETTING}: table '{column_table}' does not match "
                    f"{PBI_SECOND_TABLE_SETTING} '{table}'"
                )
        else:
            _refuse_placeholder(PBI_SECOND_COLUMNS_SETTING, column)
        if name not in names:
            raise ConfigError(f"{PBI_SECOND_COLUMNS_SETTING}: '{name}' is not a name in {PBI_COLUMNS_SETTING}")
        columns[name] = column
    reference = os.getenv(PBI_SECOND_REFERENCE_COLUMN_SETTING, "").strip()
    if reference:
        _refuse_placeholder(PBI_SECOND_REFERENCE_COLUMN_SETTING, reference)
    return table, required(PBI_SECOND_CONNOTE_COLUMN_SETTING), reference, columns


# POD Outstanding row, as "Excel column=source" pairs separated by ";" (D-007).
# Sources: number, connote, reference, pod_status, sender_email, subject,
# received_time, requested_at, or pbi:<Power BI column>.
OUTSTANDING_COLUMNS_SETTING = "POD_OUTSTANDING_COLUMNS"


def outstanding_columns() -> dict[str, str]:
    """The POD Outstanding column map; fails naming the setting if unusable."""
    mapping = {}
    for pair in _split_list(required(OUTSTANDING_COLUMNS_SETTING)):
        column, separator, source = pair.partition("=")
        if not separator or not column.strip() or not source.strip():
            raise ConfigError(
                f"{OUTSTANDING_COLUMNS_SETTING}: '{pair}' is not 'Excel column=source'"
            )
        mapping[column.strip()] = source.strip()
    return mapping

# Settings the dashboard may change, with the check each value must pass.
EDITABLE_SETTINGS = {
    "POD_CONFIDENCE_THRESHOLD": lambda value: 0.5 <= float(value) < 1.0,
    # The pilot's PODs unchanged target and the intent model's precision target (product owner, D-054).
    "POD_PILOT_TARGET": lambda value: 0.5 <= float(value) <= 1.0,
    "POD_PRECISION_TARGET": lambda value: 0.5 <= float(value) <= 1.0,
    "POD_SEND_MODE": lambda value: value in SEND_MODES,
    "POD_SEND_DOMAINS": lambda value: all(_DOMAIN.fullmatch(domain) for domain in split_domains(value)),
    "POD_MOVE_FINISHED": lambda value: value in {"on", "off"},
    "POD_DASHBOARD_AUTO_REFRESH": lambda value: value in {"on", "off"},
    "POD_DASHBOARD_REFRESH_INTERVAL_SECONDS": lambda value: (
        value.isdigit() and 10 <= int(value) <= 3600
    ),
    # An ISO time with its zone, written by the dashboard when moving is switched on.
    "POD_MOVE_FINISHED_SINCE": lambda value: datetime.fromisoformat(value).tzinfo is not None,
}


DEFAULT_TARGET = "0.90"  # product owner, D-054: only when .env has no POD_PILOT_TARGET or POD_PRECISION_TARGET line


def pilot_target() -> float:
    """The share of drafts sent with the same PODs at which to switch to sending (D-014, D-054)."""
    return float(current_setting("POD_PILOT_TARGET", DEFAULT_TARGET))


def precision_target() -> float:
    """The share of emails acted on that must be POD requests, to choose the threshold by (D-054)."""
    return float(current_setting("POD_PRECISION_TARGET", DEFAULT_TARGET))


def current_setting(name: str, default: str = "") -> str:
    """The value in `.env` now (not the value this process started with)."""
    env_path = PROJECT_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == name:
                return value.strip().strip('"').strip("'")
    return default


def save_setting(name: str, value: str) -> None:
    """Write one dashboard-editable setting to `.env`; refuses anything else.

    The scheduled run reads `.env` at start, so the change applies from the
    next run.
    """
    check = EDITABLE_SETTINGS.get(name)
    try:
        allowed = check is not None and check(value)
    except ValueError:
        allowed = False
    if not allowed:
        raise ConfigError(f"{name}={value!r} is not an allowed dashboard setting")
    env_path = PROJECT_DIR / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    setting = f"{name}={value}"
    for index, line in enumerate(lines):
        if line.partition("=")[0].strip() == name:
            lines[index] = setting
            break
    else:
        lines.append(setting)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
