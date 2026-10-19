"""DETERMINISM: the same inputs on the stub give byte-identical Engine Core state (docs/04 §6;
NFR-DETERM-1; #75).

``script.json`` is a committed 60-step script: turns (some with an action), regenerations,
truncations, a swap, two forks (one a fork of a fork), pinned memories and custom instructions,
and drains of the Lite summary jobs. The first story's window is small (its override of its
profile's ``window_messages``, #113), so the summary crosses it several times. Every step
carries a fixed ``Idempotency-Key`` and the one principal. The script runs in-process through
the REST app (the stub adapter, no background worker; ``drain`` steps run the due summary jobs)
on two fresh scratch databases. Then:

- every step's response body is the same in both runs, once ids and timestamps are mapped and
  durations dropped (``epistrel.testing.determinism.Normalizer``);
- the Engine Core state of the two databases is the same table by table
  (``determinism.state``: every story table except the operational ones, plus
  ``deployment_state``).

The complement runs the script a third time with ``stub.nondeterministic = True`` (every
generated text is salted per call) and shows the comparator reports the drift, in the response
bodies and in the stored state alike.
"""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest

from epistrel.core.db import make_engine
from epistrel.engine import Engine
from epistrel.testing.determinism import Normalizer, differences, state
from tests import genesis_support as g
from tests import scenario_support as scenarios
from tests import structured_genesis_support as sg
from tests.auth_support import auth_settings
from tests.conformance.conftest import migrated_database
from tests.idempotency_support import KEY
from tests.respond_support import FIRST, PLAYER, SECOND, respond_client
from tests.story_support import READ, WRITE, story_body
from tests.summary_support import run_due_jobs

SCRIPT = Path(__file__).with_name("script.json")
STRUCTURED_SCRIPT = Path(__file__).with_name("script_structured.json")


class Run:
    """One replay of the script: its normalised answers and final state."""

    def __init__(self, client: httpx.AsyncClient, engine: Engine, principal: str) -> None:
        self.client = client
        self.engine = engine
        self.headers = {**WRITE, "X-Epistrel-Principal": principal}
        self.stories: dict[str, str] = {}
        self.players: dict[str, str] = {}
        self.answers: list[Any] = []
        self.normalize = Normalizer()

    async def step(self, step: dict[str, Any]) -> None:
        op = step["op"]
        if op == "drain":
            ran = 0
            for story in self.stories.values():
                ran += len(await run_due_jobs(self.engine.db, self.engine.models, story))
            self.answers.append({"drained": ran})
            return
        method, path, payload = self._request(step)
        response = await self.client.request(
            method, path, json=payload, headers={**self.headers, KEY: step["key"]}
        )
        assert response.status_code in (200, 201), f"{step}: {response.text}"
        found = response.json()
        if op in ("create_story", "fork"):
            self.stories[step["alias"]] = found["story_id"]
            self.players[step["alias"]] = next(
                c["id"] for c in found["characters"] if c["name"] == PLAYER
            )
        if "overrides" in step:  # the story's overrides of its profile's dials (#113)
            overridden = await self.client.patch(
                f"/stories/{found['story_id']}/profile-overrides",
                json={"settings": step["overrides"], "ignore_warnings": True},
                headers={**self.headers, KEY: f"{step['key']}-overrides"},
            )
            assert overridden.status_code == 200, f"{step}: {overridden.text}"
            found = {**found, "overrides": overridden.json()}
        self.answers.append(self.normalize(found))

    def _request(self, step: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
        op = step["op"]
        if op == "create_story":
            return "POST", "/stories", story_body(PLAYER, FIRST, SECOND, settings=step["settings"])
        story = self.stories[step["story"]]
        base = f"/stories/{story}"
        if op == "respond":
            player_input = {"say": step["say"], **({"do": step["do"]} if "do" in step else {})}
            actor = self.players[step["story"]]
            return (
                "POST",
                f"{base}/respond",
                {
                    "actor_character_id": actor,
                    "player_input": player_input,
                },
            )
        if op == "regenerate":
            return "POST", f"{base}/regenerate", {}
        if op == "truncate":
            return "POST", f"{base}/truncate", {"to_turn": step["to_turn"]}
        if op == "swap":
            return "POST", f"{base}/swap", {"to_variant_chain": step["chain"]}
        if op == "fork":
            return "POST", f"{base}/fork", {"at_turn": step["at_turn"]}
        if op == "pins":
            return "PUT", f"{base}/pinned-memories", {"items": [{"text": t} for t in step["items"]]}
        if op == "instructions":
            return "PUT", f"{base}/custom-instructions", {"text": step["text"]}
        raise AssertionError(f"unknown script step {op!r}")


async def replay(pg_url: str, *, nondeterministic: bool = False) -> tuple[list[Any], Any]:
    """Run the script on a fresh database; its normalised answers and state."""
    script = json.loads(SCRIPT.read_text())
    with migrated_database(pg_url, f"epistrel_det_{secrets.token_hex(4)}") as url:
        settings = auth_settings(url, model_adapter="stub")
        db = make_engine(settings)
        try:
            async with respond_client(settings, db) as (client, engine):
                assert engine.models.stub is not None
                engine.models.stub.nondeterministic = nondeterministic
                run = Run(client, engine, script["principal"])
                for step in script["steps"]:
                    await run.step(step)
                for story in run.stories.values():
                    assert (await client.get(f"/stories/{story}", headers=READ)).status_code == 200
                return run.answers, await state(db)
        finally:
            await db.dispose()


@pytest.mark.part("M2", "NFR-DETERM-1")
async def test_determinism(pg_url: str) -> None:
    """Two replays of the script on fresh databases: identical answers and identical state.

    Complement asserted: a nondeterministic stub's replay differs from the first, in its
    answers and in its state, so the comparison detects drift."""
    first_answers, first_state = await replay(pg_url)
    second_answers, second_state = await replay(pg_url)
    assert differences(first_answers, second_answers) == []
    assert differences(first_state, second_state) == []
    assert len(first_answers) == 60
    assert {name: len(rows) for name, rows in first_state.items()}["lite_summary_segments"] > 0
    assert sum(a.get("drained", 0) for a in first_answers if isinstance(a, dict)) > 0

    drift_answers, drift_state = await replay(pg_url, nondeterministic=True)
    assert differences(first_answers, drift_answers) != []
    assert differences(first_state, drift_state) != []


_MARK = re.compile(r"«\d+»")
_SHAPE = re.compile(r'"shape": "[0-9a-f]{32}"')
"""A detected claim's shape (#148): a digest of its normalized form, which holds ids."""


def masked(found: Any) -> dict[str, list[str]]:
    """A normalised state with every id mark the same (``«id»``) and each table's rows sorted:
    the structured story writes rows that differ only in their ids (two aliases of one name,
    memories of one beat, …), which no natural order separates, so their first-met numbering
    is not stable across runs. Every value but the ids (and the claim shapes digesting them, #148)
    is still compared."""
    return {
        table: sorted(
            _SHAPE.sub(
                '"shape": "«hash»"',
                _MARK.sub("«id»", json.dumps(row, sort_keys=True, default=str, ensure_ascii=False)),
            )
            for row in rows
        )
        for table, rows in found.items()
    }


async def replay_structured(pg_url: str, *, nondeterministic: bool = False) -> tuple[Any, Any]:
    """Run the structured script on a fresh database: the Sunday Dinner scenario created as a
    structured story (#149's genesis, the seeded ``Structured (M4)`` profile), then its steps
    (``{op: respond, actor, input}``, regenerations, a truncation); the normalised answers
    and the final state."""
    script = json.loads(STRUCTURED_SCRIPT.read_text())
    with migrated_database(pg_url, f"epistrel_det_{secrets.token_hex(4)}") as url:
        settings = auth_settings(url, model_adapter="stub")
        db = make_engine(settings)
        try:
            async with respond_client(settings, db) as (client, engine):
                assert engine.models.stub is not None
                engine.models.stub.nondeterministic = nondeterministic
                normalize = Normalizer()
                locked, default = await scenarios.cast_characters(client)
                mother = await g.catalog_character_id(
                    client, "June Harrow", age=60, labels=["warm"]
                )
                scenario_id, _ = await g.published_scenario(
                    client, sg.structured_dinner(locked, default)
                )
                made = await g.create_from(client, sg.structured_request(scenario_id, mother))
                answers: list[Any] = [normalize(made)]
                story = made["story_id"]
                roles = {c["role_id"]: c["id"] for c in made["characters"]}
                headers = {**WRITE, "X-Epistrel-Principal": script["principal"]}
                base = f"/stories/{story}"
                for step in script["steps"]:
                    op = step["op"]
                    if op == "respond":
                        path = f"{base}/respond"
                        payload: dict[str, Any] = {
                            "actor_character_id": roles[step["actor"]],
                            "player_input": step["input"],
                        }
                    elif op == "regenerate":
                        path, payload = f"{base}/regenerate", {}
                    elif op == "truncate":
                        path, payload = f"{base}/truncate", {"to_turn": step["to_turn"]}
                    else:  # pragma: no cover - the script's own ops
                        raise AssertionError(f"unknown script step {op!r}")
                    response = await client.post(
                        path, json=payload, headers={**headers, KEY: step["key"]}
                    )
                    assert response.status_code == 200, f"{step}: {response.text}"
                    answers.append(normalize(response.json()))
                return answers, await state(db)
        finally:
            await db.dispose()


@pytest.mark.part("M4", "NFR-DETERM-1")
async def test_determinism__structured(pg_url: str) -> None:
    """DETERMINISM (M4): the structured script (a structured story from the Sunday Dinner
    scenario, structured turns on the stub model and the stub embedder, two regenerations
    through #154's commit boundary and a truncation) on two fresh databases gives identical
    answers and identical Engine Core state after the normaliser (facts, thunks, the ledger,
    memories and their vectors byte for byte).

    Complement asserted: a nondeterministic stub's replay differs, in its answers and its
    state, so the comparison detects drift."""
    first_answers, first_state = await replay_structured(pg_url)
    second_answers, second_state = await replay_structured(pg_url)
    assert differences(first_answers, second_answers) == []
    assert differences(masked(first_state), masked(second_state)) == []
    script = json.loads(STRUCTURED_SCRIPT.read_text())
    assert len(first_answers) == len(script["steps"]) + 1
    counts = {name: len(rows) for name, rows in first_state.items()}
    assert counts["facts"] > 0
    assert counts["memories"] > 0
    regenerated = [
        r
        for r in first_state["operations"]
        if "regenerated_variant" in (r.get("turn_report") or {})
    ]
    assert len(regenerated) == 2  # both regenerations ran the structured path (#154)
    assert any(r["membership"] == "dormant" for r in first_state["memories"])

    drift_answers, drift_state = await replay_structured(pg_url, nondeterministic=True)
    assert differences(first_answers, drift_answers) != []
    assert differences(masked(first_state), masked(drift_state)) != []
