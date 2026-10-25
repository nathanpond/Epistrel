"""P-04's open-loop driver (#76): one Locust user per seeded story, submitting ``respond`` on a
fixed schedule whatever the engine does.

Story ``i`` submits tick ``k`` at ``start + offset_i + k * interval`` (the offsets spread the ten
stories evenly over one interval), until submissions stop 2 minutes before the end. Each
submission runs in its own greenlet, so a slow engine builds a backlog instead of slowing the
arrivals: ``POST /stories/{id}/respond`` with ``Prefer: respond-async`` and the
``Idempotency-Key`` ``<story>-<tick>``, the tick embedded in ``say`` (``[tick k]``) so the run can
be reconciled against ``history``. A 200 is a turn that played at once; a 202 is queued, and its
``Location`` (``GET /operations/{id}?viewpoint=…``) is polled every 500 ms until it settles.
Anything else (a 423 ``queue_full`` included) is a failed request.

Every turn is written to ``EPISTREL_PERF_RECORDS`` as JSON lines: a ``submit`` line when it is
sent and a ``finish`` line when it settles, with story, tick, times (seconds since the run's
start), outcome, HTTP status and, for a committed turn, ``elapsed_ms``, ``queued_ms``,
``first_call_offset_ms`` and the model calls' summed latency. A turn with no ``finish`` line
never settled.
Configured by the orchestrator (``perf.lite_smoke.__main__``) through ``EPISTREL_PERF_*``.
"""

from __future__ import annotations

import itertools
import json
import os
import time
from typing import Any

import gevent
from locust import User, constant, task
from locust.env import Environment

from perf.lite_smoke import INTERVAL_S, POLL_S, Target

STORIES_FILE = os.environ.get("EPISTREL_PERF_STORIES", "build/perf/stories.json")
RECORDS_FILE = os.environ.get("EPISTREL_PERF_RECORDS", "build/perf/records.jsonl")
INTERVAL = float(os.environ.get("EPISTREL_PERF_INTERVAL", str(INTERVAL_S)))
START = float(os.environ.get("EPISTREL_PERF_START", str(time.time())))
"""The run's start (epoch seconds), shared with the orchestrator's sampler."""
SUBMIT_UNTIL = float(os.environ.get("EPISTREL_PERF_SUBMIT_UNTIL", "1680"))
"""Seconds after ``START`` past which no turn is submitted."""
SCENARIO = os.environ.get("EPISTREL_PERF_SCENARIO", "lite")
"""``structured`` (#153): each turn names one render character, the story's second
(``other_id``): a single-character structured turn. ``multi`` (#182): each turn names the
story's two primaries (``primary_ids``), rendered ``sequential``ly in that order."""
PLANNING = ("reduced_characters", "deferred_characters", "escalation_deferred")
SETTLED = frozenset({"committed", "failed", "abandoned"})

_stories: list[dict[str, Any]] = json.loads(open(STORIES_FILE).read())  # noqa: SIM115
_next = itertools.count()
_records = open(RECORDS_FILE, "a", buffering=1)  # noqa: SIM115 - line-buffered log


def _now() -> float:
    return time.time() - START


def _write(record: dict[str, Any]) -> None:
    _records.write(json.dumps(record) + "\n")


class StoryPlayer(User):
    """One story's schedule; ``host`` is the engine's base URL."""

    wait_time = constant(0)

    def __init__(self, environment: Environment) -> None:
        super().__init__(environment)
        self.target = Target.from_env()
        self.story = _stories[next(_next) % len(_stories)]
        from locust.clients import HttpSession

        self.client = HttpSession(
            base_url=self.host or self.target.base_url,
            request_event=environment.events.request,
            user=self,
        )

    @task
    def play(self) -> None:
        """Submit every tick of the schedule, then idle until the run stops."""
        index = int(self.story["index"])
        offset = INTERVAL * index / max(1, len(_stories))
        for tick in itertools.count():
            due = offset + tick * INTERVAL
            if due > SUBMIT_UNTIL:
                break
            gevent.sleep(max(0.0, due - _now()))
            gevent.spawn(self._turn, tick, due)
        while True:
            gevent.sleep(60)

    def _turn(self, tick: int, due: float) -> None:
        story_id = self.story["story_id"]
        viewpoint = f"character:{self.story['player_id']}"
        record: dict[str, Any] = {
            "story_id": story_id,
            "index": self.story["index"],
            "tick": tick,
            "due": round(due, 3),
            "submitted": round(_now(), 3),
        }
        _write({**record, "event": "submit"})
        body: dict[str, Any] = {
            "actor_character_id": self.story["player_id"],
            "player_input": {"say": f"[tick {tick}] I set the lantern on the table and wait."},
        }
        if SCENARIO == "structured":
            body["render_character_ids"] = [self.story["other_id"]]
        elif SCENARIO == "multi":
            body["render_character_ids"] = list(self.story["primary_ids"])
            body["interaction_mode"] = "sequential"
        headers = {
            **self.target.headers(),
            "Prefer": "respond-async",
            "Idempotency-Key": f"{story_id}-{tick}",
        }
        try:
            answer = self.client.post(
                f"/stories/{story_id}/respond", json=body, headers=headers, name="respond"
            )
            record["status"] = answer.status_code
            if answer.status_code == 200:
                self._finish(record, "committed", answer.json())
            elif answer.status_code == 202:
                record["operation_id"] = answer.json().get("operation_id")
                self._poll(record, viewpoint)
            else:
                record["error"] = _error_code(answer)
                self._finish(record, "failed_request", None)
        except Exception as exc:  # the record says what failed; the run goes on
            record["error"] = type(exc).__name__
            self._finish(record, "failed_request", None)

    def _poll(self, record: dict[str, Any], viewpoint: str) -> None:
        operation_id = record["operation_id"]
        while True:
            gevent.sleep(POLL_S)
            answer = self.client.get(
                f"/operations/{operation_id}",
                params={"viewpoint": viewpoint},
                headers=self.target.headers(),
                name="poll",
            )
            if answer.status_code != 200:
                record["status"] = answer.status_code
                record["error"] = _error_code(answer)
                self._finish(record, "failed_request", None)
                return
            view = answer.json()
            if view["status"] not in SETTLED:
                continue
            if view["status"] != "committed":
                record["error"] = view["status"]
                self._finish(record, "failed_turn", None)
                return
            result = (view.get("result") or {}).get("body")
            if not isinstance(result, dict):
                result = {
                    "queued_ms": view.get("queued_ms"),
                    "first_call_offset_ms": view.get("first_call_offset_ms"),
                }
            self._finish(record, "committed", result)
            return

    def _finish(self, record: dict[str, Any], outcome: str, result: dict[str, Any] | None) -> None:
        record["event"] = "finish"
        record["finished"] = round(_now(), 3)
        record["outcome"] = outcome
        if result is not None:
            calls = result.get("model_calls") or []
            record["elapsed_ms"] = result.get("elapsed_ms")
            record["queued_ms"] = result.get("queued_ms") or 0
            record["first_call_offset_ms"] = result.get("first_call_offset_ms")
            record["model_ms"] = sum(int(c.get("latency_ms", 0)) for c in calls)
            record["turn"] = result.get("turn")
            if SCENARIO == "multi":
                record.update(_multi(self.story, result))
        _write(record)


def _multi(story: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """A multi-character turn's figures (#182): each primary's beats in the viewpoint's result,
    the budget-planning entries it reported, its degradation steps, the profile it ran under
    and the perception level its beats were filtered at."""
    beats = result.get("beats") or []
    profile = result.get("profile") or {}
    return {
        "primary_beats": {
            p: sum(1 for b in beats if b.get("actor_character_id") == p)
            for p in story.get("primary_ids") or []
        },
        "planning": {k: len(result[k]) for k in PLANNING if result.get(k)},
        "degradation": list(result.get("degradation") or []),
        "profile": f"{profile['id']} v{profile['version']}" if profile else None,
        "audience_filter": result.get("audience_filter"),
    }


def _error_code(answer: Any) -> str:
    try:
        return str(answer.json()["error"]["code"])
    except ValueError, KeyError, TypeError:
        return str(answer.status_code)
