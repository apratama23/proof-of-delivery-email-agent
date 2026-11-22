"""Demo data for the dashboard: a month of dummy POD emails, labels and trained models.

`python -m sim.run --dashboard` runs this after the scenarios pass, in its own
process, so the dashboard opens on full charts. The scenario checks never see
it, and `python -m sim.run` alone (the CI check) skips it. Every name, number
and email here is made up.

The history goes through the agent's own `tracking.record`, with the clock
set back to each event's time; the labels through `training_refresh` and the
workbook's own columns; the models through the agent's own training code.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd

from app import classifier, config, model_registry, number_crf, pilot, reply_generator, training_refresh, tracking
from app.historical_pull import TRAINING_COLUMNS
from sim import fakes

DAYS = 30
SEED = 7
LOCAL = config.LOCAL_TIME

DOMAINS = (["example1.com"] * 6 + ["example2.com"] * 5 + ["example3.com"] * 4 + ["example4.com"] * 3
           + ["example5.com"] * 2 + ["example6.com", "example7.com"])
PEOPLE = ("alex", "sam", "jordan", "taylor", "chris", "morgan", "casey", "riley", "jamie", "drew")
NAMES = ("Alex", "Sam", "Jordan", "Taylor", "Chris", "Morgan")

POD_SUBJECTS = ("POD request {n}", "Proof of delivery - {n}", "PODs please", "RE: delivery {n}",
                "Signed POD for con note {n}", "POD required", "Missing POD {n}", "Delivery proof needed")
POD_BODIES = (
    "Hi team,\n\nCould you please send the POD for connote {nums}?\n\nThanks,\n{name}",
    "Hello,\n\nPlease send the signed proof of delivery for {nums}. Our ref PO {ref}.\n\nKind regards,\n{name}",
    "Hi,\n\nCustomer is asking for the POD on {nums}. Can you send it through?\n\nCheers,\n{name}\nMobile {phone}",
    "Good morning,\n\nCan we get the PODs for these consignments: {nums}\n\nThank you,\n{name}",
    "Hi POD team,\n\nInvoice {invoice} is on hold until we have the POD for {nums}. Please send.\n\nRegards,\n{name}",
    "Hi,\n\nPlease provide proof of delivery for our order {ref}, con note {nums}.\n\nThanks\n{name}",
)
OTHER_SUBJECTS = ("Remittance advice {invoice}", "Pickup booking tomorrow", "Delivery address change {n}",
                  "Invoice query {invoice}", "Rate card update", "Delivery time for {n}")
OTHER_BODIES = (
    "Hi,\n\nPlease find attached the remittance for invoice {invoice}.\n\nRegards,\n{name}",
    "Hello,\n\nCan you book a pickup for tomorrow morning, 4 pallets, ref {ref}?\n\nThanks,\n{name}",
    "Hi team,\n\nThe delivery address for {n} has changed, please update before dispatch.\n\nCheers,\n{name}",
    "Hi,\n\nWe have a question about invoice {invoice}, the amount looks different from the quote.\n\n{name}",
    "Hello,\n\nWhat time will {n} be delivered today? The receiver closes at 3pm.\n\nThanks,\n{name}",
)
NO_NUMBER_BODIES = ("Thanks for your help today!", "Noted, thank you.", "Please see the meeting notes attached.")


class _Clock(datetime):
    """`datetime` whose now() is the demo event's time, for `tracking.record`."""

    at: datetime | None = None

    @classmethod
    def now(cls, tz=None):
        return cls.at.astimezone(tz) if tz else cls.at


def _record(email: dict, status: str, at: datetime, **fields) -> None:
    _Clock.at = at
    tracking.record(email, status, **fields)


def _connote(rng: random.Random) -> str:
    return str(rng.randint(3000000, 3999999))


def _fill(template: str, rng: random.Random, numbers: list[str]) -> str:
    return template.format(
        n=numbers[0] if numbers else _connote(rng), nums=", ".join(numbers) or _connote(rng),
        ref=f"80{rng.randint(100000, 999999)}", invoice=str(rng.randint(5000000, 5999999)),
        phone=f"04{rng.randint(10000000, 99999999)}", name=rng.choice(NAMES),
    )


def _scan(number: str) -> str:
    return str(fakes.SCAN_ROOT / "Demo" / f"{number} - Signed.pdf")


def _times(rng: random.Random, now: datetime) -> list[datetime]:
    """When each demo email arrived: about 15-20 per weekday, a few at weekends, mostly mornings."""
    start = now.astimezone(LOCAL).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=DAYS - 1)
    times = []
    for day in range(DAYS):
        date = start + timedelta(days=day)
        count = rng.randint(14, 20) if date.weekday() < 5 else rng.randint(1, 4)
        for _ in range(count):
            hour = min(17.9, max(7.0, rng.gauss(10, 2.2)))
            at = date + timedelta(hours=hour, minutes=rng.random() * 5)
            if at < now:
                times.append(at)
    return sorted(times)


def _reply_state(found: list[str], not_received: list[str], chosen: bool) -> dict:
    files = {number: _scan(number) for number in found}
    state = {
        "found": {} if chosen else files, "chosen": files if chosen else {}, "ambiguous": {}, "missing": [],
        "not_received": {number: {"Connote": number, "POD Status": "Unsigned"} for number in not_received},
        "attachments": list(files.values()),
        "reply": reply_generator.build_reply(found, not_received, include_notice=False),
    }
    return state


def add_history(seed: int = SEED, now: datetime | None = None) -> int:
    """Write the demo emails into the tracking database; return how many."""
    rng = random.Random(seed)
    now = now or datetime.now(timezone.utc)
    times = _times(rng, now)
    original = tracking.datetime
    tracking.datetime = _Clock
    try:
        for index, received in enumerate(times, start=1):
            _one_email(rng, index, received, now)
    finally:
        tracking.datetime = original
    return len(times)


def spread_scenarios(seed: int = SEED, now: datetime | None = None) -> None:
    """Move the scenario emails, all processed a moment ago, onto earlier days of the month.

    For the charts only, after the scenario checks have passed: each email
    keeps its own events in order, shifted together by whole days.
    """
    rng = random.Random(seed + 2)
    now = now or datetime.now(timezone.utc)
    with sqlite3.connect(config.TRACKING_DB_PATH) as connection:
        ids = [row[0] for row in connection.execute(
            "SELECT DISTINCT entry_id FROM events WHERE entry_id NOT LIKE 'DEMO-%'")]
        for entry_id in ids:
            shift = timedelta(days=rng.randint(1, DAYS - 2))
            events = connection.execute("SELECT id, at_utc FROM events WHERE entry_id = ?", (entry_id,)).fetchall()
            for event_id, at_utc in events:
                moved = datetime.fromisoformat(at_utc) - shift
                connection.execute("UPDATE events SET at_utc = ?, received_time = ? WHERE id = ?",
                                   (moved.isoformat(), f"{(moved - timedelta(minutes=2)).astimezone(LOCAL):%Y-%m-%d %H:%M:%S}",
                                    event_id))


def _one_email(rng: random.Random, index: int, received: datetime, now: datetime) -> None:
    seen = received + timedelta(minutes=rng.randint(1, 4))
    age_hours = (now - seen).total_seconds() / 3600
    sender = f"{rng.choice(PEOPLE)}@{rng.choice(DOMAINS)}"
    numbers = [_connote(rng) for _ in range(rng.choices((1, 2, 3), (70, 22, 8))[0])]
    email = {"entry_id": f"DEMO-{index:04d}", "received_time": f"{received:%Y-%m-%d %H:%M:%S}",
             "sender_email": sender, "to_recipients": "POD", "message_id": f"<demo-{index}@example.com>"}
    kind = rng.choices(
        ("files", "files_pbi", "not_received", "chosen", "resolved", "other", "no_number", "guard"),
        (52, 10, 8, 4, 5, 9, 6, 6))[0]
    if age_hours < 72 and rng.random() < 0.12:
        kind = rng.choice(("waiting_pbi", "waiting_team", "held", "manual"))

    if kind in ("other", "no_number", "guard"):
        email["subject"] = _fill(rng.choice(OTHER_SUBJECTS), rng, numbers)
        email["body"] = rng.choice(NO_NUMBER_BODIES) if kind == "no_number" else _fill(rng.choice(OTHER_BODIES), rng, numbers)
        if kind == "guard":
            email["subject"] = f"Automatic reply: {email['subject']}"
            _record(email, tracking.SKIPPED_GUARD, seen, reason="automatic or bounce subject")
        elif kind == "no_number":
            _record(email, tracking.SKIPPED_NO_NUMBER, seen, intent="OTHER", confidence=round(rng.uniform(0.9, 0.99), 3))
        else:
            _record(email, tracking.OTHER_INTENT, seen, numbers=numbers[:1], intent="OTHER",
                    confidence=round(rng.uniform(0.9, 0.99), 3))
            filed = seen + timedelta(hours=rng.uniform(1, 20))
            if filed < now:  # the team files it in Outlook, and the agent sees it go (D-041)
                _record(email, tracking.RESOLVED, filed, reason="moved or deleted in Outlook", numbers=numbers[:1],
                        intent="OTHER")
        return

    email["subject"] = _fill(rng.choice(POD_SUBJECTS), rng, numbers)
    email["body"] = _fill(rng.choice(POD_BODIES), rng, numbers)
    sure = round(rng.uniform(0.9, 0.995), 3)
    base = {"numbers": numbers, "intent": "POD_REQUEST", "confidence": sure}
    request = f"demo{index:04d}"
    asked = {"found": {}, "ambiguous": {}, "missing": numbers[-1:], "pbi_asked": numbers[-1:],
             "pbi_request": f"{request}-p0",
             "card_delivery": {"power_bi": {"sent_at": seen.astimezone(timezone.utc).isoformat(), "resends": 0}}}

    if kind == "waiting_pbi":
        _record(email, tracking.WAITING_PBI, seen, state=asked, **base)
    elif kind == "waiting_team":
        ambiguous = {numbers[0]: [_scan(numbers[0]), _scan(numbers[0]).replace(".pdf", " (2).pdf")]}
        _record(email, tracking.WAITING_TEAM, seen, state={**asked, "ambiguous": ambiguous, "cards": {numbers[0]: f"{request}-0"}}, **base)
    elif kind == "held":
        _record(email, tracking.WAITING_PBI, seen, state=asked, **base)
        _record(email, tracking.HELD, seen + timedelta(minutes=6), reason="number not on the share and not in Power BI",
                state={**asked, "unknown": numbers[-1:], "not_received": {}, "correction_card": f"{request}-h0"}, **base)
    elif kind == "manual":
        _record(email, tracking.MANUAL_REVIEW, seen, numbers=numbers, intent="OTHER",
                confidence=round(rng.uniform(0.55, 0.85), 3), reason="intent below threshold: maybe not a POD request",
                state={"review_card": f"{request}-m0"})
    elif kind == "resolved":
        _record(email, tracking.WAITING_PBI, seen, state=asked, **base)
        replied_at = seen + timedelta(minutes=rng.randint(20, 180))
        if replied_at < now:
            _record(email, tracking.RESOLVED, replied_at,
                    reason=f"replied manually in Outlook (reply on {replied_at.astimezone(LOCAL):%d %b %Y %H:%M} UTC+8)",
                    state={**asked, "manual_reply": {"action": "reply", "at": replied_at.astimezone(timezone.utc).isoformat()}},
                    **base)
    else:
        found, not_received = numbers, []
        drafted = seen
        if kind in ("files_pbi", "not_received", "chosen"):
            _record(email, tracking.WAITING_PBI, seen, state=asked, **base)
            drafted = seen + timedelta(minutes=rng.randint(3, 9))
            if kind == "not_received":
                found, not_received = numbers[:-1], numbers[-1:]
        if kind == "chosen":
            ambiguous = {numbers[0]: [_scan(numbers[0]), _scan(numbers[0]).replace(".pdf", " (2).pdf")]}
            _record(email, tracking.WAITING_TEAM, drafted, state={**asked, "ambiguous": ambiguous,
                                                                  "cards": {numbers[0]: f"{request}-0"}}, **base)
            drafted += timedelta(minutes=rng.randint(15, 120))
            if drafted > now:
                return
        state = {**_reply_state(found, not_received, kind == "chosen"), "draft_entry_id": f"D-{request}",
                 "conversation_id": f"C-{request}"}
        _record(email, tracking.REPLIED, drafted, reason="draft", state=state, **base)
        decided_at = drafted + timedelta(hours=rng.uniform(0.3, 5))
        if decided_at < now:
            outcome = rng.choices((pilot.SENT_SAME_PODS, pilot.SENT_OTHER_PODS, pilot.DELETED, None), (86, 6, 3, 5))[0]
            if outcome:
                sent = {"sent_at": decided_at.astimezone(timezone.utc).isoformat()} if outcome != pilot.DELETED else {}
                _record(email, tracking.REPLIED, decided_at, reason=f"draft {outcome}",
                        state={**state, "pilot": outcome, **sent}, **base)


def add_training_data(seed: int = SEED, now: datetime | None = None) -> None:
    """Labeled emails for the Labeling and Models tabs: past emails, pulls of the agent's decisions, reviews."""
    rng = random.Random(seed + 1)
    now = now or datetime.now(timezone.utc)
    path = config.TRAINING_DATA_PATH
    rows = []
    for index in range(1, 381):
        is_pod = rng.random() < 0.62
        numbers = [_connote(rng) for _ in range(rng.choices((1, 2), (80, 20))[0])]
        ref = f"80{rng.randint(100000, 999999)}"
        template = rng.choice(POD_BODIES if is_pod else OTHER_BODIES)
        body = template.format(n=numbers[0], nums=", ".join(numbers), ref=ref, invoice=str(rng.randint(5000000, 5999999)),
                               phone=f"04{rng.randint(10000000, 99999999)}", name=rng.choice(NAMES))
        label = "POD_REQUEST" if is_pod else "OTHER"
        if rng.random() < 0.04:  # people disagree sometimes; the scores should not look perfect
            label = "OTHER" if is_pod else "POD_REQUEST"
        labeled = index <= 330
        received = now - timedelta(days=rng.uniform(40, 365))
        rows.append({
            "entry_id": f"PAST-{index:04d}", "received_time": f"{received.astimezone(LOCAL):%Y-%m-%d %H:%M:%S}",
            "sender_email": f"{rng.choice(PEOPLE)}@{rng.choice(DOMAINS)}", "to_recipients": "POD",
            "subject": rng.choice(POD_SUBJECTS if is_pod else OTHER_SUBJECTS).format(n=numbers[0], invoice=rng.randint(5000000, 5999999)),
            "body": body, "outlook_folder": "Inbox",
            "intent_label": label if labeled else "",
            "connotes_manual": ";".join(numbers) if labeled and label == "POD_REQUEST" and is_pod else "",
            "reference_number_manual": ref if labeled and label == "POD_REQUEST" and is_pod and "{ref}" in template else "",
            "label_source": training_refresh.MANUAL if labeled else "",
            "labeled_at": (now - timedelta(days=rng.uniform(1, 30))).isoformat() if labeled else "",
        })
    existing = pd.read_excel(path, dtype=str) if path.exists() else pd.DataFrame(columns=TRAINING_COLUMNS)
    data = pd.concat([existing, pd.DataFrame(rows)], ignore_index=True)
    for column in TRAINING_COLUMNS:
        if column not in data.columns:
            data[column] = ""
    data[TRAINING_COLUMNS].to_excel(path, index=False)


def add_pulls(seed: int = SEED, now: datetime | None = None) -> None:
    """"Pull new data" once a week, with the earlier pulls reviewed.

    Each tracked email belongs to the first pull after it arrived. The product
    owner reviewed the earlier pulls, mostly agreeing with the agent; this
    week's pull still waits as "System decision". Runs after the pseudo-labels,
    which clear `intent_label_pseudo` on reviewed rows.
    """
    rng = random.Random(seed + 3)
    now = now or datetime.now(timezone.utc)
    path = config.TRAINING_DATA_PATH
    training_refresh.pull_system_decisions(path, now=now)
    data = pd.read_excel(path, dtype=str)
    pulls = [now - timedelta(weeks=weeks) for weeks in (3, 2, 1, 0)]
    pulled = data["label_source"].eq(training_refresh.SYSTEM_DECISION) & data["system_pulled_at"].fillna("").ne("")
    for index in data[pulled].index:
        received = pd.Timestamp(data.at[index, "received_time"]).tz_localize(LOCAL)
        pulled_at = next((pull for pull in pulls if pull >= received), now)
        data.at[index, "system_pulled_at"] = pulled_at.isoformat()
        if pulled_at < now and rng.random() < 0.85:
            agent = str(data.at[index, "intent_label_pseudo"])
            data.at[index, "intent_label"] = agent if rng.random() < 0.93 else ("OTHER" if agent == "POD_REQUEST" else "POD_REQUEST")
            data.at[index, "label_source"] = training_refresh.MANUAL
            data.at[index, "labeled_at"] = now.isoformat()
    data.to_excel(path, index=False)


def train_models(now: datetime | None = None) -> None:
    """Two training runs (most of the data, then all), so the Models tab shows a trend; activate the best."""
    path = config.TRAINING_DATA_PATH
    full = pd.read_excel(path, dtype=str)
    for share in (0.7, 1.0):
        part = full.iloc[: int(len(full) * share)]
        part.to_excel(path, index=False)
        classifier.train_and_compare(classifier.load_training_data(path), path)
        number_crf.train_and_compare(path)
    full.to_excel(path, index=False)
    model_registry.set_active("intent", "xgboost")
    model_registry.set_active("number", "crf")
    classifier.generate_pseudo_labels(path, threshold=0.95)
    number_crf.generate_pseudo_labels(path, config.NUMBER_CONFIDENCE_THRESHOLD)


def main() -> None:
    fakes.apply_settings()
    spread_scenarios()
    print(f"Adding demo data for the dashboard: {add_history()} emails over {DAYS} days...", flush=True)
    add_training_data()
    print("Training demo models (about half a minute)...", flush=True)
    train_models()
    add_pulls()
    print("Demo data ready.", flush=True)


if __name__ == "__main__":
    main()
