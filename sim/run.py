"""Run every scenario through the real agent and report what happened: `python -m sim.run`.

`--dashboard` then opens the dashboard on the simulated data. The scenarios
and what each checks are in docs/SIMULATION.md. Exit code 1 if one fails.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Callable

from app import main, pilot, tracking
from sim import fakes

ROUND_PAUSE = 1.2  # seconds between runs; Teams cards count as "not taken" after 1 second here
ROUNDS = 9


@dataclass
class Scenario:
    entry_id: str
    title: str
    subject: str
    body: str
    sender: str = "ops@customer.example"
    files: list[str] = field(default_factory=list)  # dummy scans, under the scan folder
    large: str = ""  # one dummy scan of 2 MB, over the simulated 1 MB limit
    pbi_rows: list[dict] = field(default_factory=list)
    answer: Callable | None = None  # how the team answers its Teams card
    pbi_delay: int = 0
    expect_status: str = tracking.REPLIED
    expect_files: list[str] | None = None  # attached to the draft, in any order
    check: Callable | None = None  # more checks: (world) -> "" or what went wrong
    attached: list[dict] = field(default_factory=list)  # {"name", "text" OCR or the PDF reader finds} or {"name", "rows"}


def _scenarios() -> list[Scenario]:
    signed = fakes.SCAN_ROOT / "POD" / "Receiver" / "524037 - Signed.pdf"
    return [
        Scenario("S01", "Scan on the share", "POD request", "Please send the POD for 1234567",
                 files=["1234567.pdf"], expect_files=["1234567.pdf"]),
        Scenario("S02", "Short-form list (D-034)", "PODs please", "Can you send PODs for 7335842/843",
                 files=["7335842.pdf", "7335843.pdf"], expect_files=["7335842.pdf", "7335843.pdf"]),
        Scenario("S03", "Two signed scans of one number: team picks one (D-006, D-042)", "POD 444444",
                 "Please send the POD for 444444", files=["444444 - Signed.pdf", "444444 - Signed (2).pdf"],
                 answer=fakes.choose_file("444444 - Signed.pdf"), expect_files=["444444 - Signed.pdf"]),
        Scenario("S04", "Not on the share, Unsigned in Power BI (D-004)", "POD 7777777",
                 "Please send the POD for 7777777",
                 pbi_rows=[{"Connote": "7777777", "Reference": "", "POD Status": "Unsigned", "Sender": "Example2 Company"}],
                 expect_files=[], check=lambda w: _outstanding(w, "7777777", "Unsigned")
                 or _in_draft(w, "S04", "not received the Proof of Delivery for 7777777")),
        Scenario("S05", "Invoice next to a connote: held, corrected on the Teams card (D-035, D-036)",
                 "POD request", "Invoice 20260407 - 80655502, please send the POD",
                 files=["80655502.pdf"],
                 answer=lambda card: {"choice": "correct", "connotes": "80655502"},
                 expect_files=["80655502.pdf"], check=lambda w: _not_outstanding(w, "20260407")),
        Scenario("S06", "Unreadable list: held, numbers typed on the card (D-034, D-036)", "PODs",
                 "Please send PODs 7336101-199", files=["7336101.pdf", "7336102.pdf"],
                 answer=lambda card: {"choice": "correct", "connotes": "7336101; 7336102"},
                 expect_files=["7336101.pdf", "7336102.pdf"]),
        Scenario("S07", "Files too large: a draft without them, a status list and a SharePoint link (D-038, D-050)",
                 "PODs", "Please send the PODs for 2345678 and 7777707", large="2345678.pdf",
                 pbi_rows=[{"Connote": "7777707", "Reference": "", "POD Status": "Unsigned", "Sender": "Example2 Company"}],
                 expect_files=[], check=_large_email),
        Scenario("S08", "Answered by hand while waiting for Power BI (D-039)", "POD 3333333",
                 "Please send the POD for 3333333", pbi_delay=2,
                 pbi_rows=[{"Connote": "3333333", "Reference": "", "POD Status": "Signed", "Sender": "Example2 Company"}],
                 expect_status=tracking.RESOLVED, expect_files=None, check=_answered_by_hand),
        Scenario("S09", "Not a POD request (intent OTHER)", "Remittance advice",
                 "Remittance advice: invoice 20261111 has been paid", expect_status=tracking.OTHER_INTENT),
        Scenario("S10", "Automatic reply is never answered (D-002)", "Automatic reply: Out of office",
                 "I am away until Monday. Re: POD 1234567", expect_status=tracking.SKIPPED_GUARD),
        Scenario("S11", "Teams card never reaches Teams: 3 resends, warning, sent again (D-037)", "POD 666666",
                 "Please send the POD for 666666", files=["666666 - Signed.pdf", "666666 - Signed (2).pdf"],
                 answer=fakes.choose_file("666666 - Signed.pdf"), expect_files=["666666 - Signed.pdf"], check=_card_failed_once),
        Scenario("S12", "Reference number: Power BI gives the connote, its scan is sent", "POD request",
                 "Please send the POD for our reference 56781234", files=["3456789.pdf"],
                 pbi_rows=[{"Connote": "3456789", "Reference": "0056781234", "POD Status": "Signed", "Sender": "Example2 Company"}],
                 expect_files=["3456789.pdf"]),
        Scenario("S13", "Customer whose Power BI row gives the scan path (D-043)", "POD request",
                 "Please send the POD for 80655501",
                 pbi_rows=[{"Connote": "12345678", "Reference": "0080655501", "POD Status": "Signed",
                            "Sender": "Example1 Company", "Scan Path": signed.as_uri()}],
                 expect_files=["524037 - Signed.pdf"]),
        Scenario("S14", "Team answers Not all: Power BI says Unsigned", "POD 555555",
                 "Please send the POD for 555555", files=["555555 - Signed.pdf", "555555 - Signed (2).pdf"],
                 answer=lambda card: {"choice": "not_all"},
                 pbi_rows=[{"Connote": "555555", "Reference": "", "POD Status": "Unsigned", "Sender": "Example2 Company"}],
                 expect_files=[], check=lambda w: _outstanding(w, "555555", "Unsigned")),
        Scenario("S15", "Reprocessed in Power BI; the first request's answer comes late (D-040)", "POD 8888888",
                 "Please send the POD for 8888888", pbi_delay=2,  # the right answer comes after the stale one
                 pbi_rows=[{"Connote": "8888888", "Reference": "", "POD Status": "Unsigned", "Sender": "Example2 Company"}],
                 expect_files=[], check=_late_old_answer),
        Scenario("S19", "Scan names as on the share: reference after a dash, a signed copy beside an unsigned one (D-042)",
                 "PODs", "Please send the PODs for 7000006 and our reference 80600001",
                 files=["7000003-80600001 - Signed.pdf", "7000006..pdf", "7000006 - Signed.pdf", "7000006 - Unsigned.pdf",
                        "PLT70000061J - Signed.pdf"],
                 expect_files=["7000003-80600001 - Signed.pdf", "7000006 - Signed.pdf"]),
        Scenario("S20", "No number in the text: the PDF name's number is unknown, the picture's is found (D-044)",
                 "POD", "Please send the POD, see attached", files=["1234520.pdf"],
                 attached=[{"name": "Manifest 7000020.pdf"}, {"name": "screen.png", "text": "Consignment 1234520"}],
                 expect_files=["1234520.pdf"],
                 check=lambda w: "" if _latest("S20")["state"].get("number_source") == "picture" else "not read from the picture"),
        Scenario("S21", "A connote and its reference: one document, attached once (D-044)", "PODs",
                 "Please send POD for 3000021, our reference 80600021", files=["3000021 - Signed.pdf"],
                 pbi_rows=[{"Connote": "3000021", "Reference": "80600021", "POD Status": "Signed", "Sender": "Example2 Company"}],
                 expect_files=["3000021 - Signed.pdf"],
                 check=lambda w: _in_draft(w, "S21", "3000021 (your reference 80600021)")),
        Scenario("S22", "Two signed scans of one number: Power BI's scan path chooses, no card (D-044)", "POD 3000022",
                 "Please send the POD for 3000022", files=["3000022 - Signed.pdf", "3000022 - Signed (2).pdf"],
                 pbi_rows=[{"Connote": "3000022", "Reference": "", "POD Status": "Signed", "Sender": "Example2 Company",
                            "Scan Path": (fakes.SCAN_ROOT / "Sep" / "3000022 - Signed (2).pdf").as_uri()}],
                 expect_files=["3000022 - Signed (2).pdf"]),
        Scenario("S23", "Checked customer: its reference in Power BI, the connote's scan in its folder (D-044)", "POD",
                 "Please send the POD for our reference 80600023", sender="ops@example1.com",
                 files=["Example1/Sep/4000023 - Signed.pdf"],
                 pbi_rows=[{"Connote": "4000023", "Reference": "80600023", "POD Status": "Signed", "Sender": "Example1 Company"}],
                 expect_files=["4000023 - Signed.pdf"]),
        Scenario("S24", "A POD request with no number anywhere goes to Manual Review (D-044)", "POD please",
                 "Please send the POD for our last delivery", expect_status=tracking.MANUAL_REVIEW, expect_files=None),
        Scenario("S25", "Intent below threshold, its scan on the share: answered like any POD request (D-044)",
                 "POD maybe", "Maybe you can send the POD for 1234525?", files=["1234525.pdf"],
                 expect_files=["1234525.pdf"]),
        Scenario("S26", "Intent below threshold, its number nowhere: Manual Review with a review card, no correction card",
                 "POD maybe", "Maybe you can send the POD for 7777726?",
                 expect_status=tracking.MANUAL_REVIEW, expect_files=None),
        Scenario("S27", "One number found, one not in Power BI: the team keeps it, the reply says so (D-045)", "PODs",
                 "Please send the PODs for 1234527 and 7777727", files=["1234527.pdf"],
                 answer=lambda card: {"choice": "correct", "connotes": _prefilled(card)},  # the number is right
                 expect_files=["1234527.pdf"],
                 check=lambda w: _in_draft(w, "S27", "Regarding 7777727, we were unable to find it in our system.")
                 or _outstanding(w, "7777727", "Not in The System")
                 or ("" if sum(m["entry_id"] == "S27" for m in w.flows.posted) == 1 else "the card was posted again")),
        Scenario("S28", "The team changes the unknown number, still not in Power BI: asked again, then kept (D-045)",
                 "PODs", "Please send the PODs for 1234528 and 7777728", files=["1234528.pdf"],
                 answer=lambda card: {"choice": "correct",
                                      "connotes": "8888828" if _prefilled(card) == "7777728" else _prefilled(card)},
                 expect_files=["1234528.pdf"], check=_changed_then_kept),
        Scenario("S29", "Numbers only in an Excel list: its Con Note column read, a short list written out (D-046)",
                 "PODs", "Please send the PODs for the list attached", files=["1234529.pdf", "7335829.pdf", "7335830.pdf"],
                 attached=[{"name": "Delivery list.xlsx", "rows": [
                     ["Delivery list"], ["Item", "Con Note", "Invoice", "Qty"],
                     ["Fish", 1234529, 20260407, 12], ["Milk", "7335829/830", 20260408, 3]]}],
                 expect_files=["1234529.pdf", "7335829.pdf", "7335830.pdf"],
                 check=lambda w: ("" if _latest("S29")["state"].get("number_source") == "spreadsheet"
                                  else "not read from the spreadsheet")
                 or ("the Invoice column was read" if "20260407" in str(_latest("S29")["numbers"]) else "")),
        Scenario("S30", "Saved in the dashboard as a POD request with no numbers: attachments read again (D-046)",
                 "POD please", "Please send the POD for yesterday's delivery",
                 expect_status=tracking.MANUAL_REVIEW, expect_files=None, check=_reviewed_without_numbers),
        Scenario("S31", "Manual Review answered in Teams: POD request, a connote and a reference typed (D-048)",
                 "Remittance maybe", "Remittance maybe; also the POD for 1234531 please", files=["1234531.pdf", "3000031.pdf"],
                 pbi_rows=[{"Connote": "3000031", "Reference": "80600031", "POD Status": "Signed", "Sender": "Example2 Company"}],
                 answer=lambda card: {"choice": "pod", "connotes": _prefilled(card), "references": "80600031"},
                 expect_files=["1234531.pdf", "3000031.pdf"],
                 check=lambda w: "" if _latest("S31")["state"]["review"]["references"] == ["80600031"]
                 else "the reference typed on the card was not used"),
        Scenario("S32", "Manual Review answered in Teams: not a POD request (D-048)", "Remittance maybe",
                 "Remittance maybe for invoice 20263232", answer=lambda card: {"choice": "other"},
                 expect_status=tracking.REVIEWED_OTHER, expect_files=None),
        Scenario("S33", "A wrong number in the text, not in Power BI; the right connote in the attachment (D-056)",
                 "POD request", "Please send the POD for our invoice 7777733", files=["1234533.pdf"],
                 attached=[{"name": "Delivery 1234533.pdf"}], expect_files=["1234533.pdf"],
                 check=lambda w: ("the text's number is in the draft" if "7777733" in w.mailbox.drafts["S33"]["body"] else "")
                 or ("7777733 was added to POD Outstanding" if "7777733" in w.flows.outstanding else "")),
        Scenario("S34", "Partial sending: a sender of a chosen domain is answered directly (D-057)", "POD request",
                 "Please send the POD for 1234534", sender="ops@sendnow.example", files=["1234534.pdf"],
                 expect_files=["1234534.pdf"],
                 check=lambda w: ("" if w.mailbox.drafts["S34"]["mode"] == "send" else "the reply was not sent")
                 or _all_drafts_but_s34(w)),
        Scenario("S16", "Waiting for the team, filed in Actioned/Finished by hand (D-041)", "POD 777000",
                 "Please send the POD for 777000", files=["777000 - Signed.pdf", "777000 - Signed (2).pdf"],
                 expect_status=tracking.RESOLVED, check=lambda w: _reason_and_no_draft(w, "S16", "moved to Actioned/Finished in Outlook")),
        Scenario("S17", "Waiting for Power BI, moved to another folder by hand (D-041)", "POD 9191919",
                 "Please send the POD for 9191919", pbi_delay=4,
                 pbi_rows=[{"Connote": "9191919", "Reference": "", "POD Status": "Signed", "Sender": "Example2 Company"}],
                 expect_status=tracking.HELD, check=lambda w: _reason_and_no_draft(w, "S17", "moved to Customers in Outlook")
                 or ("the late Power BI result was left in pbi_results" if _results_left("S17") else "")),
        Scenario("S18", "Not a POD request, filed in another folder by hand (product owner: any folder)", "Remittance advice",
                 "Remittance advice: invoice 20262222 has been paid",
                 expect_status=tracking.RESOLVED, check=lambda w: _reason_and_no_draft(w, "S18", "moved or deleted in Outlook")),
    ]


@dataclass
class World:
    mailbox: fakes.FakeMailbox
    flows: fakes.FakeFlows
    notes: dict = field(default_factory=dict)  # what the scenario hooks saw during the run


def _outstanding(world: World, number: str, status: str) -> str:
    row = world.flows.outstanding.get(number)
    return "" if row and row.get("Status") == status else f"POD Outstanding has no {status} row for {number}"


def _not_outstanding(world: World, number: str) -> str:
    return f"{number} is still in POD Outstanding" if number in world.flows.outstanding else ""


def _changed_then_kept(world: World) -> str:
    cards = [_prefilled(m) for m in world.flows.posted if m["entry_id"] == "S28" and m["kind"] == "choice"]
    if cards != ["7777728", "8888828"]:
        return f"the cards asked {cards}, not 7777728 then 8888828"
    return (_in_draft(world, "S28", "Regarding 8888828, we were unable to find it in our system.")
            or _outstanding(world, "8888828", "Not in The System") or _not_outstanding(world, "7777728")
            or ("the draft names 7777728" if "7777728" in world.mailbox.drafts["S28"]["body"] else ""))


def _reviewed_without_numbers(world: World) -> str:
    statuses = [event["status"] for event in tracking.history("S30")]
    if statuses != [tracking.MANUAL_REVIEW, tracking.REVIEWED, tracking.MANUAL_REVIEW]:
        return f"statuses {statuses}"
    return "" if _latest("S30")["reason"] == "POD request without a number" else f"reason {_latest('S30')['reason']}"


def _prefilled(card: dict) -> str:
    return next(item["value"] for item in card["card"]["body"] if item["type"] == "Input.Text")


def _all_drafts_but_s34(world: World) -> str:
    sent = sorted(entry for entry, draft in world.mailbox.drafts.items() if draft["mode"] == "send")
    return "" if sent == ["S34"] else f"sent directly: {sent}"


def _in_draft(world: World, entry_id: str, text: str) -> str:
    return "" if text in world.mailbox.drafts.get(entry_id, {}).get("body", "") else f"draft text lacks '{text}'"


def _large_email(world: World) -> str:
    message = next((m for m in world.flows.posted if m["entry_id"] == "S07" and m["kind"] == "info"), None)
    if message is None:
        return "no Teams message about the large email"
    rid = fakes.request_id("S07")
    for text in ("- 2345678: scan found (2345678.pdf, 2.0 MB)", "- 7777707: not received (Power BI status: Unsigned)",
                 f"Manual%20upload/{rid}", "a Reply All draft without them"):
        if text not in message["message"]:
            return f"Teams message lacks '{text}'"
    if world.mailbox.drafts["S07"]["mode"] != "draft":
        return "the large email's reply was not a draft"
    if not world.notes.get("S07 copy seen"):
        return "the file was never copied for sending by hand, or deleted before the draft was sent"
    if (fakes.UPLOAD_DIR / rid).exists():
        return "the copy was not deleted after the draft was sent"
    if _latest("S07")["state"].get("pilot") not in pilot.SENT:
        return "the sent draft was not recorded"
    return (_outstanding(world, "7777707", "Unsigned")
            or _in_draft(world, "S07", "Please find attached the Proof of Delivery for 2345678"))


def _answered_by_hand(world: World) -> str:
    latest = _latest("S08")
    if "replied manually in Outlook" not in latest["reason"]:
        return f"reason is '{latest['reason']}'"
    if "S08" in world.mailbox.drafts:
        return "a draft was made"
    return "the late Power BI result was left in pbi_results" if _results_left("S08") else ""


def _results_left(entry_id: str) -> list[Path]:
    """Power BI results of this email still in pbi_results, unread or not archived."""
    folder = fakes.SIM_ROOT / "sharepoint" / "handoff" / "pbi_results"
    return list(folder.glob(f"{fakes.request_id(entry_id)}*.json")) if folder.exists() else []


def _late_old_answer(world: World) -> str:
    if not world.notes.get("S15 reprocessed"):
        return "the email was never reprocessed"
    if _results_left("S15"):
        return "the late answer to the first request was left in pbi_results"
    return _outstanding(world, "8888888", "Unsigned")


def _reason_and_no_draft(world: World, entry_id: str, reason: str) -> str:
    latest = _latest(entry_id)
    if latest["reason"] != reason:
        return f"reason is '{latest['reason']}', expected '{reason}'"
    return "a draft was made" if entry_id in world.mailbox.drafts else ""


def _card_failed_once(world: World) -> str:
    return "" if world.notes.get("S11 failed") else "the card was never reported as not reaching Teams"


def _latest(entry_id: str) -> dict:
    return next(event for event in tracking.latest() if event["entry_id"] == entry_id)


def _build(scenarios: list[Scenario]) -> World:
    if fakes.SIM_ROOT.exists():
        shutil.rmtree(fakes.SIM_ROOT)
    fakes.AGENT_DIR.mkdir(parents=True)
    fakes.UPLOAD_DIR.mkdir(parents=True)
    mailbox = fakes.FakeMailbox()
    for number, scenario in enumerate(scenarios):
        for name in scenario.files:
            fakes.write_pdf(fakes.SCAN_ROOT / name if "/" in name else fakes.SCAN_ROOT / "Sep" / name)
        mailbox.attached[scenario.entry_id] = scenario.attached
        if scenario.large:
            fakes.write_pdf(fakes.SCAN_ROOT / "Sep" / scenario.large, 2_000_000)
        mailbox.add({
            "entry_id": scenario.entry_id, "message_class": "IPM.Note", "subject": scenario.subject,
            "sender_email": scenario.sender, "to_recipients": "POD", "body": scenario.body,
            "received_time": f"2026-10-20 {8 + number // 6:02d}:{(number * 7) % 60:02d}:00",
            "message_id": f"<{scenario.entry_id.lower()}@customer.example>",
        })
    fakes.write_pdf(fakes.SCAN_ROOT / "POD" / "Receiver" / "524037 - Signed.pdf")
    flows = fakes.FakeFlows(
        [row for scenario in scenarios for row in scenario.pbi_rows],
        {scenario.entry_id: scenario.answer for scenario in scenarios if scenario.answer},
        {scenario.entry_id: scenario.pbi_delay for scenario in scenarios if scenario.pbi_delay},
    )
    flows.by_request = {fakes.request_id(scenario.entry_id): scenario.entry_id for scenario in scenarios}
    flows.flow3_drops.add("S11")  # Flow 3 does not take S11's cards until it is "fixed"
    flows.stale_first.add("S15")  # Flow 1's first run for S15 is slow and finds nothing
    return World(mailbox, flows)


def _between_rounds(world: World, round_number: int) -> None:
    """What people do between runs: reply by hand, mark resolved, fix Flow 3, review."""
    latest = {event["entry_id"]: event for event in tracking.latest()}
    if round_number == 1:
        world.mailbox.manual["S08"] = {"action": "reply", "at": "2026-10-20T02:30:00+00:00"}
        main.reprocess_power_bi([latest["S15"]])  # the team presses Reprocess in Power BI
        world.notes["S15 reprocessed"] = True
        # Handled by hand in Outlook while waiting: filed, moved, filed.
        world.mailbox.move("S16", "Actioned/Finished")  # a new entry ID: found by its message ID
        world.mailbox.move("S17", "Customers", keep_entry_id=True)
        world.mailbox.move("S18", "Accounts")
    s30 = latest.get("S30")
    if s30 and s30["status"] == tracking.MANUAL_REVIEW and not world.notes.get("S30 reviewed"):
        world.notes["S30 reviewed"] = True
        main.record_review(s30, "POD_REQUEST", "", "")  # POD_REQUEST saved with the number fields empty
    s07 = latest.get("S07")
    if s07 and s07["status"] == tracking.REPLIED and not world.notes.get("S07 sent"):
        # The team attaches the copied file to the agent's draft and sends it.
        copy = fakes.UPLOAD_DIR / fakes.request_id("S07") / "2345678.pdf"
        world.notes["S07 copy seen"] = copy.exists()
        world.notes["S07 sent"] = True
        world.mailbox.send_draft("S07", [copy.name])
    s11 = latest.get("S11")
    if s11 and tracking.card_problems(s11["status"], s11["state"]) and not world.notes.get("S11 failed"):
        world.notes["S11 failed"] = True
        world.flows.flow3_drops.discard("S11")  # Flow 3 fixed; the team presses Send Teams card again
        main.send_cards_again([s11])


def _verdict(scenario: Scenario, world: World) -> tuple[bool, str]:
    latest = _latest(scenario.entry_id)
    problems = []
    if latest["status"] != scenario.expect_status:
        problems.append(f"status {latest['status']} ({latest['reason'] or '-'}), expected {scenario.expect_status}")
    if scenario.expect_files is not None:
        attached = sorted(world.mailbox.drafts.get(scenario.entry_id, {}).get("attachments", []))
        if attached != sorted(scenario.expect_files):
            problems.append(f"draft has {attached or 'no draft'}, expected {sorted(scenario.expect_files)}")
    if scenario.check and not problems:
        if problem := scenario.check(world):
            problems.append(problem)
    shown = f"{latest['status']}" + (f": {latest['reason']}" if latest["reason"] else "")
    return not problems, "; ".join(problems) or shown


def run() -> bool:
    fakes.apply_settings()
    fakes.use_fake_models()
    main.CARD_RESEND_AFTER = timedelta(seconds=1)  # 30 minutes, made short for the simulation
    scenarios = _scenarios()
    world = _build(scenarios)
    world.mailbox.install()
    for round_number in range(1, ROUNDS + 1):
        main.run(process_new=True, resume=True)
        world.flows.run(round_number)
        _between_rounds(world, round_number)
        time.sleep(ROUND_PAUSE)

    lines, passed = [], 0
    for scenario in scenarios:
        ok, text = _verdict(scenario, world)
        passed += ok
        lines.append(f"{'PASS' if ok else 'FAIL'}  {scenario.entry_id}  {scenario.title}\n      {text}")
    lines += [
        "",
        f"{passed} of {len(scenarios)} scenarios passed.",
        f"Teams messages and cards posted: {len(world.flows.posted)}",
        f"POD Outstanding rows: {', '.join(f'{n} ({r.get('Status')})' for n, r in world.flows.outstanding.items()) or 'none'}",
        f"Simulated data: {fakes.SIM_ROOT}",
    ]
    report = "\n".join(lines)
    print(report)
    (fakes.SIM_ROOT / "report.txt").write_text(report + "\n", encoding="utf-8")
    return passed == len(scenarios)


def main_() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dashboard", action="store_true", help="open the dashboard on the simulated data afterwards")
    args = parser.parse_args()
    ok = run()
    if args.dashboard:
        # A month of demo emails, labels and trained models, so every chart has data (sim/demo.py).
        subprocess.run([sys.executable, "-m", "sim.demo"], cwd=Path(__file__).resolve().parent.parent)
        print("Opening the dashboard on the simulated data (Ctrl+C to stop)...")
        subprocess.run([sys.executable, "-m", "streamlit", "run", str(Path(__file__).with_name("sim_dashboard.py"))],
                       cwd=Path(__file__).resolve().parent.parent)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main_()
