"""Replay synthetic approval workloads with a SimPy reviewer pool."""

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from random import Random

import simpy


REVIEWER_ROLE = "approver"
RETRY_DELAY = 5


@dataclass(frozen=True)
class Request:
    id: str
    arrival: int
    deadline: int
    review_minutes: int
    revise_after_review: bool = False


@dataclass(frozen=True)
class Reviewer:
    id: str
    available_from: int = 0
    role: str = REVIEWER_ROLE


@dataclass(frozen=True)
class Approval:
    reviewer: Reviewer
    evidence_version: int


@dataclass(frozen=True)
class Scenario:
    name: str
    requests: tuple[Request, ...]
    reviewers: tuple[Reviewer, ...]
    dependency_outage_until: int = 0


@dataclass(frozen=True)
class Summary:
    requests: int
    completed: int
    expired: int
    policy_blocks: int
    review_minutes: int
    retries: int

    @property
    def deadline_violation_rate(self):
        return self.expired / self.requests if self.requests else 0.0


def simulate(scenario: Scenario) -> list[dict]:
    env = simpy.Environment()
    pool = simpy.Store(env)
    events = []

    def record(event, request_id=None, **details):
        events.append(
            {
                "scenario": scenario.name,
                "minute": env.now,
                "event": event,
                "request": request_id,
                **details,
            }
        )

    def start_shift(reviewer):
        yield env.timeout(reviewer.available_from)
        record("reviewer_available", reviewer=reviewer.id, role=reviewer.role)
        yield pool.put(reviewer)

    def handle(request):
        yield env.timeout(request.arrival)
        evidence_version = 1
        record("arrived", request.id, deadline=request.deadline, evidence=evidence_version)

        with pool.get() as slot:
            yield slot | env.timeout(request.deadline - env.now)
            if not slot.triggered:
                record("expired", request.id, stage="queue")
                return
            reviewer = slot.value

        try:
            if env.now >= request.deadline:
                record("expired", request.id, stage="queue")
                return
            review_started = env.now
            record("assigned", request.id, reviewer=reviewer.id)
            yield env.timeout(min(request.review_minutes, request.deadline - env.now))
            record(
                "review_ended",
                request.id,
                reviewer=reviewer.id,
                review_minutes=env.now - review_started,
            )
            if env.now >= request.deadline:
                record("expired", request.id, stage="review")
                return
            approval = Approval(reviewer, evidence_version)
            record("reviewed", request.id, reviewer=reviewer.id, evidence=evidence_version)
        finally:
            pool.put(reviewer)

        if request.revise_after_review:
            evidence_version += 1
            record("evidence_changed", request.id, evidence=evidence_version)

        for attempt, delay in enumerate((1, RETRY_DELAY), start=1):
            yield env.timeout(min(delay, request.deadline - env.now))
            if env.now >= request.deadline:
                record("expired", request.id, stage="completion")
                return
            if approval.reviewer.role != REVIEWER_ROLE:
                record("policy_blocked", request.id, reason="ineligible_reviewer")
                return
            if approval.evidence_version != evidence_version:
                record("policy_blocked", request.id, reason="stale_evidence")
                return
            if attempt == 2:
                record("retry_attempted", request.id)
            if env.now >= scenario.dependency_outage_until:
                record("completed", request.id, attempt=attempt, evidence=evidence_version)
                return
            record("dependency_unavailable", request.id, attempt=attempt)
            if attempt == 1:
                record("retry_scheduled", request.id, minute_due=env.now + RETRY_DELAY)

        record("dependency_blocked", request.id)

    for reviewer in scenario.reviewers:
        env.process(start_shift(reviewer))
    for request in scenario.requests:
        env.process(handle(request))
    env.run()
    return events


def summarize(events: list[dict]) -> Summary:
    counts = {event: 0 for event in ("arrived", "completed", "expired", "policy_blocked")}
    review_minutes = 0
    retries = 0
    for entry in events:
        event = entry["event"]
        if event in counts:
            counts[event] += 1
        elif event == "review_ended":
            review_minutes += entry["review_minutes"]
        elif event == "retry_attempted":
            retries += 1
    return Summary(
        counts["arrived"],
        counts["completed"],
        counts["expired"],
        counts["policy_blocked"],
        review_minutes,
        retries,
    )


def scenarios(seed: int) -> tuple[Scenario, ...]:
    rng = Random(seed)
    review_times = [rng.randint(3, 5) for _ in range(8)]
    steady = tuple(
        Request(f"R{index + 1}", index * 4, index * 4 + 15, minutes)
        for index, minutes in enumerate(review_times)
    )
    burst = tuple(
        Request(request.id, 0, 15, request.review_minutes) for request in steady
    )
    full_staff = (Reviewer("A"), Reviewer("B"))
    return (
        Scenario("Steady demand", steady, full_staff),
        Scenario("Burst, reviewer absent", burst, (Reviewer("A"), Reviewer("B", 10)), 9),
        Scenario("Burst, full staffing", burst, full_staff, 9),
        Scenario(
            "Stale approval fixture",
            (Request("S1", 0, 15, 3, revise_after_review=True),),
            full_staff,
        ),
    )


def _check_fields(value, location, required, optional):
    if not isinstance(value, dict):
        raise ValueError(f"{location} must be an object")
    missing = required - value.keys()
    if missing:
        raise ValueError(f"{location} is missing {', '.join(sorted(missing))}")
    unknown = value.keys() - required - optional
    if unknown:
        raise ValueError(f"{location} has unknown fields: {', '.join(sorted(unknown))}")


def _text(value, location):
    if not isinstance(value, str) or not value.strip() or not value.isprintable():
        raise ValueError(f"{location} must be nonempty printable text")
    return value


def _minute(value, location, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{location} must be an integer greater than or equal to {minimum}")
    return value


def _records(values, location, parse, identity):
    if not isinstance(values, list):
        raise ValueError(f"{location} must be an array")
    seen = set()
    records = []
    for index, value in enumerate(values):
        item_location = f"{location}[{index}]"
        record = parse(value, item_location)
        key = getattr(record, identity)
        if key in seen:
            raise ValueError(f"{item_location}.{identity} is duplicated: {key!r}")
        seen.add(key)
        records.append(record)
    return tuple(records)


def _request(value, location):
    _check_fields(
        value,
        location,
        {"id", "arrival", "deadline", "review_minutes"},
        {"revise_after_review"},
    )
    arrival = _minute(value["arrival"], f"{location}.arrival")
    deadline = _minute(value["deadline"], f"{location}.deadline")
    if deadline <= arrival:
        raise ValueError(f"{location}.deadline must be after arrival")
    revise = value.get("revise_after_review", False)
    if type(revise) is not bool:
        raise ValueError(f"{location}.revise_after_review must be a boolean")
    return Request(
        _text(value["id"], f"{location}.id"),
        arrival,
        deadline,
        _minute(value["review_minutes"], f"{location}.review_minutes", minimum=1),
        revise,
    )


def _reviewer(value, location):
    _check_fields(value, location, {"id"}, {"available_from", "role"})
    return Reviewer(
        _text(value["id"], f"{location}.id"),
        _minute(value.get("available_from", 0), f"{location}.available_from"),
        _text(value.get("role", REVIEWER_ROLE), f"{location}.role"),
    )


def _scenario(value, location):
    _check_fields(value, location, {"name", "requests", "reviewers"}, {"dependency_outage_until"})
    return Scenario(
        _text(value["name"], f"{location}.name"),
        _records(value["requests"], f"{location}.requests", _request, "id"),
        _records(value["reviewers"], f"{location}.reviewers", _reviewer, "id"),
        _minute(value.get("dependency_outage_until", 0), f"{location}.dependency_outage_until"),
    )


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key!r}")
        result[key] = value
    return result


def load_scenarios(path: Path) -> tuple[Scenario, ...]:
    """Read and validate all scenarios before starting a simulation."""
    data = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_json_object)
    loaded = _records(data, "scenarios", _scenario, "name")
    if not loaded:
        raise ValueError("scenarios must contain at least one scenario")
    return loaded


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--seed", default=42, type=int, help="Seed for built-in review durations")
    source.add_argument("--scenario-file", type=Path, help="Run the scenarios from a JSON file")
    parser.add_argument("--events", action="store_true", help="Print JSON Lines audit events")
    args = parser.parse_args()

    if args.scenario_file is None:
        selected = scenarios(args.seed)
        title = f"ApprovalStorm seed {args.seed}"
    else:
        try:
            selected = load_scenarios(args.scenario_file)
        except (OSError, ValueError) as error:
            parser.error(f"{args.scenario_file}: {error}")
        title = "ApprovalStorm scenario comparison"

    if not args.events:
        width = max(24, max(len(scenario.name) for scenario in selected))
        print(title)
        print(
            f"{'Scenario':<{width}} {'Requests':>8} {'Completed':>9} {'Deadline misses':>15}"
            f" {'Review minutes':>14} {'Policy blocks':>13} {'Retries':>7}"
        )
    for scenario in selected:
        events = simulate(scenario)
        if args.events:
            for event in events:
                print(json.dumps(event))
            continue
        summary = summarize(events)
        print(
            f"{scenario.name:<{width}} {summary.requests:>8} {summary.completed:>9}"
            f" {summary.deadline_violation_rate:>15.1%} {summary.review_minutes:>14}"
            f" {summary.policy_blocks:>13} {summary.retries:>7}"
        )


if __name__ == "__main__":
    main()
