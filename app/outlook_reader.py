"""Read and send email through the locally installed Outlook application."""

import re
from datetime import datetime, timezone
from pathlib import Path

from .entity_extractor import extract_latest_email_body
from . import reply_generator

OL_FOLDER_SENT_MAIL = 5  # Outlook's OlDefaultFolders.olFolderSentMail
# As classic Outlook draws it above "From:" in a manual reply.
QUOTE_SEPARATOR = '<div style="border:none;border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0cm 0cm 0cm"></div>'


def _outlook_quote_start(reply_html: str) -> int:
    """Locate Outlook's quoted message, excluding the user's signature."""
    marker = re.search(
        r"""<div\b[^>]*\bid\s*=\s*["']divRplyFwdMsg["'][^>]*>""",
        reply_html,
        flags=re.IGNORECASE,
    )
    if marker:
        separator = reply_html.rfind("<hr", 0, marker.start())
        start = separator if separator >= 0 else marker.start()
    else:
        from_matches = list(
            re.finditer(
                r"""(?:<b\b[^>]*>\s*)?From:\s*(?:</b>\s*)?""",
                reply_html,
                flags=re.IGNORECASE,
            )
        )
        quote = next(
            (
                match for match in from_matches
                if re.search(r"(?i)Subject\s*:", reply_html[match.end():match.end() + 4000])
            ),
            None,
        )
        if quote is None:
            raise ValueError("Could not identify Outlook's quoted email; refusing to remove its reply body.")
        head = reply_html[:quote.start()]
        # Classic Outlook draws the separator as the top border of the div around "From:".
        border = next(reversed(list(re.finditer(r"<div\b[^>]*border-top[^>]*>", head, flags=re.IGNORECASE))), None)
        separator = reply_html.rfind("<hr", 0, quote.start())
        if border and "</div>" not in head[border.end():].lower():
            wrapper = re.search(r"<div>\s*$", head[:border.start()], flags=re.IGNORECASE)
            start = wrapper.start() if wrapper else border.start()
        elif separator >= 0:
            start = separator
        else:
            # Start at the paragraph "From:" is in, so its bold and spans stay whole.
            paragraph = next(reversed(list(re.finditer(r"<p\b", head, flags=re.IGNORECASE))), None)
            start = paragraph.start() if paragraph and "</p>" not in head[paragraph.end():].lower() else quote.start()

    return start


def _outlook_quoted_html(reply_html: str) -> str:
    """Keep Outlook's original quote markup, excluding the user's signature."""
    quoted = reply_html[_outlook_quote_start(reply_html):]
    return re.sub(r"(?is)</body>\s*</html>\s*$", "", quoted).strip()


def _outlook_reply_html(reply_html: str, body: str) -> str:
    """Replace Outlook's signature while preserving its native reply document."""
    body_tag = re.search(r"<body\b[^>]*>", reply_html, flags=re.IGNORECASE)
    if body_tag is None:
        raise ValueError("Could not identify Outlook's reply body.")
    quote_start = _outlook_quote_start(reply_html)
    if body_tag.end() > quote_start:
        raise ValueError("Outlook's quoted email starts before its reply body.")
    quoted = reply_html[quote_start:]
    # The quote keeps Outlook's own separator; one is drawn only if it has none.
    has_separator = re.match(r"(?is)(<div>\s*)?(<hr|<div\b[^>]*border-top)", quoted)
    return (
        reply_html[:body_tag.end()]
        + reply_generator.build_reply_fragment(body)
        + ("" if has_separator else QUOTE_SEPARATOR)
        + quoted
    )


class DraftOutsideMailbox(RuntimeError):
    """A draft reply did not end up in the POD mailbox's Drafts folder."""


def _open(entry_id: str, store_id: str = ""):
    """An Outlook item by entry ID, looked up in `store_id`'s mailbox when given."""
    namespace = connect_outlook()
    return namespace.GetItemFromID(entry_id, store_id) if store_id else namespace.GetItemFromID(entry_id)


# Outlook's own record of the last reply or forward on an email: what it shows
# as "You replied to this message on ..." (MAPI PR_LAST_VERB_EXECUTED and its time).
_LAST_VERB = "http://schemas.microsoft.com/mapi/proptag/0x10810003"
_LAST_VERB_TIME = "http://schemas.microsoft.com/mapi/proptag/0x10820040"
_REPLY_VERBS = {102: "reply", 103: "reply all"}  # 104, forward, does not answer the customer


_MESSAGE_ID = "http://schemas.microsoft.com/mapi/proptag/0x1035001F"  # PR_INTERNET_MESSAGE_ID
# Whom an email was sent on behalf of (PR_SENT_REPRESENTING_SMTP_ADDRESS), and a recipient's address (PR_SMTP_ADDRESS).
_SENT_FOR = "http://schemas.microsoft.com/mapi/proptag/0x5D02001F"
_SMTP_ADDRESS = "http://schemas.microsoft.com/mapi/proptag/0x39FE001F"
OL_TO, OL_CC = 1, 2  # Outlook's OlMailRecipientType


def _message_id(item) -> str:
    """The email's Internet Message ID, set by the sender; it stays the same when the email is moved."""
    try:
        return str(item.PropertyAccessor.GetProperty(_MESSAGE_ID) or "")
    except Exception:
        return ""


def _sent_for(item) -> str:
    """The address an email was sent on behalf of, or "": the POD mailbox for the team's replies (D-055)."""
    try:
        return str(item.PropertyAccessor.GetProperty(_SENT_FOR) or "")
    except Exception:
        return ""


def _recipient_address(recipient) -> str:
    """A recipient's SMTP address; an Exchange one is looked up, as the sender's is."""
    try:
        user = recipient.AddressEntry.GetExchangeUser()
        if user is not None and user.PrimarySmtpAddress:
            return str(user.PrimarySmtpAddress)
    except Exception:
        pass  # not an Exchange user: the address as written, below
    try:
        return str(recipient.PropertyAccessor.GetProperty(_SMTP_ADDRESS) or "")
    except Exception:
        return str(getattr(recipient, "Address", "") or "")


def _pod_mailbox_in_cc(recipients, address: str) -> None:
    """The POD mailbox (POD_SHARED_MAILBOX_ADDRESS) in CC, never in To, also when the email was not sent to it (product owner, D-055).

    The reply comes back to the POD inbox that way, so the team sees it was
    answered. Its other recipients stay where Reply All put them.
    """
    own = [recipient for recipient in _iter_collection(recipients)
           if _recipient_address(recipient).casefold() == address.casefold()]
    for recipient in own or [recipients.Add(address)]:
        recipient.Type = OL_CC
    recipients.ResolveAll()


def replied_manually(entry_id: str, store_id: str = "") -> dict | None:
    """{"action", "at"} if someone answered this email with Reply or Reply All, else None (D-039).

    Outlook (desktop and web) sets the mark on the email in the POD mailbox,
    whoever replied and from which PC. An email it cannot open raises, so a
    failed check never passes as "not replied".
    """
    return _reply_mark(_open(entry_id, store_id))


def save_attachments(entry_id: str, store_id: str, folder: Path) -> list[dict]:
    """The email's PDF, spreadsheet and picture attachments, saved into `folder`: [{"name", "path", "size"}] (D-044).

    Each is saved under a name made here ("<position>_<its own name>"), so a
    name from the email can never point outside `folder`.
    """
    wanted = {".pdf", ".xlsx", ".xlsm", ".xls", ".csv", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff"}
    saved = []
    for position, attachment in enumerate(_iter_collection(_open(entry_id, store_id).Attachments)):
        name = Path(str(getattr(attachment, "FileName", "") or "")).name
        if int(getattr(attachment, "Type", 1) or 1) != 1 or Path(name).suffix.lower() not in wanted:
            continue  # only files (not attached emails or links) of the kinds read
        path = Path(folder) / f"{position}_{name}"
        attachment.SaveAsFile(str(path))
        saved.append({"name": name, "path": path, "size": int(getattr(attachment, "Size", 0) or path.stat().st_size)})
    return saved


def email_status(entry_id: str, store_id: str = "") -> dict | None:
    """Where a waiting email is and whether someone replied to it, or None if Outlook cannot open it (D-041).

    {"folder": its folder, "manual": as `replied_manually`}. Outlook gives a
    moved email a new entry ID, so None means moved or deleted.
    """
    try:
        item = _open(entry_id, store_id)
    except Exception:
        # shortcut: any error counts as moved or deleted; tell MAPI_E_NOT_FOUND apart if Outlook glitches show up as moves.
        return None
    return {"folder": item.Parent, "manual": _reply_mark(item)}


def in_child_folder(parent_folder, folder_name: str, message_id: str) -> bool:
    """Whether the email with this Internet Message ID is in the folder `folder_name` directly under `parent_folder`."""
    folder = _find_child_folder(parent_folder, folder_name)
    if folder is None or not message_id:
        return False
    quoted = message_id.replace("'", "''")
    return folder.Items.Restrict(f"@SQL=\"{_MESSAGE_ID}\" = '{quoted}'").Count > 0


def _folders_under(folder):
    """A folder and every folder below it."""
    yield folder
    for child in _iter_collection(folder.Folders):
        yield from _folders_under(child)


def find_by_message_id(mailbox_folder, message_id: str, finished_name: str) -> dict | None:
    """{"entry_id", "in_finished"} of the email with this Internet Message ID, or None (D-059).

    A person who moves an email gives it a new entry ID; its Internet Message
    ID stays. Looked for in the mailbox folder and its `finished_name`
    folder first, then in every folder of the mailbox.
    """
    if not message_id:
        return None
    condition = f"@SQL=\"{_MESSAGE_ID}\" = '{message_id.replace(chr(39), chr(39) * 2)}'"

    def entry_in(folder) -> str:
        try:
            found = folder.Items.Restrict(condition)
            return str(found.Item(1).EntryID) if found.Count else ""
        except Exception:
            return ""  # a folder that cannot be searched, such as a calendar

    finished = _find_child_folder(mailbox_folder, finished_name)
    for folder, in_finished in ((mailbox_folder, False), (finished, True)):
        if folder is not None and (entry_id := entry_in(folder)):
            return {"entry_id": entry_id, "in_finished": in_finished}
    for folder in _folders_under(mailbox_folder.Store.GetRootFolder()):
        if entry_id := entry_in(folder):
            return {"entry_id": entry_id, "in_finished": False}
    return None


def _reply_mark(item) -> dict | None:
    accessor = item.PropertyAccessor
    try:
        verb = int(accessor.GetProperty(_LAST_VERB))
    except Exception:
        return None  # never replied to or forwarded: Outlook has no such property
    if verb not in _REPLY_VERBS:
        return None
    try:
        # PropertyAccessor gives times in UTC. (Unverified in production.)
        value = accessor.GetProperty(_LAST_VERB_TIME)
        at = datetime(value.year, value.month, value.day, value.hour, value.minute, value.second,
                      tzinfo=timezone.utc).isoformat()
    except Exception:
        at = ""
    return {"action": _REPLY_VERBS[verb], "at": at}


def open_in_outlook(entry_id: str, mailbox_name: str) -> None:
    """Open an email in its own Outlook Desktop window, looked up in the POD mailbox only.

    For the dashboard, which calls Outlook from its own thread, so COM is set
    up for that thread first. Outlook marks the email read, as when it is
    opened by hand (product owner); nothing else about it changes.
    """
    try:
        import pythoncom

        pythoncom.CoInitialize()  # once per thread; repeated calls are harmless
    except ImportError:
        pass  # no pywin32: connect_outlook reports it
    _open(entry_id, mailbox_store_id(mailbox_name)).Display(False)


def mailbox_store_id(folder_name: str) -> str:
    """The StoreID of the mailbox that `folder_name` (POD_OUTLOOK_MAILBOX) is in."""
    return str(get_folder(connect_outlook(), folder_name).Store.StoreID)


def mailbox_drafts(store):
    """The "Drafts" folder directly under a mailbox's root, found by its name.

    For a shared mailbox in someone's profile, Outlook saves a new reply in
    that person's own Drafts, and GetDefaultFolder(olFolderDrafts) on the
    shared store did not reliably give the shared one: the product owner saw agent
    drafts, and their own replies, land in personal Drafts (D-032).
    """
    drafts = _find_child_folder(store.GetRootFolder(), "Drafts")
    if drafts is None:
        raise LookupError(f"No 'Drafts' folder directly under mailbox '{store.DisplayName}'")
    return drafts


def connect_outlook():
    """Return the Outlook MAPI namespace from the running desktop app."""
    try:
        import win32com.client
    except ImportError as error:
        raise RuntimeError("pywin32 is required to connect to Outlook") from error

    outlook = win32com.client.Dispatch("Outlook.Application")
    return outlook.GetNamespace("MAPI")


def _iter_collection(collection):
    for index in range(1, collection.Count + 1):
        yield collection.Item(index)


def _find_child_folder(parent, folder_name: str):
    for child in _iter_collection(parent.Folders):
        if child.Name.casefold() == folder_name.casefold():
            return child
    return None


def get_folder(namespace, folder_name: str):
    """Find a folder by mailbox-relative path without using the default mailbox.

    The first path component must be the exact display name of an Outlook
    mailbox/store, such as ``POD`` or ``pod@example.com``. This is intentional:
    silently falling back to the personal default Inbox could process the wrong
    mailbox.
    """
    parts = [part for part in folder_name.replace("\\", "/").split("/") if part]
    if not parts:
        raise ValueError("folder_name cannot be empty")

    current = None
    available_roots = []
    for root in _iter_collection(namespace.Folders):
        available_roots.append(str(root.Name))
        if root.Name.casefold() == parts[0].casefold():
            current = root
            break
    if current is None:
        available = ", ".join(available_roots) or "(none)"
        raise LookupError(
            f"Outlook mailbox/folder not found: {folder_name}. "
            f"Available mailbox roots: {available}. "
            "Set POD_OUTLOOK_MAILBOX to the exact shared mailbox display name."
        )

    for part in parts[1:]:
        current = _find_child_folder(current, part)
        if current is None:
            raise LookupError(f"Outlook folder not found: {folder_name}")
    return current


def smtp_address(item) -> str:
    """The sender's SMTP address; internal (Exchange) senders need a lookup."""
    address = str(getattr(item, "SenderEmailAddress", "") or "")
    if str(getattr(item, "SenderEmailType", "") or "").upper() == "EX":
        try:
            user = item.Sender.GetExchangeUser()
            if user is not None and user.PrimarySmtpAddress:
                return str(user.PrimarySmtpAddress)
        except Exception:
            pass  # Keep the Exchange address; the guard and reply still work.
    return address


def get_unread_emails(folder, processed_ids: set[str]) -> list[dict]:
    """Unread mail items not yet processed, oldest first, as plain dictionaries.

    Only unread items are read (D-002): a read email may already have been
    handled by a colleague. Outlook filters them (`Restrict`), so a large
    folder is not walked. Emails the agent could not finish stay unread and
    are picked up again only if their ID is not in `processed_ids`.
    """
    items = folder.Items.Restrict("[UnRead] = True")
    items.Sort("[ReceivedTime]")
    emails = []
    for item in _iter_collection(items):
        entry_id = str(getattr(item, "EntryID", ""))
        if not entry_id or entry_id in processed_ids:
            continue
        full_body = str(getattr(item, "Body", "") or "")
        emails.append(
            {
                "entry_id": entry_id,
                "message_class": str(getattr(item, "MessageClass", "") or ""),
                "subject": str(getattr(item, "Subject", "") or ""),
                "sender_email": smtp_address(item),
                "sent_for": _sent_for(item),  # a reply the team sent on behalf of the POD mailbox (D-055)
                "to_recipients": str(getattr(item, "To", "") or ""),
                "received_time": getattr(item, "ReceivedTime", None),
                "message_id": _message_id(item),
                "body": extract_latest_email_body(full_body),
                # Whole email incl. quoted history, for the forwarded-request
                # fallback (D-017). Not written to tracking.
                "full_body": full_body,
            }
        )
    return emails


def reply(
    entry_id: str,
    body: str,
    attachments: list[Path],
    mode: str,
    send_as: str = "",
    store_id: str = "",
) -> dict:
    """Reply to the sender of an email with the given text and PDFs.

    `store_id` is the POD mailbox's (`mailbox_store_id`), so the email is opened
    in that mailbox. A draft goes to that mailbox's own Drafts folder, or the
    reply fails naming where it went instead (D-032).

    Outlook's own reply keeps the recipient and "RE:" subject. `mode` is
    "draft" (saved for a person to review and send, D-008) or "send".
    `send_as` makes the reply come from the POD mailbox; it needs send-as or
    send-on-behalf rights on that mailbox. Returns the draft's entry ID and
    the conversation ID, which the pilot measure uses to find what was sent
    (D-014).
    """
    if mode not in {"draft", "send"}:
        raise ValueError(f"mode must be draft or send, not {mode!r}")
    item = _open(entry_id, store_id)
    # Found before anything is saved, so a missing folder leaves no stray draft.
    drafts = mailbox_drafts(item.Parent.Store) if mode == "draft" else None
    response = item.ReplyAll()
    if send_as:
        response.SentOnBehalfOfName = send_as
        _pod_mailbox_in_cc(response.Recipients, send_as)
    original_reply_html = str(getattr(response, "HTMLBody", "") or "")
    response.HTMLBody = _outlook_reply_html(original_reply_html, body)
    for path in attachments:
        response.Attachments.Add(str(path))
    conversation_id = str(getattr(item, "ConversationID", "") or "")
    if mode == "draft":
        response.Save()  # Outlook may put it in the person's own Drafts first
        response = response.Move(drafts)
        saved_in = response.Parent
        if (str(saved_in.Store.StoreID) != str(item.Parent.Store.StoreID)
                or str(saved_in.FolderPath) != str(drafts.FolderPath)):
            raise DraftOutsideMailbox(
                f"The draft was saved in '{saved_in.FolderPath}', not in '{drafts.FolderPath}'. "
                "Delete it there, then process the email again."
            )
        return {
            "draft_entry_id": str(response.EntryID),
            "draft_store_id": str(saved_in.Store.StoreID),
            "conversation_id": conversation_id,
        }
    response.Send()
    return {"conversation_id": conversation_id}


def draft_exists(entry_id: str, store_id: str = "") -> bool:
    """Whether a draft the agent saved is still an unsent draft."""
    try:
        namespace = connect_outlook()
        item = (
            namespace.GetItemFromID(entry_id, store_id)
            if store_id
            else namespace.GetItemFromID(entry_id)
        )
    except Exception:
        return False  # sent (it moved and got a new ID) or deleted
    return not bool(getattr(item, "Sent", False))


def move_to_child_folder(entry_id: str, parent_folder, folder_name: str) -> str:
    """Move an email into a folder directly under `parent_folder`; return its new entry ID.

    The folder is matched by its exact name, which may itself hold a "/"
    ("Actioned/Finished"), so it is looked up among the children, not as a path.
    """
    folder = _find_child_folder(parent_folder, folder_name)
    if folder is None:
        raise LookupError(f"Folder '{folder_name}' not found under '{parent_folder.Name}'")
    item = connect_outlook().GetItemFromID(entry_id, str(parent_folder.Store.StoreID))
    return str(item.Move(folder).EntryID)


def add_categories(entry_id: str, categories: list[str], store_id: str = "") -> list[str]:
    """Add missing Outlook categories without removing the item's existing ones."""
    namespace = connect_outlook()
    item = (
        namespace.GetItemFromID(entry_id, store_id)
        if store_id
        else namespace.GetItemFromID(entry_id)
    )
    existing = [
        category.strip()
        for category in str(getattr(item, "Categories", "") or "").split(",")
        if category.strip()
    ]
    existing_keys = {category.casefold() for category in existing}
    added = [category for category in categories if category.casefold() not in existing_keys]
    if added:
        item.Categories = ", ".join([*existing, *added])
        item.Save()
    return added


def _utc_iso(value) -> str:
    """An Outlook date as an ISO UTC string.

    pywin32 hands Outlook dates over as the local wall-clock time; they are
    read as local time and converted to UTC. (Unverified in production.)
    """
    local = datetime(value.year, value.month, value.day, value.hour, value.minute, value.second)
    return local.astimezone().astimezone(timezone.utc).isoformat()


def sent_replies_since(mailbox_folder, since_utc: str) -> list[dict]:
    """Emails sent since `since_utc`, from the POD mailbox's and the user's Sent Items.

    Where Outlook files a reply sent on behalf of the POD mailbox depends on
    the account's settings, so both folders are read.
    """
    folders = []
    try:
        folders.append(mailbox_folder.Store.GetDefaultFolder(OL_FOLDER_SENT_MAIL))
    except Exception:
        pass  # the shared store may not expose its Sent Items
    try:
        folders.append(connect_outlook().GetDefaultFolder(OL_FOLDER_SENT_MAIL))
    except Exception:
        pass
    since = datetime.fromisoformat(since_utc)
    found, seen = [], set()
    for folder in folders:
        items = folder.Items
        items.Sort("[SentOn]", True)
        for item in _iter_collection(items):
            sent_on = _utc_iso(getattr(item, "SentOn"))
            if datetime.fromisoformat(sent_on) < since:
                break  # newest first: everything after this is older
            entry_id = str(getattr(item, "EntryID", ""))
            if entry_id in seen:
                continue
            seen.add(entry_id)
            found.append({
                "conversation_id": str(getattr(item, "ConversationID", "") or ""),
                "sent_on": sent_on,
                "body": str(getattr(item, "Body", "") or ""),
                "attachments": [
                    str(attachment.FileName) for attachment in _iter_collection(item.Attachments)
                ],
            })
    return found


def mark_read(entry_id: str, store_id: str = "") -> None:
    """Mark an email read, so the team sees it was handled (D-002)."""
    item = _open(entry_id, store_id)
    item.UnRead = False
    item.Save()


def load_processed_ids(path: str | Path) -> set[str]:
    """Load processed Outlook entry IDs from a local text file."""
    source = Path(path)
    if not source.exists():
        return set()
    return {line.strip() for line in source.read_text(encoding="utf-8").splitlines() if line.strip()}


def forget_processed(entry_id: str, path: str | Path) -> None:
    """Remove an entry ID from the processed-ID file, so the next run reads it again."""
    source = Path(path)
    if not source.exists():
        return
    kept = [line for line in source.read_text(encoding="utf-8").splitlines() if line.strip() and line.strip() != entry_id]
    source.write_text("".join(f"{line}\n" for line in kept), encoding="utf-8", newline="\n")


def mark_as_processed(entry_id: str, path: str | Path) -> None:
    """Append an entry ID to the local processed-ID file."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("a", encoding="utf-8") as file:
        file.write(f"{entry_id}\n")