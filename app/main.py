"""One unattended cycle of the POD email workflow (PRD section 4).

Per new unread email: guard -> numbers -> intent -> scan search. Numbers not
on the share go to Power BI; ambiguous or unknown numbers go to the team.
Each email is a small state machine whose state is the latest tracking
event, so a run picks up where the previous one stopped and waiting for
Power BI or the team never blocks other emails.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import sys
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import attachments, classifier, config, handoff, model_registry, number_classifier
from . import outlook_reader, pilot, pod_files, reply_generator, tracking, training_refresh

_NO_REPLY_SENDER = re.compile(
    r"^(no-?reply|do-?not-?reply|mailer-daemon|postmaster)\b", re.IGNORECASE
)
_AUTOMATIC_SUBJECT = re.compile(
    r"^\s*(automatic reply|auto-?reply|out of (the )?office|undeliverable|"
    r"delivery status notification|mail delivery failed)",
    re.IGNORECASE,
)
_CATEGORY_AGENT_REPLY = "POD Agent Reply"
_CATEGORY_ON_LIST = "On List"
UNKNOWN_NUMBER = "number not on the share and not in Power BI"
OVERSIZE = "attachments total"  # start of the reason of a hold for attachment size (D-038)
UNREADABLE_LIST = "number list could not be read"
ATTACHMENTS_NOT_READ = "no number in the email, and its attachments could not all be read"
# Holds whose numbers the team can correct in Teams or the dashboard (D-036).
CORRECTABLE_HOLDS = (UNKNOWN_NUMBER, UNREADABLE_LIST)


# A Teams card that has not reached Teams this long after it was sent is sent
# again, at most this many times; then the dashboard asks the team to check
# Flow 3 (product owner, D-037).
CARD_RESEND_AFTER = timedelta(minutes=30)
CARD_RESENDS = 3

# How long a run that takes new emails waits for the lock, and how often it tries.
LOCK_WAIT_SECONDS = 120
LOCK_RETRY_SECONDS = 5
DASHBOARD_LOCK_WAIT_SECONDS = 30  # a dashboard save waits this long for a running cycle
REVIEW_SNIPPET_CHARS = 500  # of the email's newest message, on its Teams review card (product owner, D-048)


class AlreadyRunning(RuntimeError):
    """Another cycle holds the run lock."""


class ShareUnavailable(RuntimeError):
    """The scan folders cannot be read; stop before judging any POD missing."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _progress(enabled: bool, message: str) -> None:
    if enabled and sys.stdout is not None:
        print(f"[POD] {message}", flush=True)


@contextmanager
def _run_lock():
    """Only one cycle at a time (scheduler and dashboard share this lock).

    The operating system releases the lock if the process dies, so a crash
    never leaves it stuck.
    """
    # ponytail: msvcrt is Windows-only, like Outlook Desktop; use fcntl if
    # the agent ever runs on Linux.
    import msvcrt

    path = Path(config.LOCK_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise AlreadyRunning("Another run is in progress") from error
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


def skip_reason(email: dict) -> str:
    """Why an email must never get an automatic reply, or "" (D-002).

    Replying to the POD mailbox itself, an auto-reply, a bounce or a no-reply
    sender can start a loop between two automatic senders.
    """
    sender = str(email.get("sender_email") or "").strip()
    # A reply sent on behalf of the POD mailbox comes back to it in CC (D-055): its sender
    # is then the person who sent it, and the POD mailbox the one it was sent for.
    pod = config.POD_SHARED_MAILBOX_ADDRESS.casefold()
    if pod and pod in (sender.casefold(), str(email.get("sent_for") or "").strip().casefold()):
        return "sent by the POD mailbox"
    message_class = str(email.get("message_class") or "")
    if message_class and not (
        message_class == "IPM.Note" or message_class.startswith("IPM.Note.")
    ):
        return f"not an ordinary email ({message_class})"
    if "Rules.OofTemplate" in message_class:
        return "automatic reply"
    if _NO_REPLY_SENDER.match(sender.split("@", 1)[0]):
        return "no-reply sender"
    if _AUTOMATIC_SUBJECT.match(str(email.get("subject") or "")):
        return "automatic or bounce subject"
    return ""


def process_again(entries: list[dict]) -> int:
    """Let the next cycle process these emails from scratch (dashboard "Process again").

    Only statuses in `tracking.REPROCESSABLE` qualify: nothing was replied, so
    no customer gets a second reply. The history stays; a `Reset` event is
    added. The email must still be unread in Outlook to be read again. Holds
    the run lock, since a running cycle appends to the processed-ID file this
    rewrites. Raises AlreadyRunning while a cycle runs. Returns how many.
    """
    refused = [entry["status"] for entry in entries if entry["status"] not in tracking.REPROCESSABLE]
    if refused:
        raise ValueError(f"Cannot process again an email in status {refused[0]!r}")
    with _run_lock():
        for entry in entries:
            # Forget first: if recording fails, the email is still read again.
            outlook_reader.forget_processed(str(entry["entry_id"]), config.PROCESSED_IDS_PATH)
            _withdraw_cards(entry.get("state") or {})
            tracking.record(entry, tracking.RESET, reason="process again, asked in the dashboard")
    return len(entries)


def reprocess_power_bi(entries: list[dict]) -> int:
    """Recreate Power BI requests for waiting emails using the current query code.

    Move prior request and result files to the archive before writing the same
    request filename again. The new file-creation event retriggers Flow 1, and
    an old result cannot be mistaken for the new query's response.
    """
    refused = [entry["status"] for entry in entries if entry["status"] != tracking.WAITING_PBI]
    if refused:
        raise ValueError(f"Cannot reprocess Power BI for an email in status {refused[0]!r}")
    with _run_lock():
        latest = {event["entry_id"]: event for event in tracking.latest()}
        for entry in entries:
            current = latest.get(entry["entry_id"])
            if current is None or current["status"] != tracking.WAITING_PBI:
                status = current["status"] if current else "unknown"
                raise ValueError(f"This email is no longer waiting for Power BI (now {status})")

            numbers = [number for number in str(current.get("numbers") or "").split(";") if number]
            state = current.get("state") or {}
            scan_missing = state.get("pbi_asked") or state.get("missing") or []
            if not numbers or not scan_missing:
                raise ValueError("The waiting email has no recorded numbers missing from the scan folders")

            handoff.archive(f"{_pbi_request_id(current, state)}.json")
            state = _ask_power_bi(current, state, numbers, scan_missing)
            tracking.record(
                current,
                tracking.WAITING_PBI,
                reason="Power BI lookup reprocessed from the dashboard",
                state=state,
            )
    return len(entries)


def _locked(action, wait_seconds: float):
    """`action()` under the run lock, waiting up to `wait_seconds` for a running cycle; AlreadyRunning after."""
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            with _run_lock():
                return action()
        except AlreadyRunning:
            if time.monotonic() >= deadline:
                raise
            time.sleep(LOCK_RETRY_SECONDS)


def mark_resolved(entries: list[dict]) -> int:
    """Mark these emails Resolved (dashboard), under the run lock so a running cycle cannot overwrite it."""
    def resolve() -> int:
        for entry in entries:
            _withdraw_cards(entry.get("state") or {})
            tracking.record(entry, tracking.RESOLVED, reason="resolved in the dashboard")
        return len(entries)

    return _locked(resolve, DASHBOARD_LOCK_WAIT_SECONDS)


def split_numbers(text: str) -> list[str]:
    """Numbers typed by a person, separated by ";", "," or new lines; unique, in order."""
    return list(dict.fromkeys(part.strip() for part in re.split(r"[;,\n]+", text or "") if part.strip()))


_TYPED_NUMBER = re.compile(r"[A-Za-z0-9/\-]{4,20}")


def check_numbers(numbers: list[str]) -> None:
    """Refuse numbers typed by a person that cannot be a connote or reference (D-035); any count (D-047)."""
    for number in numbers:
        if not _TYPED_NUMBER.fullmatch(number):
            raise ValueError(
                f"Not a number: {number!r}. Use letters, digits, - and / only, 4 to 20 characters, "
                "no spaces; separate numbers with ;"
            )


def record_review(entry: dict, intent: str, connotes: str, references: str, via: str = "the dashboard") -> str:
    """Record a person's decision on an email the intent model was unsure of, or a held email's numbers.

    Under the run lock, waiting for a running cycle, so the cycle never
    records over the review (D-047); AlreadyRunning if it does not finish in
    time. A cycle itself, which holds the lock, uses `_record_review`.
    """
    return _locked(lambda: _record_review(entry, intent, connotes, references, via), DASHBOARD_LOCK_WAIT_SECONDS)


def _record_review(entry: dict, intent: str, connotes: str, references: str, via: str) -> str:
    """The review itself (`record_review`).

    `entry` is the email's latest tracking event. POD_REQUEST becomes
    `Reviewed`, and the next cycle continues it with these numbers (file
    search, then Power BI, `_resume_reviewed`), or with none, with the numbers
    in its attachments (D-046); OTHER becomes
    `Reviewed - Other` and nothing is sent. Only `tracking.REVIEWABLE`
    emails qualify, so saving twice or after a refresh records nothing more.
    Returns the status recorded and the review (intent, connotes, references).
    """
    current = next((event for event in tracking.latest() if event["entry_id"] == entry["entry_id"]), None)
    if current is None or current["status"] not in tracking.REVIEWABLE:
        status = current["status"] if current else "unknown"
        raise ValueError(f"This email is no longer waiting for a review (now {status})")
    _withdraw_cards(current["state"])  # answered here first: the card's answer is not read (D-048)
    review = {
        "intent": intent,
        "connotes": split_numbers(connotes),
        "references": split_numbers(references),
        "reviewed_at": _now(),
    }
    if intent == "OTHER":
        tracking.record(current, tracking.REVIEWED_OTHER, reason=f"reviewed in {via}: not a POD request",
                        state={**current["state"], "review": review})
        return tracking.REVIEWED_OTHER, review
    if intent != "POD_REQUEST":
        raise ValueError(f"Intent must be POD_REQUEST or OTHER, not {intent!r}")
    numbers = review["connotes"] + [n for n in review["references"] if n not in review["connotes"]]
    check_numbers(numbers)
    # A number Power BI did not know, saved unchanged, is confirmed; a changed one is checked again (product owner, D-045).
    state = current["state"]
    # Shown as unknown: on a correction card only those asked; in Manual Review its numbers, the reason naming them.
    shown = state.get("correction_numbers", []) if current["status"] != tracking.MANUAL_REVIEW else \
        [n for n in str(current.get("numbers") or "").split(";") if n]
    confirmed = set(state.get("review", {}).get("confirmed", [])) | (set(shown) & set(state.get("unknown", [])))
    review["confirmed"] = [number for number in numbers if number in confirmed]
    tracking.record(current, tracking.REVIEWED, numbers=numbers, intent="POD_REQUEST",
                    reason=f"reviewed in {via}: POD request" + ("" if numbers else ", numbers from its attachments"),
                    state={**current["state"], "review": review})
    return tracking.REVIEWED, review


def _describe(email: dict) -> str:
    return (
        f"POD request from {email.get('sender_email', '')}, "
        f"subject \"{email.get('subject', '')}\", received {email.get('received_time', '')}"
    )


def _hold(email: dict, state: dict, reason: str, counters: Counter, details: str) -> None:
    """Hold the whole email for the team and tell them in Teams (D-006).

    When the numbers are what is wrong, the message is a card to correct
    them in Teams; `_resume_held` reads the answer (D-036).
    """
    rid = handoff.request_id(str(email["entry_id"]))
    message = f"{_describe(email)}.\nHeld for the team: {reason}.\n{details}\nThe email is left unread in the POD mailbox."
    if reason in CORRECTABLE_HOLDS:
        # Only the numbers not found are asked; the others are kept (product owner, D-045).
        asked = state.get("unknown") if reason == UNKNOWN_NUMBER and state.get("unknown") else \
            [n for n in str(email.get("numbers") or "").split(";") if n]
        message += ("\nCorrect the number(s) below, keep them if they are right, or clear them if they are "
                    "not POD numbers, and save: the agent replies with every POD found and says which "
                    "number is not in our system. Or correct them in the dashboard Review queue.")
        state = {
            **state,
            "correction_card": _send_correction_card(email, message, asked),
            "correction_message": message,  # to send the card again (D-037)
            "correction_numbers": asked,
            "card_delivery": {**state.get("card_delivery", {}), "correction": {"sent_at": _now(), "resends": 0}},
        }
    else:
        handoff.write_teams(rid, "info", message)
    tracking.record(email, tracking.HELD, reason=reason, state=state)
    counters["held"] += 1


def _to_manual_review(email: dict, state: dict, reason: str, counters: Counter) -> None:
    """Queue an email for a person: the dashboard Review queue, and a Teams card to review it there too (product owner, D-048).

    Cold start does not come here: every email with a number would post a card.
    """
    snippet = " ".join(str(email.get("body") or "").split())
    message = (
        f"Manual Review: email from {email.get('sender_email', '')}, subject \"{email.get('subject', '')}\", "
        f"received {email.get('received_time', '')}.\nWhy: {reason}.\n"
        f"Email: \"{snippet[:REVIEW_SNIPPET_CHARS]}{'...' if len(snippet) > REVIEW_SNIPPET_CHARS else ''}\"\n"
        "POD request: fill in its connote and reference numbers (none: the agent reads its attachments). "
        "Not a POD request: nothing is sent. Or review it in the dashboard Review queue."
    )
    numbers = [n for n in str(email.get("numbers") or "").split(";") if n]
    state = {
        **state,
        "review_card": _send_review_card(email, message, numbers),
        "review_message": message,  # to send the card again (D-037)
        "card_delivery": {**state.get("card_delivery", {}), "review": {"sent_at": _now(), "resends": 0}},
    }
    tracking.record(email, tracking.MANUAL_REVIEW, reason=reason, state=state)
    counters["manual_review"] += 1


def _send_review_card(email: dict, message: str, numbers: list[str]) -> str:
    """Post a Manual Review email's review card, its connotes filled in with `numbers`; returns its id."""
    # A new id each time: an answer to an older card is never read as this one's.
    card_id = f"{handoff.request_id(str(email['entry_id']))}-m{len(tracking.history(str(email['entry_id'])))}"
    handoff.write_teams(card_id, "choice", message, card=handoff.review_card(message, "; ".join(numbers)))
    return card_id


def _oversize_details(email: dict, found: dict, not_received: dict, attachments: list[Path]) -> str:
    """For the Teams message of an email held for its size: each number's status, and where its files are (D-038).

    The files are copied to POD_MANUAL_UPLOAD_DIR, one folder per email, for
    the team to send by hand; `_clean_manual_uploads` removes the folder once
    the email is no longer held.
    """
    status_column = os.getenv(config.PBI_STATUS_COLUMN_SETTING, "").strip()
    lines = ["Requested:"]
    for number in [n for n in str(email.get("numbers") or "").split(";") if n]:
        if number in found:
            paths = [Path(path) for path in ([found[number]] if isinstance(found[number], str) else found[number])]
            files = ", ".join(f"{path.name}, {path.stat().st_size / 1_000_000:.1f} MB" for path in paths)
            lines.append(f"- {number}: scan found ({files})")
        elif number in not_received:
            status = str(not_received[number].get(status_column) or "").strip() if status_column else ""
            lines.append(f"- {number}: not received" + (f" (Power BI status: {status})" if status else ""))
        else:
            lines.append(f"- {number}: not in Power BI")
    folder = config.manual_upload_dir()
    if folder is None:
        return "\n".join(lines)
    target = folder / handoff.request_id(str(email["entry_id"]))
    try:
        target.mkdir(parents=True, exist_ok=True)
        for position, path in enumerate(attachments):
            copy = target / path.name
            if copy.exists() and copy.stat().st_size != path.stat().st_size:
                copy = target / f"{position + 1} {path.name}"  # two scans with one name
            shutil.copy2(path, copy)
    except OSError as error:
        # Still held: the team finds the files from the list above.
        return "\n".join([*lines, f"The files could not be copied for sending by hand ({error})."])
    url = os.getenv(config.MANUAL_UPLOAD_URL_SETTING, "").strip()
    where = f"{url.rstrip('/')}/{target.name}".replace(" ", "%20") if url else str(target)
    return "\n".join([*lines, f"The files, to attach by hand: {where}"])


def _clean_manual_uploads(counters: Counter) -> None:
    """Delete the copies of an email's files once nobody needs them (product owner, D-038, D-050).

    Kept while its draft without attachments is still a draft (the pilot
    measure has not seen it sent or deleted), and for an email held for its
    size before D-050 until it is no longer held.
    """
    folder = config.manual_upload_dir()
    if folder is None or not folder.exists():
        return
    by_request = {handoff.request_id(str(event["entry_id"])): event for event in tracking.latest()}
    for child in folder.iterdir():
        # Only the folders the agent made: one per email, named by its request id.
        if not child.is_dir() or not re.fullmatch(r"[0-9a-f]{16}", child.name):
            continue
        event = by_request.get(child.name)
        if event and event["status"] == tracking.HELD and str(event["reason"] or "").startswith(OVERSIZE):
            continue
        if event and event["status"] == tracking.REPLIED and event["state"].get("attach_by_hand") \
                and "pilot" not in event["state"]:
            continue  # its draft is waiting for the files
        try:
            # A synced file may be read-only: made writable, then removed (D-059).
            shutil.rmtree(child, onexc=lambda remove, path, _: (os.chmod(path, stat.S_IWRITE), remove(path)))
        except OSError as error:
            # In use or still syncing: the next run tries again, and the other folders go on.
            counters[f"files to send by hand not removed ({child.name}: {type(error).__name__})"] += 1
            continue
        counters["files to send by hand removed"] += 1


def _pbi_request_id(email: dict, state: dict) -> str:
    """The id of an email's current Power BI request; emails waiting from before ids were numbered use the plain one."""
    return state.get("pbi_request") or handoff.request_id(str(email["entry_id"]))


def _ask_power_bi(email: dict, state: dict, numbers: list[str], missing: list[str]) -> dict:
    """Write a Power BI request under a new id and return the state that remembers it (D-040).

    Each request of an email has its own id, which Flow 1 names its result
    after, so a late answer to an earlier request is never read as this one's.
    """
    request = f"{handoff.request_id(str(email['entry_id']))}-p{len(tracking.history(str(email['entry_id'])))}"
    handoff.write_pbi_request(request, str(email["entry_id"]), numbers, missing)
    # Watched as a Teams card is: no answer in 30 minutes, it is written again (D-049).
    delivery = {**state.get("card_delivery", {}), "power_bi": {"sent_at": _now(), "resends": 0}}
    return {**state, "pbi_request": request, "card_delivery": delivery}


def _check_pbi_answer(event: dict, state: dict, request: str) -> bool:
    """Write a Power BI request again that has had no answer for 30 minutes, at most 3 times (D-049).

    Flow 1 leaves its request file in place, so an unanswered request looks
    the same whether Flow 1 never ran or failed on the way. After the last
    resend the dashboard asks a person to check Flow 1 and Reprocess in
    Power BI. `state` is a copy. Returns whether anything changed.
    """
    delivery = dict(state.get("card_delivery", {}).get("power_bi") or {"sent_at": event["at_utc"], "resends": 0})
    if delivery.get("failed") or datetime.now(timezone.utc) - datetime.fromisoformat(delivery["sent_at"]) < CARD_RESEND_AFTER:
        return False
    if delivery["resends"] >= CARD_RESENDS:
        delivery["failed"] = "Flow 1 has not answered the Power BI request"
        state["card_delivery"] = {**state.get("card_delivery", {}), "power_bi": delivery}
        return True
    handoff.archive(f"{request}.json", ("pbi_requests",))
    numbers = [number for number in str(event.get("numbers") or "").split(";") if number]
    state.update(_ask_power_bi(event, state, numbers, state.get("pbi_asked") or state["missing"]))
    state["card_delivery"]["power_bi"]["resends"] = delivery["resends"] + 1
    return True


def _send_correction_card(email: dict, message: str, numbers: list[str]) -> str:
    """Post a held email's number correction card, filled in with `numbers`; returns its id."""
    # A new id each time: an answer to an older card is never read as this one's.
    card_id = f"{handoff.request_id(str(email['entry_id']))}-h{len(tracking.history(str(email['entry_id'])))}"
    handoff.write_teams(card_id, "choice", message, card=handoff.correction_card(message, "; ".join(numbers)))
    return card_id


def _copy_cards(state: dict) -> dict:
    """`state` with its card records copied, so they can change without touching the event read."""
    state["cards"] = dict(state.get("cards", {}))
    state["team_card_history"] = {number: list(ids) for number, ids in state.get("team_card_history", {}).items()}
    state["team_base_cards"] = dict(state.get("team_base_cards", {}))
    state["card_delivery"] = {slot: dict(record) for slot, record in state.get("card_delivery", {}).items()}
    return state


def _resend_card(event: dict, state: dict, slot: str) -> str:
    """Post a card of this email again under a new id: "correction", or the file choice for a number."""
    if slot == "correction":
        message = state.get("correction_message") or f"{_describe(event)}.\nHeld for the team: {event['reason']}."
        asked = state.get("correction_numbers") or [n for n in str(event.get("numbers") or "").split(";") if n]
        state["correction_card"] = _send_correction_card(event, message, asked)
        return state["correction_card"]
    if slot == "review":
        numbers = [n for n in str(event.get("numbers") or "").split(";") if n]
        state["review_card"] = _send_review_card(event, state["review_message"], numbers)
        return state["review_card"]
    cards, history = state["cards"], state["team_card_history"]
    history.setdefault(slot, [cards[slot]])
    base = state["team_base_cards"].setdefault(slot, _team_card_base_id(cards[slot]))
    retry_id = f"{base}-r{len(history[slot])}"
    files = state["ambiguous"][slot]
    handoff.write_teams(retry_id, "choice", _file_choice_message(event, slot, files, state), _file_choice_options(files))
    cards[slot] = retry_id
    history[slot].append(retry_id)
    return retry_id


def _check_delivery(event: dict, state: dict, slot: str, card_id: str) -> bool:
    """Send again a Teams card that has not reached Teams 30 minutes after it was sent (D-037).

    Not reached: Flow 3 has not taken its file from teams/, or Teams refused
    it (an error result). The old file goes to the archive, so files never
    pile up in teams/. After `CARD_RESENDS` resends the card is marked failed
    for the dashboard instead. `state` is a `_copy_cards` copy. Returns
    whether anything changed.
    """
    # A card sent before delivery was recorded counts from its email's last event.
    delivery = state["card_delivery"].setdefault(slot, {"sent_at": event["at_utc"], "resends": 0})
    if delivery.get("failed") or not (delivery.get("error") or handoff.teams_request_pending(card_id)):
        return False  # failed already, or in Teams waiting for an answer
    if datetime.now(timezone.utc) - datetime.fromisoformat(delivery["sent_at"]) < CARD_RESEND_AFTER:
        return False
    handoff.archive(f"{card_id}.json", ("teams",))
    if delivery["resends"] >= CARD_RESENDS:
        delivery["failed"] = delivery.get("error") or "Flow 3 did not take the card from the teams folder"
        return True
    delivery.update(sent_at=_now(), resends=delivery["resends"] + 1, error="")
    _resend_card(event, state, slot)
    return True


def _read_card_answer(state: dict, slot: str, card_ids: list[str]) -> tuple[dict | None, bool]:
    """The newest answer to any of these cards, and whether Flow 3 reported that one could not be posted.

    An error for the current card (the last id) is kept in its delivery record.
    """
    reported = False
    for card_id in reversed(card_ids):
        answer = handoff.read_teams_result(card_id)
        if answer is None:
            continue
        handoff.archive(f"{card_id}.json")
        if not answer["error"]:
            return answer, reported
        reported = True
        if card_id == card_ids[-1]:
            state["card_delivery"].setdefault(slot, {"sent_at": _now(), "resends": 0})["error"] = answer["error"]
    return None, reported


def send_cards_again(entries: list[dict]) -> int:
    """Post again the Teams cards that never reached Teams, once Flow 3 is fixed (dashboard, D-037).

    Each gets a new id and three more resends. Holds the run lock; raises
    AlreadyRunning while a cycle runs. Returns how many cards were posted.
    """
    with _run_lock():
        latest = {event["entry_id"]: event for event in tracking.latest()}
        posted = 0
        for entry in entries:
            event = latest.get(entry["entry_id"])
            problems = tracking.card_problems(event["status"], event["state"]) if event else {}
            problems.pop("power_bi", None)  # a Power BI request is written again by Reprocess in Power BI
            if not problems:
                raise ValueError("This email has no Teams card that failed to reach Teams")
            state = _copy_cards(dict(event["state"]))
            for slot in problems:
                state["card_delivery"][slot] = {"sent_at": _now(), "resends": 0}
                _resend_card(event, state, slot)
                posted += 1
            tracking.record(event, event["status"], reason=event["reason"], state=state)
    return posted


def _sweep_handoff(counters: Counter) -> None:
    """Archive handoff files nothing waits for any more, once left 30 minutes, so none pile up (product owner).

    Teams requests no open card needs, and Power BI results for an email no
    longer waiting, such as one answered by hand meanwhile (D-039).
    """
    needed = {card for event in tracking.latest(tracking.WAITING_TEAM) for card in event["state"].get("cards", {}).values()}
    needed |= {event["state"]["correction_card"] for event in tracking.latest(tracking.HELD) if "correction_card" in event["state"]}
    needed |= {event["state"]["review_card"] for event in tracking.latest(tracking.MANUAL_REVIEW) if "review_card" in event["state"]}
    needed |= {_pbi_request_id(event, event["state"]) for event in tracking.latest(tracking.WAITING_PBI)}
    cutoff = time.time() - CARD_RESEND_AFTER.total_seconds()
    for name, what in (("teams", "Teams message not taken by Flow 3"), ("pbi_results", "Power BI result no email waits for")):
        folder = Path(config.HANDOFF_DIR) / name
        for path in folder.glob("*.json") if folder.exists() else []:
            if path.stem not in needed and path.stat().st_mtime < cutoff:
                handoff.archive(path.name, (name,))
                counters[f"{what}, archived"] += 1


# Emails a person may still be handling, looked at in Outlook each run (D-039, D-041):
# POD requests, which are done once in POD_FINISHED_FOLDER, and Other emails,
# which the team may file in any folder (product owner).
_POD_WAITING = (tracking.WAITING_PBI, tracking.WAITING_TEAM, tracking.HELD, tracking.REVIEWED, tracking.MANUAL_REVIEW)
_WATCHED = (*_POD_WAITING, tracking.OTHER_INTENT)
MOVED = "moved or deleted in Outlook"


def _withdraw(email: dict, state: dict) -> None:
    """Withdraw what the email still waits for: Teams cards Flow 3 has not taken, its Power BI request.

    A card already in Teams stays; an answer to it is ignored, as the email
    no longer waits for one.
    """
    _withdraw_cards(state)
    handoff.archive(f"{_pbi_request_id(email, state)}.json")  # its Power BI request and any result


def _withdraw_cards(state: dict) -> None:
    """Archive the email's Teams card files Flow 3 has not taken; a card already in Teams stays, its answer unread."""
    for card_id in [*state.get("cards", {}).values(), state.get("correction_card"), state.get("review_card")]:
        if card_id:
            handoff.archive(f"{card_id}.json", ("teams",))


def _resolve_manual(email: dict, state: dict, manual: dict, counters: Counter) -> None:
    """A person answered this email in Outlook: stop, with no draft and no POD Outstanding change (D-039)."""
    _withdraw(email, state)
    when = (f" on {datetime.fromisoformat(manual['at']).astimezone(config.LOCAL_TIME):%d %b %Y %H:%M} UTC+8"
            if manual.get("at") else "")
    tracking.record(email, tracking.RESOLVED, reason=f"replied manually in Outlook ({manual['action']}{when})",
                    state={**state, "manual_reply": manual})
    counters["resolved: replied manually in Outlook"] += 1


def _check_outlook(mailbox_folder, counters: Counter, *, progress: bool = False) -> None:
    """Stop for emails a person handled in Outlook while they waited: answered, moved or deleted (D-039, D-041).

    Answered with Reply or Reply All, or moved to POD_FINISHED_FOLDER:
    Resolved. A POD request moved anywhere else, or deleted, is Held for the
    team; an Other email moved anywhere is Resolved. A moved email gets a new
    entry ID, so it is looked for in the finished folder by its Internet
    Message ID.
    """
    watched = [event for event in tracking.latest() if event["status"] in _WATCHED]
    if not watched:
        return
    store_id = str(mailbox_folder.Store.StoreID)
    home = str(mailbox_folder.FolderPath)
    for event in watched:
        subject = event.get("subject") or event["entry_id"]
        try:
            found = outlook_reader.email_status(str(event["entry_id"]), store_id)
            if found and found["manual"]:
                _resolve_manual(event, dict(event["state"]), found["manual"], counters)
                _progress(progress, f"Answered by hand in Outlook, resolved: {subject}")
                continue
            folder = found["folder"] if found else None
            if folder is not None and str(folder.FolderPath) == home:
                continue  # where it arrived, not answered: still waiting
            finished = (
                str(folder.Name) == config.FINISHED_FOLDER and str(folder.Parent.FolderPath) == home
                if folder is not None
                else outlook_reader.in_child_folder(mailbox_folder, config.FINISHED_FOLDER, event.get("message_id") or "")
            )
        except Exception as error:
            _progress(progress, f"Could not check {subject} in Outlook: {error}")
            continue
        where = config.FINISHED_FOLDER if finished else (str(folder.Name) if folder is not None else "")
        reason = f"moved to {where} in Outlook" if where else MOVED
        # Its Teams cards are dropped too, or a correction card would still be posted again.
        state = {key: value for key, value in event["state"].items()
                 if key not in ("cards", "correction_card", "review_card")}
        if finished or event["status"] == tracking.OTHER_INTENT:
            _withdraw(event, event["state"])
            tracking.record(event, tracking.RESOLVED, reason=reason, state=state)
            counters["resolved: handled in Outlook"] += 1
            _progress(progress, f"Resolved, {reason}: {subject}")
        elif not (event["status"] == tracking.HELD and str(event["reason"]).startswith("moved")):
            _withdraw(event, event["state"])
            tracking.record(event, tracking.HELD, reason=reason, state=state)  # the team: Mark resolved or Process again
            counters["held: moved or deleted in Outlook"] += 1
            _progress(progress, f"Held, {reason}: {subject}")


def _reply(email: dict, state: dict, counters: Counter, *, progress: bool = False) -> None:
    """Draft or send the one reply, mark the email read, update Outstanding."""
    # The POD mailbox's own store: the email is opened there and its draft kept there (D-032).
    store_id = outlook_reader.mailbox_store_id(config.OUTLOOK_MAILBOX_NAME)
    # Last look before replying: a person may have answered it meanwhile (D-039).
    # If Outlook cannot tell, this raises and nothing is sent.
    manual = outlook_reader.replied_manually(str(email["entry_id"]), store_id)
    if manual:
        _resolve_manual(email, state, manual, counters)
        _progress(progress, "Already answered in Outlook by a person; no reply created.")
        return
    found = dict(state.get("found", {}))
    for number, choice in state.get("chosen", {}).items():
        found[number] = choice
    # One file can be two numbers' scan ("7000003-80600001 - Signed"): attached once.
    attachments = list(dict.fromkeys(
        Path(path) for paths in found.values() for path in ([paths] if isinstance(paths, str) else paths)))
    not_received = state.get("not_received", {})

    outside = [path for path in attachments if not pod_files.is_inside(path, config.SCAN_FOLDERS)]
    if outside:
        # Never attach a file from outside the scan folders (HANDBOOK security).
        raise ValueError(f"Attachment outside the scan folders: {outside[0]}")
    excluded = [
        path for path in attachments
        if pod_files.is_excluded(path, config.SCAN_FOLDERS, config.SCAN_EXCLUDED_FOLDERS)
    ]
    if excluded:
        _hold(
            email,
            state,
            "an attachment is in a scan folder excluded by POD_SCAN_EXCLUDED_FOLDERS",
            counters,
            f"Files: {', '.join(path.name for path in excluded)}",
        )
        return

    size_mb = sum(path.stat().st_size for path in attachments) / 1_000_000
    # Too large to attach: a draft without them, whatever the send mode; the
    # team attaches the copies and sends it (product owner, D-050).
    oversize = size_mb > config.MAX_ATTACHMENT_MB
    details = _oversize_details(email, found, not_received, attachments) if oversize else ""
    mode = "draft" if oversize else config.reply_mode(email.get("sender_email", ""))

    requested_at = _now()
    rid = handoff.request_id(str(email["entry_id"]))
    # Build the Outstanding rows first: a wrong column map fails before any reply.
    rows = {
        number: handoff.outstanding_row(number, pbi_row, email, requested_at)
        for number, pbi_row in not_received.items()
    }
    # Confirmed by a person and still unknown to Power BI: in the reply, and kept in POD Outstanding (D-045).
    not_in_system = list(state.get("not_in_system", []))
    rows.update({number: handoff.outstanding_row(number, None, email, requested_at) for number in not_in_system})
    same_as = state.get("same_as", {})
    label = lambda number: number + (
        f" (your reference {', '.join(ref for ref, other in same_as.items() if other == number)})"
        if number in same_as.values() else ""
    )
    body = reply_generator.build_reply(
        [label(number) for number in found],
        [label(number) for number in not_received],
        not_in_system,
        include_notice=mode == "send",
    )
    drafted_at = _now()
    _progress(progress, f"Creating {mode} Reply All with {0 if oversize else len(attachments)} attachment(s)...")
    sent = outlook_reader.reply(
        str(email["entry_id"]),
        body,
        [] if oversize else attachments,
        mode,
        send_as=config.POD_SHARED_MAILBOX_ADDRESS,
        store_id=store_id,
    )
    _progress(progress, "Reply created; updating the email and POD Outstanding...")
    outlook_reader.mark_read(str(email["entry_id"]), store_id)
    for number in found:
        handoff.write_outstanding(rid, "remove", number)
    if not_received:
        connote_column = config.required(config.PBI_CONNOTE_COLUMN_SETTING)
        reference_column = config.required(config.PBI_REFERENCE_COLUMN_SETTING)
    for number, pbi_row in not_received.items():
        handoff.write_outstanding(
            rid,
            "add",
            number,
            connote=str(pbi_row.get(connote_column, "")),
            reference=str(pbi_row.get(reference_column, "")),
            row=rows[number],
        )
    for number in not_in_system:  # added once already when held; Flow 2 adds only if absent
        handoff.write_outstanding(rid, "add", number, connote=number, row=rows[number])
    state = {**state, "reply": body, "attachments": [str(path) for path in attachments], **sent}
    if not_received or not_in_system:
        state["outstanding_add_numbers"] = [*not_received, *not_in_system]
    if "draft_entry_id" in sent:
        state["drafted_at"] = drafted_at  # for the pilot measure (D-014)
    else:
        state["sent_at"] = _now()  # sent now; a draft gets its time once it is sent
    reason = mode
    if oversize:
        # Its files are attached by hand, so the pilot measure leaves it out (D-050).
        state["attach_by_hand"] = True
        reason = f"draft without its attachments ({size_mb:.1f} MB, over the {config.MAX_ATTACHMENT_MB:g} MB limit)"
        handoff.write_teams(rid, "info", (
            f"{_describe(email)}.\nIts attachments total {size_mb:.1f} MB, over the {config.MAX_ATTACHMENT_MB:g} MB "
            "limit: a Reply All draft without them is in the POD mailbox's Drafts, and POD Outstanding is "
            f"updated. Attach the files and send it.\n{details}"))
    tracking.record(email, tracking.REPLIED, reason=reason, state=state)
    counters["replied"] += 1


def _check_drafts(mailbox_folder, counters: Counter) -> None:
    """Record what became of each draft not yet decided (pilot measure, D-014).

    The status stays Replied; the reason and state say sent with the same
    PODs, sent with PODs changed, or deleted (D-053). Drafts still waiting
    are looked at again next run.
    """
    waiting = [
        event for event in tracking.latest(tracking.REPLIED)
        if "draft_entry_id" in event["state"] and "pilot" not in event["state"]
    ]
    if not waiting:
        return
    since = min(event["state"]["drafted_at"] for event in waiting)
    sent_items = outlook_reader.sent_replies_since(mailbox_folder, since)
    for event in waiting:
        state = event["state"]
        result = pilot.outcome(
            state,
            sent_items,
            outlook_reader.draft_exists(
                state["draft_entry_id"], state.get("draft_store_id", "")
            ),
        )
        if result is None:
            continue
        decided = {**state, "pilot": result}
        if result in pilot.SENT:
            # When it really went out: moving finished emails counts from it.
            decided["sent_at"] = pilot.first_reply(state, sent_items)["sent_on"]
        tracking.record(event, tracking.REPLIED, reason=f"draft {result}", state=decided)
        counters[f"pilot {result}"] += 1


def _check_email_categories(mailbox_folder, counters: Counter, *, progress: bool = False) -> None:
    """Add completion categories after a sent reply and successful Outstanding adds."""
    for event in tracking.latest():
        state = event["state"]
        categories = []
        updated_state = dict(state)
        if state.get("pilot") in pilot.SENT and not state.get(
            "category_pod_agent_replay"
        ):
            categories.append(_CATEGORY_AGENT_REPLY)
            updated_state["category_pod_agent_replay"] = True

        add_numbers = state.get("outstanding_add_numbers") or []
        if add_numbers and not state.get("category_on_list"):
            if handoff.outstanding_adds_archived(
                handoff.request_id(str(event["entry_id"])), add_numbers
            ):
                categories.append(_CATEGORY_ON_LIST)
                updated_state["category_on_list"] = True

        if not categories:
            continue
        try:
            store_id = str(mailbox_folder.Store.StoreID)
            entry_id = state.get("moved_entry_id") or str(event["entry_id"])
            try:
                added = outlook_reader.add_categories(entry_id, categories, store_id=store_id)
            except Exception:
                # Moved by hand after the reply: Outlook gave it a new entry ID (D-059).
                found = outlook_reader.find_by_message_id(
                    mailbox_folder, event.get("message_id") or "", config.FINISHED_FOLDER)
                if found is None or found["entry_id"] == entry_id:
                    raise
                updated_state["moved_entry_id"] = found["entry_id"]
                added = outlook_reader.add_categories(found["entry_id"], categories, store_id=store_id)
            tracking.record(
                event,
                event["status"],
                reason=event.get("reason", ""),
                state=updated_state,
            )
            for category in added:
                counters[f"category added ({category})"] += 1
            _progress(
                progress,
                f"Updated Outlook categories for {event.get('subject') or event['entry_id']}: "
                f"{', '.join(categories)}.",
            )
        except Exception as error:
            counters[f"category update failed ({type(error).__name__})"] += 1
            _progress(
                progress,
                f"Could not update Outlook categories for {event.get('subject') or event['entry_id']}: "
                f"{type(error).__name__}: {error}",
            )


def _move_finished(mailbox_folder, counters: Counter, *, progress: bool = False) -> None:
    """Move replied emails with every POD attached into Actioned/Finished (D-031).

    Only with the dashboard switch on, and only replies sent after it was
    switched on: a draft counts once it is sent. An email with a number not
    received stays, as the team still waits for that POD. Runs after the
    category check, since moving gives the email a new entry ID.
    """
    if not config.MOVE_FINISHED or not config.MOVE_FINISHED_SINCE:
        return
    since = datetime.fromisoformat(config.MOVE_FINISHED_SINCE)
    for event in tracking.latest(tracking.REPLIED):
        state = event["state"]
        if (
            not state.get("sent_at")
            or state.get("moved_to")
            or state.get("not_received")
            or state.get("outstanding_add_numbers")
            or datetime.fromisoformat(state["sent_at"]) < since
        ):
            continue
        entry_id = state.get("moved_entry_id") or str(event["entry_id"])
        try:
            try:
                moved_id = outlook_reader.move_to_child_folder(entry_id, mailbox_folder, config.FINISHED_FOLDER)
            except Exception:
                # Moved by hand meanwhile: found by its Internet Message ID (D-059).
                found = outlook_reader.find_by_message_id(
                    mailbox_folder, event.get("message_id") or "", config.FINISHED_FOLDER)
                if found is None or found["entry_id"] == entry_id:
                    raise
                moved_id = found["entry_id"] if found["in_finished"] else outlook_reader.move_to_child_folder(
                    found["entry_id"], mailbox_folder, config.FINISHED_FOLDER)
        except Exception as error:
            # Tried again next run; one email never stops the others.
            counters[f"move to {config.FINISHED_FOLDER} failed ({type(error).__name__}: {error})"] += 1
            _progress(progress, f"Could not move {event.get('subject') or event['entry_id']}: {error}")
            continue
        tracking.record(event, tracking.REPLIED, reason=f"moved to {config.FINISHED_FOLDER}",
                        state={**state, "moved_to": config.FINISHED_FOLDER, "moved_entry_id": moved_id})
        counters[f"moved to {config.FINISHED_FOLDER}"] += 1
        _progress(progress, f"Moved to {config.FINISHED_FOLDER}: {event.get('subject') or event['entry_id']}")


def _decide(email: dict, state: dict, counters: Counter, *, progress: bool = False) -> None:
    """With every number sorted: hold, ask the team, or reply."""
    if state.get("unknown") and state.get("intent_unsure"):
        # Not sure it is a POD request, and its numbers are nowhere: a person decides (product owner, D-044).
        _to_manual_review(email, state, f"intent below threshold; not on the share or in Power BI: "
                          f"{', '.join(state['unknown'])}", counters)
        _progress(progress, "Unsure POD request whose numbers are nowhere; queued for manual review.")
        return
    confirmed = set(state.get("review", {}).get("confirmed", []))
    if confirmed & set(state.get("unknown", [])):
        # A person confirmed these numbers and Power BI still does not know them:
        # the reply says so, and they are not asked about again (product owner, D-045).
        state = {**state, "not_in_system": [n for n in state["unknown"] if n in confirmed],
                 "unknown": [n for n in state["unknown"] if n not in confirmed]}
    if state.get("unknown"):
        # Some real POD numbers are not in Power BI. Record them as
        # "Not in The System" for the team to check, and hold the email (D-023).
        requested_at = _now()
        add_rows = dict(state.get("not_received", {}))
        if not state.get("number_source"):  # numbers read from attachments may be anything (D-044)
            add_rows.update({number: None for number in state["unknown"]})
        rows = {
            number: handoff.outstanding_row(number, pbi_row, email, requested_at)
            for number, pbi_row in add_rows.items()
        }
        rid = handoff.request_id(str(email["entry_id"]))
        matched_rows = [row for row in add_rows.values() if row is not None]
        connote_column = config.required(config.PBI_CONNOTE_COLUMN_SETTING) if matched_rows else ""
        reference_column = config.required(config.PBI_REFERENCE_COLUMN_SETTING) if matched_rows else ""
        for number, pbi_row in add_rows.items():
            handoff.write_outstanding(
                rid,
                "add",
                number,
                connote=str(pbi_row.get(connote_column, "")) if pbi_row else number,
                reference=str(pbi_row.get(reference_column, "")) if pbi_row else "",
                row=rows[number],
            )
        _hold(
            email,
            {**state, "outstanding_add_numbers": list(add_rows)},
            UNKNOWN_NUMBER,
            counters,
            (f"Not in Power BI: {', '.join(state['unknown'])}, read from the {state['number_source']} "
             "(the email itself has no number). Correct the numbers if you find the right one. "
             if state.get("number_source") else
             f"Not in Power BI: {', '.join(state['unknown'])}. Added to POD Outstanding as "
             f"'{handoff.NOT_IN_SYSTEM}': please check; if it is not a POD number, take it out of the "
             "numbers and the agent removes that row and replies with the rest. ") +
            f"Found: {', '.join(state.get('found', {})) or 'none'}. "
            f"Not received: {', '.join(state.get('not_received', {})) or 'none'}."
            + (" Its attachments were read too: no number in them is known." if state.get("attachments_tried") else ""),
        )
        _progress(progress, f"Held for team review: number(s) not found in Power BI: {', '.join(state['unknown'])}.")
        return
    unresolved = [
        number for number in state.get("ambiguous", {}) if number not in state.get("chosen", {})
    ]
    if unresolved:
        rid = handoff.request_id(str(email["entry_id"]))
        cards = dict(state.get("cards", {}))
        card_history = {
            number: list(card_ids)
            for number, card_ids in state.get("team_card_history", {}).items()
        }
        base_cards = dict(state.get("team_base_cards", {}))
        deliveries = {slot: dict(record) for slot, record in state.get("card_delivery", {}).items()}
        for position, number in enumerate(unresolved):
            if number in cards:
                card_history.setdefault(number, [cards[number]])
                base_cards.setdefault(number, _team_card_base_id(cards[number]))
                continue
            card_id = f"{rid}-{len(cards) + position}"
            files = state["ambiguous"][number]
            options = _file_choice_options(files)
            handoff.write_teams(
                card_id,
                "choice",
                _file_choice_message(email, number, files, state),
                options,
            )
            cards[number] = card_id
            card_history[number] = [card_id]
            base_cards[number] = card_id
            deliveries[number] = {"sent_at": _now(), "resends": 0}
        tracking.record(
            email,
            tracking.WAITING_TEAM,
            state={
                **state,
                "cards": cards,
                "team_card_history": card_history,
                "team_base_cards": base_cards,
                "card_delivery": deliveries,
            },
        )
        counters["waiting_team"] += 1
        _progress(progress, f"Waiting for team file choice for: {', '.join(unresolved)}.")
        return
    _reply(email, state, counters, progress=progress)


def _team_card_base_id(card_id: str) -> str:
    """Remove a retry suffix from a card ID while retaining its original number suffix."""
    return re.sub(r"-r\d+$", "", card_id)


def _file_choice_options(files: list[str]) -> list[dict]:
    return [
        {
            "id": f"file:{index}",
            "label": f"{Path(path).name} | {path}",
        }
        for index, path in enumerate(files)
    ] + [
        {"id": "all", "label": "All files"},
        {"id": "not_all", "label": "Not all"},
        {"id": "manual", "label": "Handle manually"},
    ]


def _file_choice_message(email: dict, number: str, files: list[str], state: dict) -> str:
    detail = (
        f"Number {number} is not in the {state['outside_folder'][number]} folder; "
        f"it matches {len(files)} files elsewhere. Should one be sent?"
        if number in state.get("outside_folder", {})
        else f"Number {number} matches {len(files)} files. Which should be sent?"
    )
    if number in state.get("pbi_no_scan", []):
        detail += " Power BI does not name its scan."  # it was asked first (D-044)
    return f"{_describe(email)}.\n{detail}"


def _process_new_email(
    email: dict,
    model,
    number_model,
    cold_start: bool,
    get_index,
    counters: Counter,
    *,
    progress: bool = False,
) -> None:
    """Guard, intent, then the numbers: in the text, else in the attachments (product owner, D-044), then the scans.

    As a person reads it (product owner). Not a POD request, surely: done (Other
    Intent with a number, Skipped without). Not a POD request, unsurely: any
    number means Manual Review, none means done. A POD request, surely or
    not: its numbers go through the usual checks; with none anywhere, Manual
    Review. When an unsure POD request's numbers are not found, it goes to
    Manual Review instead of the Teams correction card (`_decide`). No intent
    model in use: Manual Review with a number in the text, Skipped without.
    """
    reason = skip_reason(email)
    if reason:
        tracking.record(email, tracking.SKIPPED_GUARD, reason=reason)
        counters["skipped"] += 1
        return

    confidence = 0.0
    if not cold_start:
        _progress(progress, "Classifying whether this is a POD request...")
        intent_text = classifier.build_email_text(
            subject=email.get("subject", ""),
            sender_email=email.get("sender_email", ""),
            to_recipients=email.get("to_recipients", ""),
            body=email.get("body", ""),
        )
        intent, confidence = classifier.predict(model, intent_text)
        email["intent"], email["confidence"] = intent, confidence
        _progress(progress, f"Classification: {intent} ({confidence:.0%} confidence).")

    _progress(progress, "Extracting connote/reference numbers...")
    unclear: list[str] = []
    numbers = number_classifier.extract_numbers(
        number_model,
        email.get("subject", ""),
        email.get("body", ""),
        config.NUMBER_CONFIDENCE_THRESHOLD,
        full_body=email.get("full_body", ""),
        problems=unclear,
    )
    email["numbers"] = ";".join(numbers)
    if cold_start:
        if numbers:
            tracking.record(email, tracking.MANUAL_REVIEW, reason="cold start: no intent model in use")
            counters["manual_review"] += 1
        else:
            tracking.record(email, tracking.SKIPPED_NO_NUMBER)
            counters["skipped"] += 1
        _progress(progress, "No active intent model.")
        return
    sure = confidence >= config.CONFIDENCE_THRESHOLD
    if email["intent"] != "POD_REQUEST":
        if sure:
            tracking.record(email, tracking.OTHER_INTENT if numbers else tracking.SKIPPED_NO_NUMBER)
            counters["other_intent" if numbers else "skipped"] += 1
            _progress(progress, "Not a POD request; no reply created.")
            return
        # Unsure it is not a POD request: a number anywhere means a person looks (product owner).
        if not numbers:
            found = _numbers_from_attachments(email, number_model, counters, record=False, progress=progress)
            numbers = found[0] if found else []
            email["numbers"] = ";".join(numbers)
        if numbers:
            _to_manual_review(email, {}, "intent below threshold: maybe not a POD request", counters)
        else:
            tracking.record(email, tracking.SKIPPED_NO_NUMBER)
            counters["skipped"] += 1
        return
    if unclear:
        # Replying for part of a list would leave the rest unanswered (D-034).
        _hold(email, {}, UNREADABLE_LIST, counters,
              f"Could not read: {', '.join(unclear)}. Numbers read: {', '.join(numbers) or 'none'}.")
        _progress(progress, f"Held: number list could not be read ({', '.join(unclear)}).")
        return
    # An unsure POD request goes on like any other; only its end differs (`_decide`).
    state: dict = {} if sure else {"intent_unsure": True}
    if not numbers:
        from_attachments = _numbers_from_attachments(email, number_model, counters, progress=progress)
        if from_attachments is None:
            return
        numbers, state = from_attachments[0], {**state, **from_attachments[1]}
        email["numbers"] = ";".join(numbers)
    _progress(progress, f"Found number(s): {', '.join(numbers)}.")
    _search(email, numbers, get_index, counters, state=state, progress=progress)


def _numbers_from_attachments(email: dict, number_model, counters: Counter, *, record: bool = True,
                              progress: bool = False):
    """(numbers, state) from a POD request's attachments, or None once the email is recorded otherwise (D-044).

    The first source with numbers is used; the rest wait in the state, for
    when none of its numbers is on the share or in Power BI. With `record`
    off, nothing is recorded: None only says no number was found.
    """
    _progress(progress, "No number in the text: reading the attachments...")
    try:
        found = attachments.read(
            str(email["entry_id"]), outlook_reader.mailbox_store_id(config.OUTLOOK_MAILBOX_NAME), number_model
        )
    except Exception as error:
        if record:
            _to_manual_review(email, email.get("state") or {},
                              f"POD request; its attachments could not be read ({type(error).__name__}: {error})", counters)
        return None
    if not found["sources"]:
        if not record:
            return None
        if found["not_read"]:
            _hold(email, {}, ATTACHMENTS_NOT_READ, counters,
                  f"Not read: {'; '.join(found['not_read'])}. Please check them by hand.")
        else:
            _to_manual_review(email, email.get("state") or {}, "POD request without a number", counters)
        _progress(progress, "No number in the attachments either.")
        return None
    first, *rest = found["sources"]
    _progress(progress, f"Numbers from the {first['source']}: {', '.join(first['numbers'])}.")
    return first["numbers"], {"number_source": first["source"], "number_sources_left": rest}


def _search(
    email: dict,
    numbers: list[str],
    get_index,
    counters: Counter,
    state: dict | None = None,
    *,
    progress: bool = False,
) -> None:
    """Look up scans, then ask Power BI about all numbers if any lack a scan."""
    folder = config.sender_folder(email.get("sender_email", ""))
    _progress(progress, "Searching scan files...")
    result = pod_files.search(get_index(), numbers, folder)
    _progress(
        progress,
        f"Scan lookup: {len(result.found)} found, {len(result.missing)} missing, "
        f"{len(result.ambiguous)} ambiguous.",
    )
    state = {
        **(state or {}),
        **({"outside_folder": {number: folder for number in result.outside_folder}} if result.outside_folder else {}),
        "found": {number: str(path) for number, path in result.found.items()},
        "ambiguous": {number: [str(path) for path in paths] for number, paths in result.ambiguous.items()},
        "missing": result.missing,
    }
    # Found and not ambiguous: done. Missing, or several files: Power BI first (product owner, D-044).
    asked = result.missing + list(result.ambiguous)
    if asked:
        state = _ask_power_bi(email, {**state, "pbi_asked": asked}, numbers, asked)
        tracking.record(email, tracking.WAITING_PBI, state=state)
        counters["waiting_pbi"] += 1
        _progress(progress, f"Created Power BI request for: {', '.join(asked)}.")
        return
    _decide(email, state, counters, progress=progress)


def _resume_reviewed(event: dict, get_index, counters: Counter, *, progress: bool = False) -> None:
    """Continue an email a person reviewed as a POD request, with their numbers.

    A held email may have had numbers added to POD Outstanding; those the
    person took out are not POD numbers, so Flow 2 removes their rows (D-035).
    """
    review = event["state"]["review"]
    numbers = review["connotes"] + [n for n in review["references"] if n not in review["connotes"]]
    rid = handoff.request_id(str(event["entry_id"]))
    for number in event["state"].get("outstanding_add_numbers") or []:
        if number not in numbers:
            handoff.write_outstanding(rid, "remove", number)
    email = {**event, "numbers": ";".join(numbers), "intent": "POD_REQUEST"}
    # Only the review is carried on: the earlier state was the model's view.
    state = {"review": review}
    if not numbers:
        # Reviewed as a POD request without numbers: they are read from its attachments (D-046).
        found = _numbers_from_attachments(email, model_registry.load_active("number"), counters, progress=progress)
        if found is None:
            return
        numbers, state = found[0], {**state, **found[1]}
        email["numbers"] = ";".join(numbers)
    _search(email, numbers, get_index, counters, state=state, progress=progress)


def _resume_held(event: dict, counters: Counter, *, progress: bool = False) -> None:
    """Apply the numbers corrected on a held email's Teams card, as a dashboard review would (D-036)."""
    state = _copy_cards(dict(event["state"]))
    card_id = state["correction_card"]
    answer, reported = _read_card_answer(state, "correction", [card_id])
    if answer is None:
        if _check_delivery(event, state, "correction", card_id) or reported:
            tracking.record(event, tracking.HELD, reason=event["reason"], state=state)
        return
    who = answer["responder"] or "the team"
    if answer["choice"] == "manual":
        state = {key: value for key, value in event["state"].items() if key != "correction_card"}
        tracking.record(event, tracking.HELD, reason=f"{who} chose to handle it manually", state=state)
        return
    if answer["choice"] != "correct":
        raise ValueError(f"Unknown Teams answer {answer['choice']!r} for a held email")
    asked = state.get("correction_numbers") or []
    kept = [number for number in str(event.get("numbers") or "").split(";") if number and number not in asked]
    typed = ";".join(n for n in [*kept, answer["connotes"].strip()] if n)  # a cleared card keeps the others
    try:
        if not split_numbers(typed):  # an empty card is not a review without numbers
            raise ValueError("A POD request needs at least one connote or reference number")
        _, review = _record_review(event, "POD_REQUEST", typed, "", via=f"Teams by {who}")
    except ValueError as error:
        # No usable numbers: still held, with a new card to answer.
        _hold(event, event["state"], event["reason"], counters, f"{who} saved no usable numbers ({error}).")
        return
    _progress(progress, f"Numbers corrected in Teams by {who}: {', '.join(review['connotes'])}.")
    try:
        # A correction is a manual label too, as from the dashboard (D-035).
        training_refresh.add_review(config.TRAINING_DATA_PATH, event, review)
    except Exception as error:
        counters[f"training data not updated ({type(error).__name__}: {error})"] += 1


def _resume_manual(event: dict, counters: Counter, *, progress: bool = False) -> None:
    """Apply the answer to a Manual Review email's Teams card, as a dashboard review would (D-048)."""
    state = _copy_cards(dict(event["state"]))
    card_id = state["review_card"]
    answer, reported = _read_card_answer(state, "review", [card_id])
    if answer is None:
        if _check_delivery(event, state, "review", card_id) or reported:
            tracking.record(event, tracking.MANUAL_REVIEW, reason=event["reason"], state=state)
        return
    who = answer["responder"] or "the team"
    without_card = {key: value for key, value in event["state"].items() if key != "review_card"}
    if answer["choice"] == "manual":
        tracking.record(event, tracking.MANUAL_REVIEW, reason=f"{who} chose to handle it manually", state=without_card)
        return
    if answer["choice"] not in ("pod", "other"):
        raise ValueError(f"Unknown Teams answer {answer['choice']!r} for a Manual Review email")
    if answer["references"] is None:
        # Never act on half an answer: a typed reference would be lost (D-048).
        tracking.record(event, tracking.MANUAL_REVIEW, state=without_card,
                        reason=f"{event['reason']}; the Teams answer was not used: Flow 3 does not pass the "
                               "references field yet (docs/POWER_AUTOMATE_FLOW.md); review it in the dashboard")
        return
    try:
        _, review = _record_review(
            event, "POD_REQUEST" if answer["choice"] == "pod" else "OTHER",
            answer["connotes"], answer["references"], via=f"Teams by {who}",
        )
    except ValueError as error:
        # A number that cannot be one: a new card says why.
        _to_manual_review(event, without_card, f"{event['reason']}; {who} saved {error}", counters)
        return
    _progress(progress, f"Reviewed in Teams by {who}: {review['intent']}.")
    try:
        training_refresh.add_review(config.TRAINING_DATA_PATH, event, review)
    except Exception as error:
        counters[f"training data not updated ({type(error).__name__}: {error})"] += 1


def _resume_pbi(event: dict, get_index, counters: Counter, *, progress: bool = False) -> None:
    """Settle each number Power BI was asked about, once it has answered its current request (D-044).

    A row by connote (DoNumber) first, else by reference; for a sender in
    POD_PBI_SENDER_CHECK only rows whose sender holds its word. The row's scan
    path naming an indexed PDF is the scan, whatever its status. Else a
    number the share had several files for goes to the team; any other has
    its row's connote looked for on the share, and with no scan is not
    received. A reference whose connote is another number of the email is
    that document: attached once, no question asked.
    """
    request = _pbi_request_id(event, event["state"])
    result = handoff.read_pbi_result(request)  # an answer to an older request has another name
    if result is None:
        state = dict(event["state"])
        if _check_pbi_answer(event, state, request):
            tracking.record(event, tracking.WAITING_PBI, reason=event["reason"], state=state)
        return
    handoff.archive(f"{request}.json")
    state = dict(event["state"])
    if "error" in result:
        _hold(event, state, "the Power BI lookup failed", counters, f"Error: {result['error']}")
        return
    asked = state.get("pbi_asked") or state["missing"]
    # After "Not all" only that number is asked again: the earlier answers for the others stay.
    earlier_not_received = {number: row for number, row in state.get("not_received", {}).items() if number not in asked}
    earlier_unknown = [number for number in state.get("unknown", []) if number not in asked]
    state["found"], state["ambiguous"] = dict(state.get("found", {})), dict(state.get("ambiguous", {}))
    outside_folder, same_as = dict(state.get("outside_folder", {})), dict(state.get("same_as", {}))
    no_scan_in_pbi = set(state.get("pbi_no_scan", []))
    not_received: dict[str, dict] = {}
    unknown: list[str] = []
    sender = event.get("sender_email", "")
    folder, word = config.sender_folder(sender), config.sender_check_word(sender)
    sender_column = config.required(config.PBI_SENDER_COLUMN_SETTING) if word else ""
    connote_column = config.required(config.PBI_CONNOTE_COLUMN_SETTING)
    status_column = os.getenv(config.PBI_STATUS_COLUMN_SETTING, "").strip()
    scan_column = os.getenv(config.PBI_SCAN_PATH_COLUMN_SETTING, "").strip()
    norm = lambda path: os.path.normcase(os.path.normpath(str(path)))
    skipped = {number: {norm(path) for path in paths} for number, paths in state.get("skipped_files", {}).items()}
    not_all = set(state.get("not_all_numbers", []))
    requested = [number for number in str(event.get("numbers") or "").split(";") if number]
    index_cache: list = []

    def index():
        # Built only when a row needs the share, once per answer.
        if not index_cache:
            index_cache.append(get_index())
        return index_cache[0]

    matches = handoff.matching_rows(asked, result["rows"])
    for number in asked:
        rows = matches[number]
        if word:
            # This customer's rows say so; another's is not what it asked for (product owner).
            rows = [row for row in rows if word in str(row.get(sender_column) or "").casefold()]
        consignments = {handoff.clean(str(row.get(connote_column) or "")) for row in rows}
        if len(consignments) > 1:
            # One reference, several consignments: the team says which, never one picked quietly.
            files = [str(path) for row in rows
                     if scan_column and (path := pod_files.indexed_path(index(), str(row.get(scan_column) or "")))]
            if len(set(files)) > 1:
                state["ambiguous"][number] = sorted(set(files))
                continue
        row = handoff.preferred_row(rows)
        if row is None:
            if number in state["ambiguous"]:
                no_scan_in_pbi.add(number)  # Power BI cannot tell: the team chooses
            else:
                unknown.append(number)
            continue
        status = str(row.get(status_column) or "").strip().casefold() if status_column else ""
        if number in not_all and status == "unsigned":
            # The team said none of its files is it, and Power BI agrees it is not signed.
            state["ambiguous"].pop(number, None)
            not_received[number] = row
            continue
        path = pod_files.indexed_path(index(), str(row.get(scan_column) or "")) if scan_column else None
        if path and norm(path) not in skipped.get(number, set()):
            # Power BI's own choice of scan, whatever its status, which may lag (product owner).
            state["found"][number] = str(path)
            state["ambiguous"].pop(number, None)
            continue
        if number in state["ambiguous"]:
            no_scan_in_pbi.add(number)
            continue
        connote = str(row.get(connote_column) or "").strip()
        if not handoff.same_number(connote, number):  # found by its reference
            other = next((other for other in requested if other != number and handoff.same_number(other, connote)), None)
            if other:
                same_as[number] = other  # the same document as another number of the email
                continue
        hit = pod_files.search(index(), [connote], folder) if connote else None
        files = []
        if hit:
            files = [hit.found[connote]] if connote in hit.found else hit.ambiguous.get(connote, [])
            files = [file for file in files if norm(file) not in skipped.get(number, set())]
        if hit and connote in hit.outside_folder and files:
            state["ambiguous"][number] = [str(file) for file in files]
            outside_folder[number] = folder
        elif len(files) == 1:
            state["found"][number] = str(files[0])
        elif files:
            state["ambiguous"][number] = [str(file) for file in files]
        else:
            not_received[number] = row
    state["not_received"] = {**earlier_not_received, **not_received}
    state["unknown"] = earlier_unknown + unknown
    state.update({key: value for key, value in (
        ("outside_folder", outside_folder), ("same_as", same_as), ("pbi_no_scan", sorted(no_scan_in_pbi))) if value})
    _progress(progress, f"Power BI answered for {len(asked)} number(s); {len(unknown)} not in Power BI.")
    settled = state["found"] or state["ambiguous"] or state["not_received"] or same_as or state.get("chosen")
    # Carried from one set of numbers to the next: what the intent and a review said, and the text's numbers.
    kept = {key: state[key] for key in ("intent_unsure", "review", "text_numbers", "text_unknown") if key in state}
    if state["unknown"] and not settled and not state.get("number_source") and not state.get("review"):
        # Every number in the text is not in Power BI: the right one may be in an attachment
        # (product owner, D-056). A person's numbers are not second-guessed (D-045).
        found = _numbers_from_attachments(event, model_registry.load_active("number"), counters,
                                          record=False, progress=progress)
        if found:
            numbers, sources = found
            _progress(progress, f"None of the text's numbers is in Power BI; trying the {sources['number_source']}.")
            text = {"text_numbers": requested, "text_unknown": list(state["unknown"])}
            _search({**event, "numbers": ";".join(numbers)}, numbers, get_index, counters,
                    state={**kept, **text, **sources}, progress=progress)
            return
        state["attachments_tried"] = True
    if state.get("number_source") and state["unknown"]:
        if settled:
            # Numbers read from attachments include others; drop those another number made unneeded.
            # So are the text's unknown numbers, when an attachment gave a known one (D-056).
            state["unknown"] = []
        elif state.get("number_sources_left"):
            following, *rest = state["number_sources_left"]
            _progress(progress, f"None of those is known; trying the numbers from the {following['source']}.")
            email = {**event, "numbers": ";".join(following["numbers"])}
            _search(email, following["numbers"], get_index, counters,
                    state={**kept, "number_source": following["source"], "number_sources_left": rest}, progress=progress)
            return
        elif state.get("text_unknown"):
            # The attachments did not help: back to the text's numbers, held as before (D-056).
            event = {**event, "numbers": ";".join(state["text_numbers"])}
            state = {**{key: value for key, value in state.items() if key not in ("number_source", "number_sources_left")},
                     "unknown": state["text_unknown"], "attachments_tried": True}
    _decide(event, state, counters, progress=progress)


def _resume_team(event: dict, counters: Counter, *, progress: bool = False) -> None:
    """Apply the team's answers from the Teams cards; post again a card that did not reach Teams (D-037)."""
    # Waiting for an answer records nothing: an event only when something changed.
    state = _copy_cards(dict(event["state"]))
    for retired in ("team_wait_cycles", "team_retry_counts"):  # the earlier resend counters
        state.pop(retired, None)
    chosen = dict(state.get("chosen", {}))
    skipped_files = dict(state.get("skipped_files", {}))
    not_all_numbers = set(state.get("not_all_numbers", []))
    cards, card_history, base_cards = state["cards"], state["team_card_history"], state["team_base_cards"]
    retry_with_power_bi: list[str] = []
    answered = changed = False
    for number, card_id in list(cards.items()):
        if number in chosen:
            continue
        card_history.setdefault(number, [card_id])
        base_cards.setdefault(number, _team_card_base_id(card_id))
        # Answers to an older card of this number still count.
        answer, reported = _read_card_answer(state, number, card_history[number])
        if answer is None:
            changed |= reported
            if _check_delivery(event, state, number, card_id):
                changed = True
                failed = state["card_delivery"][number].get("failed")
                _progress(progress, f"Teams card for {number} did not reach Teams: {failed}." if failed
                          else f"Teams card for {number} did not reach Teams; posted again as {cards[number]}.")
            else:
                _progress(progress, f"No team response has arrived yet for {number}.")
            continue
        state["card_delivery"].pop(number, None)
        answered = True
        files = state["ambiguous"][number]
        choice = answer["choice"]
        if choice == "manual":
            state["chosen"] = chosen
            _hold(event, state, f"{answer['responder'] or 'the team'} chose to handle it manually", counters, "")
            return
        if choice == "all":
            chosen[number] = files
        elif choice == "not_all":
            retry_with_power_bi.append(number)
            skipped_files[number] = list(files)
            not_all_numbers.add(number)
            state["ambiguous"].pop(number, None)
        elif choice.startswith("file:") and choice[5:].isdigit() and int(choice[5:]) < len(files):
            chosen[number] = [files[int(choice[5:])]]
        else:
            # Refuse an answer that is not one of the offered options.
            raise ValueError(f"Unknown Teams answer {choice!r} for {number}")
    if not answered:
        if changed:
            tracking.record(event, tracking.WAITING_TEAM, state=state)
        return
    state["chosen"] = chosen
    if retry_with_power_bi:
        requested_numbers = [
            number for number in str(event.get("numbers") or "").split(";") if number
        ] or retry_with_power_bi
        state["skipped_files"] = skipped_files
        state["not_all_numbers"] = sorted(not_all_numbers)
        state["missing"] = state["pbi_asked"] = retry_with_power_bi
        state = _ask_power_bi(event, state, requested_numbers, retry_with_power_bi)
        tracking.record(event, tracking.WAITING_PBI, state=state)
        counters["waiting_pbi"] += 1
        _progress(progress, f"Checking Power BI for skipped number(s): {', '.join(retry_with_power_bi)}.")
        return
    _decide(event, state, counters, progress=progress)


def _write_summary(counters: Counter, received: int) -> None:
    config.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    summary = " ".join(f"{key}={value}" for key, value in sorted(counters.items()))
    with config.LOG_PATH.open("a", encoding="utf-8") as log_file:
        # Local time (UTC+8), to read next to the dashboard; the tracking file keeps UTC.
        log_file.write(f"{datetime.now(config.LOCAL_TIME):%Y-%m-%d %H:%M:%S} UTC+8 received={received} {summary}\n")


def _cycle(
    counters: Counter,
    *,
    progress: bool = False,
    process_new: bool = True,
    resume: bool = True,
) -> int:
    if not process_new and not resume:
        raise ValueError("At least one of process_new or resume must be enabled")
    model = number_model = None
    cold_start = True
    if process_new:
        _progress(progress, "Loading processed IDs and active models...")
        processed_ids = outlook_reader.load_processed_ids(config.PROCESSED_IDS_PATH)
        training_data = classifier.load_training_data(config.TRAINING_DATA_PATH)
        model = model_registry.load_active("intent")
        number_model = model_registry.load_active("number")
        cold_start = model is None or not classifier.has_enough_training_data(
            training_data, config.MIN_TRAINING_SAMPLES_PER_CLASS
        )

    index_cache: list[pod_files.FileIndex] = []

    def get_index() -> pod_files.FileIndex:
        if not index_cache:
            if not config.SCAN_FOLDERS:
                raise ShareUnavailable("POD_SCAN_FOLDERS is empty in .env")
            _progress(progress, f"Indexing {len(config.SCAN_FOLDERS)} POD scan folder(s); this may take a while...")
            try:
                index_cache.append(
                    pod_files.build_index(config.SCAN_FOLDERS, config.SCAN_EXCLUDED_FOLDERS)
                )
            except OSError as error:
                raise ShareUnavailable(str(error)) from error
            counters["index_seconds"] = round(index_cache[0].seconds)
            _progress(progress, f"Scan folders indexed in {counters['index_seconds']} seconds.")
        return index_cache[0]

    _progress(progress, f"Connecting to Outlook mailbox '{config.OUTLOOK_MAILBOX_NAME}'...")
    namespace_folder = outlook_reader.get_folder(
        outlook_reader.connect_outlook(), config.OUTLOOK_MAILBOX_NAME
    )
    emails = []
    if process_new:
        emails = outlook_reader.get_unread_emails(namespace_folder, processed_ids)
        _progress(progress, f"Found {len(emails)} unread email(s) not previously processed.")
        if not emails and resume:
            _progress(progress, "No new unread emails. Checking pending Power BI, team, and review items...")
    for position, email in enumerate(emails, start=1):
        subject = " ".join(str(email.get("subject") or "(no subject)").split())
        _progress(progress, f"Processing email {position}/{len(emails)}: {subject}")
        try:
            _process_new_email(email, model, number_model, cold_start, get_index, counters, progress=progress)
        except ShareUnavailable as error:
            # Leave this and later emails unprocessed; the next run retries.
            counters[f"share_unavailable ({error})"] += 1
            _progress(progress, f"Paused: scan folders are unavailable ({error}). Email remains unprocessed.")
            break
        except Exception as error:
            tracking.record(email, tracking.ERROR, reason=f"{type(error).__name__}: {error}")
            counters["error"] += 1
        outlook_reader.mark_as_processed(email["entry_id"], config.PROCESSED_IDS_PATH)
        history = tracking.history(str(email["entry_id"]))
        status = history[-1]["status"] if history else "status unavailable"
        _progress(progress, f"Finished email {position}/{len(emails)}: {status}")

    if resume:
        try:
            _check_outlook(namespace_folder, counters, progress=progress)
        except Exception as error:
            counters[f"Outlook check of waiting emails failed ({type(error).__name__}: {error})"] += 1
        resumes = (
            # Held and Manual Review first: an answer from Teams becomes Reviewed and continues in this same run.
            (tracking.HELD, lambda event, counters: _resume_held(event, counters, progress=progress)),
            (tracking.MANUAL_REVIEW, lambda event, counters: _resume_manual(event, counters, progress=progress)),
            (tracking.REVIEWED,lambda event, counters: _resume_reviewed(event, get_index, counters, progress=progress)),
            (
                tracking.WAITING_PBI,
                lambda event, counters: _resume_pbi(event, get_index, counters, progress=progress),
            ),
            (tracking.WAITING_TEAM, lambda event, counters: _resume_team(event, counters, progress=progress)),
        )
        for status, resume_event in resumes:
            pending = tracking.latest(status)
            if status == tracking.HELD:
                pending = [event for event in pending if "correction_card" in event["state"]]
            if status == tracking.MANUAL_REVIEW:
                pending = [event for event in pending if "review_card" in event["state"]]
            if pending:
                _progress(progress, f"Checking {len(pending)} item(s) in '{status}'...")
            for event in pending:
                subject = " ".join(str(event.get("subject") or "(no subject)").split())
                _progress(progress, f"Checking {status}: {subject}")
                try:
                    resume_event(event, counters)
                except ShareUnavailable as error:
                    # Leave this and later items pending; the next run retries.
                    counters[f"share_unavailable ({error})"] += 1
                    _progress(progress, f"Paused: scan folders are unavailable ({error}).")
                    break
                except Exception as error:
                    tracking.record(event, tracking.ERROR, reason=f"{type(error).__name__}: {error}")
                    counters["error"] += 1
                    _progress(progress, f"Failed while checking {status}: {type(error).__name__}: {error}")
                    continue
                history = tracking.history(str(event["entry_id"]))
                current_status = history[-1]["status"] if history else status
                if current_status == status:
                    _progress(progress, f"Still waiting in '{status}'.")
                else:
                    _progress(progress, f"Updated status: {current_status}.")
        waiting_drafts = [
            event for event in tracking.latest(tracking.REPLIED)
            if "draft_entry_id" in event["state"] and "pilot" not in event["state"]
        ]
        if waiting_drafts:
            _progress(progress, f"Checking {len(waiting_drafts)} existing draft(s)...")
        try:
            _check_drafts(namespace_folder, counters)
        except Exception as error:
            # The measure must never stop the agent; the next run tries again.
            counters[f"pilot check failed ({type(error).__name__}: {error})"] += 1
            _progress(progress, f"Draft check failed: {type(error).__name__}: {error}")
        try:
            _check_email_categories(namespace_folder, counters, progress=progress)
        except Exception as error:
            # Like the pilot measure, categories must never stop the agent.
            counters[f"category check failed ({type(error).__name__}: {error})"] += 1
            _progress(progress, f"Category check failed: {type(error).__name__}: {error}")
        try:
            _move_finished(namespace_folder, counters, progress=progress)
        except Exception as error:
            counters[f"finished-email move failed ({type(error).__name__}: {error})"] += 1
            _progress(progress, f"Moving finished emails failed: {type(error).__name__}: {error}")
        try:
            _clean_manual_uploads(counters)
        except Exception as error:
            counters[f"cleaning files to send by hand failed ({type(error).__name__}: {error})"] += 1
        try:
            _sweep_handoff(counters)
        except Exception as error:
            counters[f"Teams folder sweep failed ({type(error).__name__}: {error})"] += 1
    _progress(progress, "Cycle complete.")
    return len(emails)


def run(
    *,
    progress: bool = False,
    process_new: bool = True,
    resume: bool = True,
) -> Counter:
    """Run one cycle unless stopped (`STOP` file) or already running."""
    if not process_new and not resume:
        raise ValueError("At least one of process_new or resume must be enabled")
    counters: Counter = Counter()
    # A resume-only run every minute that did nothing is not logged, or the log
    # fills with empty lines; an intake run always is, so the log shows it ran.
    log = lambda: process_new or bool(counters)
    if Path(config.STOP_FILE).exists():
        _progress(progress, "STOP file is present; no emails were processed.")
        if process_new:
            _write_summary(Counter({"stopped_by_STOP_file": 1}), 0)
        return counters
    _progress(progress, "Starting one POD email processing cycle.")
    _progress(progress, "Validating mailbox and sender-folder settings...")
    config.shared_mailbox_address()
    config.training_data_path()  # likewise a training workbook not set (D-060)
    config.sender_folder("")  # a malformed POD_SENDER_FOLDERS stops the run before any email
    config.send_domains()  # and a malformed POD_SEND_DOMAINS
    config.signature_html()  # likewise a missing signature file, before any reply goes without it
    config.manual_upload_dir()  # and a folder for files to send by hand that is not allowed
    # New emails should not wait five minutes because a resume run holds the
    # lock: intake retries for a while. Resume skips at once; it runs again soon.
    deadline = time.monotonic() + (LOCK_WAIT_SECONDS if process_new else 0)
    while True:
        try:
            with _run_lock():
                received = _cycle(
                    counters,
                    progress=progress,
                    process_new=process_new,
                    resume=resume,
                )
            break
        except AlreadyRunning:
            if time.monotonic() >= deadline:
                _progress(progress, "Another cycle is already running; this run was skipped.")
                if process_new:
                    _write_summary(Counter({"skipped_already_running": 1}), 0)
                return counters
            _progress(progress, "Another cycle is running; waiting for it to finish...")
            time.sleep(LOCK_RETRY_SECONDS)
    if log():
        _write_summary(counters, received)
    if progress:
        summary = ", ".join(f"{key}={value}" for key, value in sorted(counters.items())) or "no actions needed"
        _progress(progress, f"Run summary: {summary}")
    return counters


def reset_local_state(*, progress: bool = True) -> Path:
    """Back up and clear the local run history and processed-ID list."""
    with _run_lock():
        state_files = (
            Path(config.TRACKING_DB_PATH),
            Path(config.PROCESSED_IDS_PATH),
        )
        backup_dir = state_files[0].parent / (
            f"reset-backup-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}"
        )
        backup_dir.mkdir(parents=True, exist_ok=False)
        existing_files = [path for path in state_files if path.exists()]
        for path in existing_files:
            shutil.copy2(path, backup_dir / path.name)
        for path in existing_files:
            path.unlink()

    _progress(progress, f"Local run state reset. Backup saved to: {backup_dir}")
    if not existing_files:
        _progress(progress, "No tracking database or processed-ID file existed.")
    return backup_dir


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run or reset the POD email agent.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--reset",
        action="store_true",
        help="Back up and clear local tracking state, then exit without processing email.",
    )
    modes.add_argument(
        "--process-new",
        action="store_true",
        help="Process unread emails only; do not resume pending work.",
    )
    modes.add_argument(
        "--resume",
        action="store_true",
        help="Resume completed Power Automate, Teams, review, and draft work only.",
    )
    args = parser.parse_args(argv)
    if args.reset:
        reset_local_state()
        return
    run(
        progress=True,
        process_new=args.process_new or not args.resume,
        resume=args.resume or not args.process_new,
    )


if __name__ == "__main__":
    main()
