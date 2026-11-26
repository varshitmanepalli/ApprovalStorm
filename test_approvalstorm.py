import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from approvalstorm import Request, Reviewer, Scenario, load_scenarios, scenarios, simulate, summarize


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


class ScenarioFileTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "scenarios.json"
        self.request = {"id": "R1", "arrival": 0, "deadline": 11, "review_minutes": 3}
        self.scenario = {"name": "Custom", "requests": [self.request], "reviewers": [{"id": "A"}]}

    def write(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8-sig")

    def test_loads_utf8_bom_and_applies_defaults(self):
        self.write([self.scenario])
        (loaded,) = load_scenarios(self.path)
        self.assertEqual(loaded, Scenario("Custom", (Request("R1", 0, 11, 3),), (Reviewer("A"),)))
        self.assertEqual(summarize(simulate(loaded)).completed, 1)

    def test_example_compares_identical_workloads(self):
        example = Path(__file__).with_name("examples") / "staffing.json"
        delayed, staffed = load_scenarios(example)
        self.assertEqual(delayed.requests, staffed.requests)
        self.assertEqual(delayed.dependency_outage_until, staffed.dependency_outage_until)
        self.assertEqual(summarize(simulate(delayed)).completed, 1)
        self.assertEqual(summarize(simulate(staffed)).completed, 2)

    def test_rejects_invalid_request_fields_with_their_location(self):
        cases = (
            ({"deadline": 0}, "deadline must be after arrival"),
            ({"arrival": True}, "arrival must be an integer"),
            ({"review_minutes": 0}, "review_minutes must be an integer"),
            ({"revise_after_review": 1}, "revise_after_review must be a boolean"),
            ({"duration": 4}, "has unknown fields: duration"),
        )
        for fields, message in cases:
            with self.subTest(fields=fields):
                self.write([{**self.scenario, "requests": [{**self.request, **fields}]}])
                with self.assertRaisesRegex(ValueError, rf"scenarios\[0\]\.requests\[0\].*{message}"):
                    load_scenarios(self.path)

    def test_rejects_duplicate_ids_names_and_object_keys(self):
        cases = (
            ([{**self.scenario, "requests": [self.request, self.request]}], "id is duplicated"),
            ([{**self.scenario, "reviewers": [{"id": "A"}, {"id": "A"}]}], "id is duplicated"),
            ([self.scenario, self.scenario], "name is duplicated"),
        )
        for data, message in cases:
            with self.subTest(data=data):
                self.write(data)
                with self.assertRaisesRegex(ValueError, message):
                    load_scenarios(self.path)
        self.path.write_text('[{"name": "first", "name": "second"}]', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate JSON field"):
            load_scenarios(self.path)

    def test_allows_scenarios_without_reviewers_or_requests(self):
        self.write([
            {**self.scenario, "reviewers": []},
            {**self.scenario, "name": "Empty workload", "requests": []},
        ])
        unstaffed, empty = load_scenarios(self.path)
        self.assertEqual(summarize(simulate(unstaffed)).expired, 1)
        self.assertEqual(summarize(simulate(empty)).requests, 0)
