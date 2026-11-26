import unittest

from approvalstorm import Request, Reviewer, Scenario, scenarios, simulate, summarize


class SimulationTests(unittest.TestCase):
    def test_seed_replays_and_staffing_changes_completions(self):
        steady, absent, staffed, _ = scenarios(42)
        replay = scenarios(42)[1]
        self.assertEqual(simulate(absent), simulate(replay))
        self.assertEqual(
            [request.review_minutes for request in steady.requests],
            [request.review_minutes for request in staffed.requests],
        )
        self.assertEqual(absent.requests, staffed.requests)
        self.assertEqual(absent.dependency_outage_until, staffed.dependency_outage_until)
        baseline = summarize(simulate(steady))
        self.assertEqual(baseline.completed, baseline.requests)
        absent_events = simulate(absent)
        self.assertTrue(
            all(
                entry["minute"] >= 10
                for entry in absent_events
                if entry["event"] == "assigned" and entry["reviewer"] == "B"
            )
        )
        self.assertGreater(
            summarize(simulate(staffed)).completed, summarize(absent_events).completed
        )

    def test_stale_evidence_blocks_the_completion(self):
        events = simulate(scenarios(42)[-1])
        approval = next(entry for entry in events if entry["event"] == "reviewed")
        revision = next(entry for entry in events if entry["event"] == "evidence_changed")
        block = next(entry for entry in events if entry["event"] == "policy_blocked")
        self.assertEqual(revision["evidence"], approval["evidence"] + 1)
        self.assertEqual(block["reason"], "stale_evidence")
        self.assertEqual(summarize(events).completed, 0)

    def test_ineligible_reviewer_blocks_the_completion(self):
        scenario = Scenario(
            "Role check", (Request("R1", 0, 15, 3),), (Reviewer("A", role="observer"),)
        )
        events = simulate(scenario)
        self.assertEqual(summarize(events).policy_blocks, 1)
        self.assertEqual(summarize(events).completed, 0)
        self.assertIn(
            "ineligible_reviewer",
            [entry["reason"] for entry in events if entry["event"] == "policy_blocked"],
        )

    def test_dependency_retry_obeys_deadline_and_stops_after_two_attempts(self):
        cases = (
            (9, 15, "completed", 1),
            (10, 15, "dependency_blocked", 1),
            (9, 9, "expired", 0),
        )
        for outage_until, deadline, terminal_event, retries in cases:
            with self.subTest(outage_until=outage_until, deadline=deadline):
                scenario = Scenario(
                    "Retry",
                    (Request("R1", 0, deadline, 3),),
                    (Reviewer("A"),),
                    outage_until,
                )
                events = simulate(scenario)
                self.assertEqual(events[-1]["event"], terminal_event)
                self.assertEqual(summarize(events).retries, retries)
                self.assertEqual(sum(entry["event"] == "reviewed" for entry in events), 1)

    def test_queue_expiry_returns_reviewer_for_next_request(self):
        scenario = Scenario(
            "Queue deadline",
            (Request("R1", 0, 3, 4), Request("R2", 0, 3, 1), Request("R3", 3, 8, 1)),
            (Reviewer("A"),),
        )
        events = simulate(scenario)
        expired = [
            (entry["request"], entry["stage"])
            for entry in events
            if entry["event"] == "expired"
        ]
        completed = [entry["request"] for entry in events if entry["event"] == "completed"]
        self.assertCountEqual(expired, [("R1", "review"), ("R2", "queue")])
        self.assertEqual(completed, ["R3"])
        self.assertEqual(summarize(events).review_minutes, 4)
