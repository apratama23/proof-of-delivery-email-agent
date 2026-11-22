"""The figures behind the dashboard Overview."""

import unittest

import pandas as pd

from app import dashboard_data as dd
from app import tracking

NOW = pd.Timestamp("2026-09-29 12:00", tz="UTC")


def _event(event_id, entry_id, status, at, state=None, numbers="", sender="a@example1.com"):
    return {
        "id": event_id, "entry_id": entry_id, "status": status, "at_utc": at,
        "reason": "", "sender_email": sender, "subject": "s", "numbers": numbers,
        "intent": "", "state": state or {},
    }


EVENTS = [
    # E1: waited for Power BI, then replied with a file and a not-received number.
    _event(1, "E1", tracking.WAITING_PBI, "2026-09-29T08:00:00+00:00", numbers="1;2"),
    _event(2, "E1", tracking.REPLIED, "2026-09-29T08:05:00+00:00",
           {"found": {"1": "a.pdf"}, "not_received": {"2": {}}}, "1;2"),
    # E2: not received only.
    _event(3, "E2", tracking.REPLIED, "2026-09-28T09:00:00+00:00", {"not_received": {"3": {}}}, "3", "b@example2.com"),
    # E3: held with an unknown number, two days old.
    _event(4, "E3", tracking.HELD, "2026-09-27T11:00:00+00:00", {"found": {"4": "x"}, "unknown": ["5"]}, "4;5"),
    # E4: not a POD request.
    _event(5, "E4", tracking.SKIPPED_NO_NUMBER, "2026-09-29T07:00:00+00:00"),
    # E5: held, then resolved by a person.
    _event(6, "E5", tracking.HELD, "2026-09-20T07:00:00+00:00", numbers="6"),
    _event(7, "E5", tracking.RESOLVED, "2026-09-21T07:00:00+00:00", numbers="6"),
]


class DashboardDataTest(unittest.TestCase):
    def setUp(self):
        self.table = dd.emails(EVENTS)

    def test_one_row_per_email_with_its_latest_status_and_first_time(self):
        self.assertEqual(len(self.table), 5)
        e1 = self.table.set_index("entry_id").loc["E1"]
        self.assertEqual(e1["status"], tracking.REPLIED)
        self.assertEqual(e1["first_seen"], pd.Timestamp("2026-09-29T08:00:00+00:00"))

    def test_waiting_team_email_exposes_teams_request_ids(self):
        event = _event(
            10, "E10", tracking.WAITING_TEAM, "2026-09-29T08:00:00+00:00",
            {"cards": {"1234567": "a1b2c3d4e5f60718-0", "2345678": "a1b2c3d4e5f60718-1"}},
        )

        row = dd.emails([event]).iloc[0]

        self.assertEqual(
            row["teams_request_ids"],
            "a1b2c3d4e5f60718-0, a1b2c3d4e5f60718-1",
        )

    def test_a_held_email_shows_its_correction_card_and_a_card_that_never_arrived(self):
        held = _event(11, "E11", tracking.HELD, "2026-09-29T08:00:00+00:00",
                      {"correction_card": "a1-h3", "card_delivery": {"correction": {"failed": "Flow 3 did not take it"}}})
        row = dd.emails([held]).iloc[0]
        self.assertEqual((row["teams_request_ids"], row["teams_problem"]), ("a1-h3", "correction: Flow 3 did not take it"))

    def test_a_manual_review_email_shows_its_review_card(self):
        queued = _event(12, "E12", tracking.MANUAL_REVIEW, "2026-09-29T08:00:00+00:00",
                        {"review_card": "a1-m1", "card_delivery": {"review": {"failed": "Flow 3 did not take it"}}})
        row = dd.emails([queued]).iloc[0]
        self.assertEqual((row["teams_request_ids"], row["teams_problem"]), ("a1-m1", "review: Flow 3 did not take it"))

    def test_email_date_is_the_received_time_as_outlook_shows_it(self):
        # Outlook's local time, sometimes labelled UTC by pywin32: digits kept as they are.
        self.assertEqual(dd.email_date("2026-09-16 12:40:41.074000+00:00"), pd.Timestamp("2026-09-16 12:40:41.074"))
        self.assertEqual(dd.email_date("2026-09-16 12:40:41"), pd.Timestamp("2026-09-16 12:40:41"))
        self.assertTrue(pd.isna(dd.email_date("")))
        events = [{**EVENTS[0], "received_time": "2026-09-28 17:05:00+00:00"}]
        received = dd.emails(events).iloc[0]["received"]
        self.assertEqual(received, pd.Timestamp("2026-09-28 17:05"))
        self.assertTrue(pd.isna(self.table.iloc[0]["received"]))  # events without a received time

    def test_history_filters_every_processed_email_newest_first(self):
        events = [
            {**EVENTS[0], "received_time": "2026-09-29 08:00:00"},
            {**EVENTS[2], "received_time": "2026-09-28 09:00:00"},
            {**EVENTS[4], "received_time": "2026-09-29 07:00:00"},
        ]
        table = dd.emails(events)
        self.assertEqual(list(dd.history(table)["entry_id"]), ["E1", "E4", "E2"])
        self.assertEqual(list(dd.history(table, statuses=[tracking.SKIPPED_NO_NUMBER])["entry_id"]), ["E4"])
        self.assertEqual(list(dd.history(table, outcomes=[dd.NOT_RECEIVED])["entry_id"]), ["E2"])
        self.assertEqual(list(dd.history(table, sender="example2")["entry_id"]), ["E2"])
        day = pd.Timestamp("2026-09-29")
        self.assertEqual(list(dd.history(table, start=day, end=day)["entry_id"]), ["E1", "E4"])

    def test_history_filters_by_intent_including_emails_without_one(self):
        table = self.table.copy()
        table.loc[table["entry_id"] == "E1", "intent"] = "POD_REQUEST"
        table.loc[table["entry_id"] == "E2", "intent"] = "OTHER"

        self.assertEqual(list(dd.history(table, intents=["POD_REQUEST"])["entry_id"]), ["E1"])
        self.assertCountEqual(dd.history(table, intents=[""])["entry_id"], ["E4", "E3", "E5"])

    def test_guard_emails_are_not_offered_for_training(self):
        self.assertFalse(dd.can_train_on([tracking.SKIPPED_GUARD]))
        self.assertTrue(dd.can_train_on([tracking.SKIPPED_NO_NUMBER]))
        self.assertTrue(dd.can_train_on([tracking.WAITING_PBI, tracking.REPLIED]))

    def test_times_are_shown_in_local_time_whatever_the_pc_zone(self):
        # 20:00 UTC is 04:00 the next day in local time (UTC+8, no daylight saving).
        shown = dd._local("2026-10-19T20:00:00+00:00")
        self.assertEqual((shown.day, shown.hour, shown.utcoffset().total_seconds()), (20, 4, 8 * 3600))
        self.assertEqual(dd.local_now().utcoffset().total_seconds(), 8 * 3600)

    def test_an_email_moved_by_the_agent_is_opened_with_its_new_id(self):
        events = [{"state": {}}, {"state": {"moved_to": "Actioned/Finished", "moved_entry_id": "NEW"}}]
        self.assertEqual(dd.current_entry_id("OLD", events), "NEW")
        self.assertEqual(dd.current_entry_id("OLD", [{"state": {}}]), "OLD")

    def test_outcomes(self):
        outcomes = self.table.set_index("entry_id")["outcome"].to_dict()
        self.assertEqual(outcomes, {
            "E1": dd.FILES_SENT, "E2": dd.NOT_RECEIVED, "E3": dd.WAITING,
            "E4": dd.NOT_POD, "E5": dd.HANDLED,
        })

    def test_kpis_count_only_the_period(self):
        today = dd.kpis(self.table, pd.Timestamp("2026-09-29T00:00:00+00:00"))
        self.assertEqual(today, {
            "Emails received": 2, "POD requests": 1, "Replied with files": 1,
            "Replies with a POD missing": 1, "Held for the team": 0, "Manual Review": 0,
        })
        week = dd.kpis(self.table, pd.Timestamp("2026-09-23T00:00:00+00:00"))
        self.assertEqual(week["Emails received"], 4)
        self.assertEqual(week["Held for the team"], 1)

    def test_daily_volume_has_every_day_and_outcome(self):
        volume = dd.daily_volume(self.table, days=30, now=NOW)
        self.assertEqual(len(volume), 30 * len(dd.OUTCOMES))
        self.assertEqual(int(volume["emails"].sum()), 5)

    def test_number_outcomes_and_weekly_hit_rate(self):
        numbers = dd.number_outcomes(self.table)
        by_number = dict(zip(numbers["number"], numbers["outcome"]))
        self.assertEqual(by_number, {
            "1": dd.NUMBER_FOUND, "2": dd.NUMBER_NOT_RECEIVED, "3": dd.NUMBER_NOT_RECEIVED,
            "4": dd.NUMBER_FOUND, "5": dd.NUMBER_UNKNOWN, "6": dd.NUMBER_HANDLED,  # E5 was resolved
        })
        rates = dd.weekly_hit_rate(numbers)
        # Week of 28-29 Sep (Mon 28): 1 and 3 decided... found 1 of 3 (1, 2, 3).
        self.assertAlmostEqual(rates.iloc[-1]["hit_rate"], 1 / 3)
        self.assertEqual(int(rates["numbers"].sum()), 5)  # "6" has no result: handled by the team

    def test_every_number_of_a_finished_email_has_its_outcome(self):
        # D-052: not in our system, a reference attached as another number's scan, an
        # unneeded attachment number, and an email the model called OTHER.
        table = dd.emails([
            _event(1, "F1", tracking.REPLIED, "2026-09-29T08:00:00+00:00",
                   {"found": {"1": "a.pdf"}, "same_as": {"80600001": "1"}, "not_in_system": ["7"]}, "1;80600001;7"),
            _event(2, "F2", tracking.REPLIED, "2026-09-29T08:00:00+00:00",
                   {"found": {"2": "b.pdf"}, "number_source": "picture"}, "2;9"),
            {**_event(3, "F3", tracking.MANUAL_REVIEW, "2026-09-29T08:00:00+00:00", numbers="3"), "intent": "OTHER"},
        ])
        numbers = dd.number_outcomes(table)
        self.assertEqual(dict(zip(numbers["number"], numbers["outcome"])), {
            "1": dd.NUMBER_FOUND, "80600001": dd.NUMBER_FOUND, "7": dd.NUMBER_UNKNOWN, "2": dd.NUMBER_FOUND,
        })  # "9" was not needed; F3 may not be a POD request
        self.assertEqual(dd.kpis(table, NOW - pd.Timedelta(days=1)), {
            "Emails received": 3, "POD requests": 2, "Replied with files": 2,
            "Replies with a POD missing": 1, "Held for the team": 0, "Manual Review": 1,
        })
        self.assertEqual(dd.top_domains(table)["requests"].tolist(), [2])

    def test_the_overview_waiting_chart_leaves_other_emails_out(self):
        table = dd.emails([_event(1, "G1", tracking.OTHER_INTENT, "2026-09-29T08:00:00+00:00", numbers="1"),
                           _event(2, "G2", tracking.HELD, "2026-09-29T08:00:00+00:00", numbers="2")])
        self.assertEqual(dd.queue(table, now=NOW)["entry_id"].tolist(), ["G1", "G2"])  # the Review queue
        self.assertEqual(dd.queue(table, now=NOW, other_intent=False)["entry_id"].tolist(), ["G2"])

    def test_queue_holds_open_emails_with_their_age(self):
        waiting = dd.queue(self.table, now=NOW)
        self.assertEqual(waiting["entry_id"].tolist(), ["E3"])
        self.assertEqual(waiting.iloc[0]["age"], "1-3 days")
        ages = dd.queue_age(waiting)
        self.assertEqual(ages["age"].tolist(), list(dd.AGE_BUCKETS))
        self.assertEqual(int(ages["emails"].sum()), 1)

    def test_top_domains_counts_pod_requests_only(self):
        domains = dd.top_domains(self.table)
        self.assertEqual(dict(zip(domains["domain"], domains["requests"])), {"example1.com": 3, "example2.com": 1})

    def test_system_agreement_uses_reviewed_rows_only(self):
        training = pd.DataFrame({
            "system_pulled_at": ["2026-09-29T01:00:00+00:00"] * 3 + [""],
            "label_source": ["Manual", "Manual", "System decision", "Manual"],
            "intent_label": ["POD_REQUEST", "OTHER", "", "OTHER"],
            "intent_label_pseudo": ["POD_REQUEST", "POD_REQUEST", "OTHER", "OTHER"],
        })
        agreement = dd.system_agreement(training)
        self.assertEqual(len(agreement), 1)
        self.assertEqual(agreement.iloc[0]["agreement"], 0.5)
        self.assertEqual(agreement.iloc[0]["reviewed"], 2)

    def test_empty_inputs_give_empty_figures(self):
        empty = dd.emails([])
        self.assertTrue(empty.empty)
        self.assertEqual(sum(dd.kpis(empty, NOW).values()), 0)
        self.assertEqual(int(dd.daily_volume(empty, now=NOW)["emails"].sum()), 0)
        self.assertTrue(dd.number_outcomes(empty).empty)
        self.assertTrue(dd.weekly_hit_rate(dd.number_outcomes(empty)).empty)
        self.assertTrue(dd.queue(empty, now=NOW).empty)
        self.assertEqual(int(dd.queue_age(dd.queue(empty, now=NOW))["emails"].sum()), 0)
        self.assertTrue(dd.top_domains(empty).empty)
        self.assertTrue(dd.system_agreement(pd.DataFrame()).empty)


if __name__ == "__main__":
    unittest.main()
