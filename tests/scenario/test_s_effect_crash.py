"""S-EFFECT-CRASH: durable lifecycle and resume (docs/04 §7; FR-EXT-2,16..20, FR-CONC-6,
FR-RESP-8).

This file is shared by the stories that build the lifecycle: #215 (post-effect values, the
no-delta check), #212 (the staged turn, the crash boundaries and the end-to-end post-effect
scenario) and #213.

**No delta arithmetic** (``test_s_effect_crash__no_delta_apply``, #215, FR-EXT-20): the engine
never computes a resulting external value by applying a delta to the pre-effect read. Checked
on the code itself: the only writer of ``external_values`` is its projection, which folds the
three recorded sources (a read, a confirmation's resulting value, a post-effect read; and the
``Unknown`` when neither answered) by copying the value each event recorded, and neither the
fold nor the writers that stage those events does any arithmetic. Then on a story: the turn
reads 300, the purchase of 50 is confirmed with a resulting 230 (another writer spent too), so
version 2 is 230 (never the computed 250) and version 1 is unchanged.
"""

from __future__ import annotations

import ast
import asyncio
import json
import secrets
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import outbox as core_outbox
from epistrel.core.db import make_engine
from epistrel.core.ids import new_ulid
from epistrel.core.membership import Position
from epistrel.core.operation_view import read_operation
from epistrel.core.turns.staged import FORK_CLOSURE
from epistrel.orchestrator.jobs import settlement
from epistrel.orchestrator.jobs.handlers import outbox as dispatcher
from epistrel.orchestrator.providers import pinned
from epistrel.orchestrator.providers.port import WriteOutcome
from epistrel.orchestrator.structured import staged
from epistrel.testing import crash
from epistrel.testing.faults import EngineProcess, StubRule
from tests import effect_support as es
from tests import hot_set_support as hs
from tests import multi_support as ms
from tests import provider_support as ps
from tests import staged_crash_support as sc
from tests import staged_support as sp
from tests import state_support as ss
from tests.auth_support import CREDENTIAL_A, auth_settings
from tests.conformance.conftest import migrated_database
from tests.event_support import execute_in_tx, rows
from tests.respond_support import body, respond, respond_client
from tests.story_support import READ, WRITE, override
from tests.structured_turn_support import beat, structured_story, turn_report

pytestmark = pytest.mark.integration

SRC = Path(__file__).resolve().parents[2] / "src" / "epistrel"
PROJECTION = SRC / "core" / "projections" / "external.py"
WRITERS = SRC / "orchestrator" / "providers" / "pinned.py"
CORE = SRC / "core" / "states" / "external.py"

FOLDS = {"value_row", "outcome_row", "_position", "_scope"}
"""The projection's row builders: the only code that produces an ``external_values`` row."""
WRITER_FUNCTIONS = {
    "Answer.pinned",
    "OutcomeTarget.pin",
    "Outcome.event",
    "stage_outcome",
    "_version",
    "write_confirmation_version",
    "post_effect_read",
    "record_unknown",
    "settle_value",
}
"""The writers that decide and stage a post-effect version's event (#215)."""
ARITHMETIC = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)


def _arithmetic(function: ast.AST) -> list[ast.AST]:
    """The arithmetic in ``function``'s body (its annotations' ``X | None`` left out)."""
    body = [n for s in getattr(function, "body", []) for n in ast.walk(s)]
    return [
        n for n in body if isinstance(n, ast.AugAssign | ast.BinOp) and isinstance(n.op, ARITHMETIC)
    ]


WRITE_VERBS = {"insert", "update", "delete", "values"}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            found[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    found[f"{node.name}.{item.name}"] = item
    return found


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
    }


def _table_writes() -> list[str]:
    """Every place in ``src`` that writes ``external_values`` directly (an insert, update or
    delete statement naming it, SQL text writing it) or declares a projection of it."""
    found: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        where = str(path.relative_to(SRC))
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Call):
                func = node.func
                verb = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                receiver = func.value if isinstance(func, ast.Attribute) else None
                named = [*node.args, *([receiver] if receiver is not None else [])]
                if verb in WRITE_VERBS and any(
                    isinstance(a, ast.Name) and a.id == "external_values" for a in named
                ):
                    found.append(f"{where}: {verb}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = " ".join(node.value.split()).upper()
                if any(
                    f"{verb} EXTERNAL_VALUES" in text
                    for verb in ("INSERT INTO", "UPDATE", "DELETE FROM")
                ):
                    found.append(f"{where}: sql")
            elif isinstance(node, ast.ClassDef):
                for item in node.body:
                    if (
                        isinstance(item, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "table" for t in item.targets)
                        and isinstance(item.value, ast.Name)
                        and item.value.id == "external_values"
                    ):
                        found.append(f"{where}: {node.name}")
    return found


def _no_delta_check() -> None:
    # 1. The projection is the only writer of the table.
    assert _table_writes() == ["core/projections/external.py: ExternalValuesProjection"]

    # 2. It folds exactly the three recorded sources, each row built by a row builder.
    folds = _tree(PROJECTION)
    functions = _functions(folds)
    apply = functions["ExternalValuesProjection.apply"]
    checked = {
        a.id
        for n in ast.walk(apply)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "isinstance"
        for a in ast.walk(n.args[1])
        if isinstance(a, ast.Name)
    }
    assert checked == {"ExternalRead", "ExternalOutcome"}
    built = {
        n.func.id
        for n in ast.walk(apply)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id != "isinstance"
    }
    assert built == {"value_row", "outcome_row"}

    # 3. A row's value is the value its event recorded, copied: ``<payload>.value``.
    for name in ("value_row", "outcome_row"):
        (row,) = [n for n in ast.walk(functions[name]) if isinstance(n, ast.Dict)]
        values = {
            k.value: v
            for k, v in zip(row.keys, row.values, strict=True)
            if isinstance(k, ast.Constant)
        }
        copied = values["value"]
        assert isinstance(copied, ast.Attribute), name
        assert copied.attr == "value", name
        assert isinstance(copied.value, ast.Name), name
        assert copied.value.id == functions[name].args.args[1].arg, name

    # 4. No arithmetic and no delta anywhere a version's value is decided or folded.
    writers = _functions(_tree(WRITERS))
    for name, function in [
        *((n, functions[n]) for n in FOLDS),
        *((n, writers[n]) for n in WRITER_FUNCTIONS),
    ]:
        assert not _arithmetic(function), name
        assert "delta" not in _names(function), name

    # 5. The one sum on this path numbers versions; it never reads a value.
    base = _functions(_tree(CORE))["version_base"]
    assert "value" not in _names(base)


@pytest.mark.part("M6", "FR-EXT-20")
async def test_s_effect_crash__no_delta_apply(engine: AsyncEngine, states_delivered: None) -> None:
    """See the module docstring."""
    _no_delta_check()

    name = ps.unique_name("bank")
    provider = await ps.register(engine, ps.in_process_spec(name, writable=["gold"]))
    story, (cal,) = await hs.cast(engine, ["Cal"])
    await ss.set_level(engine, story, "on")
    gold = await ps.external_gold(engine, story, name)
    turn, read = await ps.record_read(engine, story, gold, cal, 300)
    (before,) = await ps.external_value_rows(engine, story)

    at = Position(turn, 0, 0, 2, 1)
    target = await pinned.outcome_target(engine, story, gold, cal, at)
    assert target is not None
    outcome = pinned.Outcome(
        outbox_id=new_ulid(),
        effect_id=new_ulid(),
        answer=WriteOutcome("confirmed", resulting_values={"gold": 230}),
        answered_at=datetime.now(UTC),
    )
    reads = pinned.PinnedReads(engine, story)
    async with ps.outcome_op(engine, story, turn=turn) as op:
        found = await pinned.settle_value(op, reads, target, outcome, provider)
        await op.commit()
    assert found is not None
    assert getattr(found, "value", None) == Decimal(230)  # the provider's, never 300 - 50
    assert reads.live_reads == 0

    first, second = await ps.external_value_rows(engine, story)
    assert first == before  # version 1 is never updated
    assert (first["version"], first["value"], first["base_event_id"]) == (1, 300, read)
    assert (second["version"], second["value"], second["base_event_id"]) == (2, 230, read)
    await ss.set_level(engine, story, None)


# -- #212: the staged turn, end to end -------------------------------------------------------

REASON = "bought a sword"
TAKES = "Mira takes the coins."
COUNTS = "Cal has 300 gold."
WENT = "The purchase went through."
PAID = WENT
LEFT_250 = "Cal has 250 gold left."
LEFT_240 = "Cal has 240 gold left."
MODEL_CALLS = 40
"""Room for the outcome step's repair (#214 reserves the outcome render itself)."""


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _purchase(cal: str, text: str = TAKES, *more: dict[str, Any]) -> str:
    return es.render(
        beat(
            "do",
            text,
            effects=[es.effect("gold", scope_key=cal, delta=-50, reason=REASON), *more],
        )
    )


def _outcome_step(stub: Any, *beats: dict[str, Any]) -> None:
    """The outcome step's narrator render answers ``beats`` (only its prompt carries
    ``## Outcome step``; registered before ``es.narrator``, the first match wins)."""
    stub.when(staged.OUTCOME_STEP, es.render(*beats), purpose="generation")


async def _sync_story(
    client: Any,
    engine: AsyncEngine,
    epistrel: Any,
    *,
    default: Any = 300,
    **spec: Any,
) -> tuple[str, str, str, ps.ScriptedProvider]:
    """A story whose gold a fresh synchronous write-capable in-process provider owns (its
    ``spec`` fields over ``write_capable_spec``'s): its id, Cal's, the provider and its fake."""
    name = ps.unique_name("bank")
    await ps.register(engine, ps.write_capable_spec(name, confirmation="synchronous", **spec))
    fake = ps.ScriptedProvider(name, default=default).attach(epistrel)
    story, ids = await structured_story(client, "Cal", "Mira")
    await override(client, story, {"budgets": {"model_calls": MODEL_CALLS}})
    await ss.set_level(engine, story, "on")
    await ps.external_gold(engine, story, name, authorization=es.ANY_CHARACTER)
    return story, ids["Cal"], name, fake


def _kinds(verify: dict[str, Any] | None) -> list[str]:
    return [
        v["kind"]
        for v in (verify or {}).get("verdicts", [])
        if v.get("kind") in ("supported", "contradicted", "uncertain")
    ]


async def _purchase_turn(
    client: Any, stub: Any, story: str, cal: str, outcome: str, *, regenerated: str | None = None
) -> dict[str, Any]:
    stub.reset()
    _outcome_step(stub, beat("narration", outcome))
    ms.renders_for(stub, "Mira", _purchase(cal))
    es.narrator(stub, es.render(beat("narration", COUNTS)))
    if regenerated is not None:
        stub.script("regeneration", [json.dumps({"text": regenerated, "effects": []})])
    answer = await respond(client, story, body(cal, None, do="buys the sword"))
    assert answer.status_code == 200, answer.text
    return dict(answer.json())


@pytest.mark.part("M6", "FR-EXT-20")
async def test_s_effect_crash__post_effect(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """#215's end-to-end post-effect clauses (moved to #212, which builds the outcome stage and
    step). Balance 300 (the turn's pinned read), a purchase of 50 on a synchronous provider:

    1. **Resulting values.** The game confirms with ``{"gold": 250}``: the outcome step's "Cal
       has 250 gold left." is ``supported`` (version 2, source ``confirmation``, at the outcome
       step's first position), while the attempt turn's "Cal has 300 gold." still verifies
       against 300 at its own beat.
    2. **Success only.** A provider that returns no resulting values, whose post-effect read
       fails: the purchase narrated as done ("The purchase went through.") is accepted; "Cal
       has 250 gold left." is ``uncertain(provider_unknown)`` (an ``Unknown`` version 2) and
       repaired.
    3. **Another writer.** No resulting values, and the post-effect read answers 240: the
       outcome step verifies against 240, never a computed 250 ("Cal has 250 gold left." is
       contradicted and repaired; "Cal has 240 gold left." stands).

    Complement: version 1 (the 300 read) is unchanged after the confirmation."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        # 1. The confirmation carries the resulting value.
        story, cal, _, fake = await _sync_story(client, engine, epistrel)
        fake.script_writes("gold", WriteOutcome(status="confirmed", resulting_values={"gold": 250}))
        played = await _purchase_turn(client, stub, story, cal, f"{WENT} {LEFT_250}")
        texts = [b["text"] for b in played["beats"]]
        assert COUNTS in texts
        assert f"{WENT} {LEFT_250}" in texts
        report = await turn_report(engine, story)
        assert _kinds(report["verify"]) == ["supported"]  # 300, at the attempt's beat
        outcome_step = report["staged"]["outcome_step"]
        assert outcome_step["repairs"] == []
        assert _kinds(outcome_step["verify"]) == ["supported"]  # 250, at the outcome step
        first, second = await ps.external_value_rows(engine, story)
        assert (first["version"], first["value"], first["source"]) == (1, 300, "read")
        assert (second["version"], second["value"], second["source"]) == (
            2,
            250,
            "confirmation",
        )
        assert second["position_beat"] == outcome_step["beats"][0]
        assert fake.reads == [("gold", story, cal)]  # no post-effect read was needed

        # 2. Success only, and no post-effect read: the effect stands, the value is unknown.
        story, cal, _, fake = await _sync_story(
            client, engine, epistrel, returns_resulting_values=False
        )
        fake.script("gold", 300, ps.Fail())
        played = await _purchase_turn(
            client, stub, story, cal, f"{PAID} {LEFT_250}", regenerated=PAID
        )
        texts = [b["text"] for b in played["beats"]]
        assert PAID in texts
        assert not any("250" in t for t in texts)
        outcome_step = (await turn_report(engine, story))["staged"]["outcome_step"]
        assert [r["verdict"] for r in outcome_step["repairs"]] == ["uncertain"]
        assert "uncertain" in _kinds(outcome_step["verify"])
        first, second = await ps.external_value_rows(engine, story)
        assert (first["version"], first["value"]) == (1, 300)
        assert (second["version"], second["value"], second["source"]) == (2, None, "unknown")

        # 3. Another writer spent too: the post-effect read (240) is what the step verifies.
        story, cal, _, fake = await _sync_story(
            client, engine, epistrel, returns_resulting_values=False
        )
        fake.script("gold", 300, 240)
        played = await _purchase_turn(client, stub, story, cal, LEFT_250, regenerated=LEFT_240)
        texts = [b["text"] for b in played["beats"]]
        assert LEFT_240 in texts
        assert not any("250" in t for t in texts)
        outcome_step = (await turn_report(engine, story))["staged"]["outcome_step"]
        assert _kinds(outcome_step["verify"]) == ["contradicted"]  # 250 against 240
        assert [r["accepted"] for r in outcome_step["repairs"]] == [True]
        rows = await ps.external_value_rows(engine, story)
        assert [(r["version"], r["value"], r["source"]) for r in rows] == [
            (1, 300, "read"),
            (2, 240, "post_effect_read"),
        ]
        await ss.set_level(engine, story, None)


STABLE = "paid the stable"
NO_WORD = "No word has come from the bank yet."
SILVER_WAIT = 2
"""The silver provider's ``confirm_wait_max_seconds``: the longer of the turn's two waits."""


async def _revision(engine: AsyncEngine, story: str) -> int:
    found = await rows(engine, "SELECT timeline_revision FROM stories WHERE id = :s", s=story)
    return int(found[0].timeline_revision)


async def _staged_op(engine: AsyncEngine, story: str) -> Any:
    """The story's latest staged turn's operation row."""
    found = await rows(
        engine,
        "SELECT operation_id, status::text AS status, turn_stage::text AS turn_stage, "
        "narrative_turn, timeline_revision, stage_log FROM operations WHERE story_id = :s "
        "AND turn_stage IS NOT NULL ORDER BY narrative_turn DESC LIMIT 1",
        s=story,
    )
    return found[0]


async def _history(client: Any, story: str, viewpoint: str) -> dict[str, Any]:
    answer = await client.get(
        f"/stories/{story}/history", params={"viewpoint": viewpoint}, headers=READ
    )
    assert answer.status_code == 200, answer.text
    return dict(answer.json())


async def _stages(client: Any, operation_id: str) -> dict[str, Any]:
    answer = await client.get(f"/operations/{operation_id}", headers=READ)
    assert answer.status_code == 200, answer.text
    return dict(answer.json())


def _stage_rows(view: dict[str, Any]) -> list[tuple[str, str, int | None]]:
    return [(s["name"], s["status"], s.get("revision")) for s in view["stages"]]


def _outcome_prompts(stub: Any) -> list[str]:
    return [p for p in es.prompts(stub, "NarratorRender") if staged.OUTCOME_STEP in p]


async def _silver(engine: AsyncEngine, epistrel: Any, story: str) -> ps.ScriptedProvider:
    """``silver``, a second external value of Cal's on a second synchronous provider (its
    ``gold`` value), whose wait is ``SILVER_WAIT`` seconds."""
    name = ps.unique_name("stable")
    await ps.register(
        engine,
        ps.write_capable_spec(
            name, confirmation="synchronous", confirm_wait_max_seconds=SILVER_WAIT
        ),
    )
    fake = ps.ScriptedProvider(name, default=100).attach(epistrel)
    await ps.bind(engine, story, name, {"player": "p-1"})
    await ss.define_all(
        engine,
        story,
        ss.gold(
            name="silver",
            aliases=("silver coins",),
            unit="sp",
            owner="external",
            provider_id=name,
            external_value_name="gold",
            authorization=es.ANY_CHARACTER,
        ),
    )
    return fake


@pytest.mark.part("M6", "FR-EXT-16,19")
async def test_s_effect_crash__visibility(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """#212: the staged turn while it waits, and its wait (FR-EXT-15,16,19).

    1. **While the turn waits for its outcome** (the provider holds its answer): the intent
       committed (one revision) and the claim did (none), so the turn is ``awaiting_outcome``,
       ``committed``. A history read shows the turn ``incomplete`` with its committed attempt
       beat and no message; a second ``respond`` is 423 ``story_busy`` with ``Retry-After``;
       ``GET /operations/{id}`` answers ``staged``, label ``staged_turn`` and the stages
       (intent and claim done, at the intent's revision, the outcome running); a fork taken now
       copies the attempt, closes the turn there with "The outcome is not known in this story."
       (completed, a message), holds the effect only as a non-executable copy and lists it in
       ``effects_pending_in_source``. Released, the game confirms and the turn completes: the
       revision moved once at intent, once at outcome and once at completion, and every stage
       is done.
    2. **A provider that never answers** (its wait 1 s): the turn completes describing the
       purchase as pending (the outcome step's "went through" is repaired), ``turn_stage =
       completed``, the row still ``claimed``; nothing settles (#217 surfaces a later answer).
    3. **Two synchronous effects in one turn** (gold, wait 1 s; silver, wait 2 s): every row is
       claimed, one outcome transaction records an ``external_outcome`` per answered row at
       the outcome step's one position, and one outcome render narrates every result: (a) both
       confirm; (b) one confirms and one fails; (c) silver confirms and gold never answers: the
       longer wait (2 s) applies, only silver's outcome is recorded, gold's row stays
       ``claimed``."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        # -- 1. While the turn waits for its outcome.
        story, cal, _, fake = await _sync_story(client, engine, epistrel)
        released = asyncio.Event()
        fake.script_writes("gold", ps.Hang(released))
        stub.reset()
        _outcome_step(stub, beat("narration", WENT))
        ms.renders_for(stub, "Mira", _purchase(cal))
        es.narrator(stub, es.render(beat("narration", COUNTS)))
        before = await _revision(engine, story)
        playing = asyncio.create_task(respond(client, story, body(cal, None, do="buys the sword")))
        await asyncio.wait_for(fake.hanging.wait(), 60)
        op = await _staged_op(engine, story)
        assert (op.status, op.turn_stage) == ("committed", "awaiting_outcome")
        assert await _revision(engine, story) == before + 1  # intent only; the claim none
        (row,) = await ps.outbox_rows(engine, story)
        assert row.status == "claimed"

        page = await _history(client, story, f"character:{cal}")
        (turn,) = [t for t in page["turns"] if t["turn"] == op.narrative_turn]
        assert turn["incomplete"] is True
        assert turn["message"] is None
        items = [i["text"] for i in page["items"] if i["turn"] == op.narrative_turn]
        assert TAKES in items
        assert WENT not in items

        busy = await respond(client, story, body(cal, None, do="waits"))
        assert busy.status_code == 423, busy.text
        assert busy.json()["error"]["code"] == "story_busy"
        assert int(busy.headers["Retry-After"]) >= 1

        view = await _stages(client, op.operation_id)
        assert view["staged"] is True
        assert view["label"] == "staged_turn"
        assert _stage_rows(view) == [
            ("intent", "done", before + 1),
            ("claim", "done", before + 1),
            ("outcome", "running", None),
            ("completion", "pending", None),
            ("response", "pending", None),
        ]

        answer = await client.post(f"/stories/{story}/fork", json={}, headers=WRITE)
        assert answer.status_code in (200, 201), answer.text
        forked = answer.json()
        assert [e["outbox_id"] for e in forked["effects_pending_in_source"]] == [row.id]
        fork_id = forked["story_id"]
        (copy,) = await ps.outbox_rows(engine, fork_id)
        assert copy.executable is False
        assert copy.id != row.id
        closed = await _staged_op(engine, fork_id)
        assert closed.turn_stage == "completed"
        page = await _history(client, fork_id, "omniscient")
        (turn,) = [t for t in page["turns"] if t["turn"] == closed.narrative_turn]
        assert "incomplete" not in turn
        assert FORK_CLOSURE in turn["message"]["text"]
        items = [i["text"] for i in page["items"] if i["turn"] == closed.narrative_turn]
        assert items.index(TAKES) < items.index(FORK_CLOSURE)
        assert WENT not in items

        released.set()
        answer = await asyncio.wait_for(playing, 60)
        assert answer.status_code == 200, answer.text
        assert WENT in answer.json()["message"]["text"]
        op = await _staged_op(engine, story)
        assert op.turn_stage == "completed"
        assert await _revision(engine, story) == before + 3
        assert _stage_rows(await _stages(client, op.operation_id)) == [
            ("intent", "done", before + 1),
            ("claim", "done", before + 1),
            ("outcome", "done", before + 2),
            ("completion", "done", before + 3),
            ("response", "done", before + 3),
        ]
        page = await _history(client, story, f"character:{cal}")
        (turn,) = [t for t in page["turns"] if t["turn"] == op.narrative_turn]
        assert "incomplete" not in turn
        assert WENT in turn["message"]["text"]
        await ps.drop_outbox_jobs(engine, story)
        await ps.drop_outbox_jobs(engine, fork_id)

        # -- 2. A provider that never answers: the turn completes, the purchase pending.
        story, cal, _, fake = await _sync_story(
            client, engine, epistrel, confirm_wait_max_seconds=1
        )
        fake.script_writes("gold", ps.Hang())
        played = await _purchase_turn(client, stub, story, cal, WENT, regenerated=NO_WORD)
        texts = [b["text"] for b in played["beats"]]
        assert NO_WORD in texts
        assert not any("went through" in t for t in texts)
        assert "went through" not in played["message"]["text"]
        (prompt,) = _outcome_prompts(stub)
        assert "no answer yet; narrate it as still pending" in prompt
        op = await _staged_op(engine, story)
        assert op.turn_stage == "completed"
        (row,) = await ps.outbox_rows(engine, story)
        assert row.status == "claimed"
        assert await ps.external_outcomes(engine, story) == []
        await ps.drop_outbox_jobs(engine, story)

        # -- 3. Two synchronous effects in one turn.
        for gold_answer, silver_answer, recorded in (
            ("confirmed", "confirmed", ["confirmed", "confirmed"]),
            ("confirmed", "failed", ["confirmed", "failed"]),
            (ps.Hang(), "confirmed", ["confirmed"]),
        ):
            story, cal, _, fake = await _sync_story(
                client, engine, epistrel, confirm_wait_max_seconds=1
            )
            silver = await _silver(engine, epistrel, story)
            fake.script_writes("gold", gold_answer)
            silver.script_writes("gold", silver_answer)
            stub.reset()
            _outcome_step(stub, beat("narration", "The coins and the silver change hands."))
            ms.renders_for(
                stub,
                "Mira",
                _purchase(
                    cal,
                    TAKES,
                    es.effect("silver", scope_key=cal, delta=-20, reason=STABLE),
                ),
            )
            es.narrator(stub, es.render(beat("narration", COUNTS)))
            before = await _revision(engine, story)
            started = time.monotonic()
            answer = await respond(client, story, body(cal, None, do="pays the smith and stable"))
            waited = time.monotonic() - started
            assert answer.status_code == 200, answer.text
            assert await _revision(engine, story) == before + 3
            (prompt,) = _outcome_prompts(stub)  # one combined outcome step
            assert f"{REASON}: " in prompt
            assert f"{STABLE}: " in prompt
            outcomes = await ps.external_outcomes(engine, story)
            assert sorted(o.payload["status"] for o in outcomes) == recorded
            assert len({(o.narrative_turn, o.beat, o.step) for o in outcomes}) == 1
            statuses = sorted(r.status for r in await ps.outbox_rows(engine, story))
            if isinstance(gold_answer, ps.Hang):
                assert waited >= SILVER_WAIT - 0.2  # the longer wait applied
                assert f"{REASON}: no answer yet" in prompt
                assert f"{STABLE}: confirmed" in prompt
                assert statuses == ["claimed", "confirmed"]
            elif silver_answer == "failed":
                assert f"{REASON}: confirmed" in prompt
                assert f"{STABLE}: refused" in prompt
                assert statuses == ["confirmed", "failed"]
            else:
                assert f"{REASON}: confirmed" in prompt
                assert f"{STABLE}: confirmed" in prompt
                assert statuses == ["confirmed", "confirmed"]
            await ps.drop_outbox_jobs(engine, story)
        await ss.set_level(engine, story, None)


@pytest.mark.part("M6", "FR-EXT-16")
async def test_s_effect_crash__late_confirmation(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """#217's part of the wait clause (FR-EXT-16,21): a synchronous provider that does not
    answer within ``confirm_wait_max_seconds`` leaves the turn completed with the purchase
    pending and its row ``claimed``. When the provider confirms later (the lease lapses and the
    deduplicating provider is sent the same key again), the dispatcher records the outcome (an
    ``outcome_recorded`` event with no narrative position) and enqueues its settlement, which
    lands at the next inter-turn slot after the staged turn, never inside it; the next turn
    surfaces the confirmation (the narrator's ``## Awaiting the game`` names it confirmed)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, cal, _, fake = await _sync_story(
            client, engine, epistrel, confirm_wait_max_seconds=1
        )
        fake.script_writes("gold", ps.Hang())
        played = await _purchase_turn(client, stub, story, cal, WENT, regenerated=NO_WORD)
        op = await _staged_op(engine, story)
        assert op.turn_stage == "completed"
        (row,) = await ps.outbox_rows(engine, story)
        assert row.status == "claimed"
        assert await ps.external_outcomes(engine, story) == []  # nothing recorded in the wait

        # Later: the lease lapses, the provider is sent the same key and confirms.
        fake.script_writes("gold", "confirmed")
        await execute_in_tx(
            engine,
            (
                "UPDATE external_effect_outbox SET claim_lease_until = now() - interval '1 s' "
                "WHERE id = :i",
                {"i": row.id},
            ),
        )
        resent = await dispatcher.expire(engine, row.id, registry=epistrel.in_process_providers)
        assert resent == "resent:confirmed"
        (record,) = [r for r in await ps.outbox_outcomes(engine, story) if r.kind == "confirmed"]
        (recorded,) = await rows(
            engine,
            "SELECT narrative_turn, payload FROM events WHERE story_id = :s "
            "AND type = 'outcome_recorded'",
            s=story,
        )
        assert recorded.narrative_turn is None
        assert recorded.payload["outcome_id"] == record.id
        done = await settlement.settle(engine, record.id, registry=epistrel.in_process_providers)
        assert done.result == settlement.SETTLED
        assert done.position is not None
        assert (done.position.turn, done.position.slot_k) == (played["turn"], 1)
        at_slot = [o for o in await ps.external_outcomes(engine, story) if o.slot_k]
        assert [(o.narrative_turn, o.slot_k) for o in at_slot] == [(played["turn"], 1)]

        stub.reset()
        es.narrator(stub, es.render(beat("narration", "Rain taps.")))
        ms.renders_for(stub, "Mira", es.render(beat("say", "Anything else?")))
        answer = await respond(client, story, body(cal, None, do="waits"))
        assert answer.status_code == 200, answer.text
        (narrator_prompt,) = [
            p for p in es.prompts(stub, "NarratorRender") if staged.OUTCOME_STEP not in p
        ]
        section = narrator_prompt.split("## Awaiting the game", 1)[1].split("\n## ", 1)[0]
        assert f"{REASON} (the game confirmed it)" in section
        await ps.drop_outbox_jobs(engine, story)
        await execute_in_tx(
            engine,
            (
                "DELETE FROM jobs WHERE story_id = :s AND kind = :k",
                {"s": story, "k": settlement.SETTLE},
            ),
        )


# -- #213: resume after a crash at each stage boundary ---------------------------------------

LEASE_S = 1.0
"""The child's idempotency and turn-slot leases: a retry takes a crashed original over soon."""
CRASHED = 137
DEDUPED_CASES: tuple[tuple[str, str | None], ...] = (
    ("staged.before_intent_commit", None),
    ("staged.after_intent", "intent_committed"),
    ("staged.after_claim", "awaiting_outcome"),
    ("staged.after_send", "awaiting_outcome"),
    ("staged.after_outcome", "outcome_recorded"),
    ("staged.after_completion", "completed"),
)
"""Each crash point of S-EFFECT-CRASH (1)-(6), with the stage the turn is left at."""
DONE_AT = {
    None: [],
    "intent_committed": ["intent"],
    "awaiting_outcome": ["intent", "claim"],
    "outcome_recorded": ["intent", "claim", "outcome"],
    "completed": ["intent", "claim", "outcome", "completion"],
}
STAGES = ["intent", "claim", "outcome", "completion", "response"]


async def _restart(
    child: EngineProcess, rules: list[StubRule], plan: crash.CrashPlan | None = None
) -> None:
    await child.kill()
    child.stub_rules = rules
    child.crash = plan
    await child.start()


async def _crashed(child: EngineProcess, story: sc.Staged, key: str) -> None:
    """Cal's purchase under ``key``, which the child dies of (its crash point fired)."""
    answer = await sc.buy(child, story, key)
    assert answer is None, answer.text if answer is not None else ""
    assert await child.wait_exit(10) == CRASHED


async def _retried(child: EngineProcess, story: sc.Staged, key: str) -> dict[str, Any]:
    """The same purchase retried under ``key`` once the crashed original's leases lapsed."""
    await asyncio.sleep(LEASE_S + 0.2)
    answer = await sc.buy(child, story, key)
    assert answer is not None
    assert answer.status_code == 200, answer.text
    return dict(answer.json())


async def _finished(db: AsyncEngine, child: EngineProcess, story: sc.Staged, key: str) -> Any:
    """Exactly one turn committed for the story and completed with every stage done once, one
    dispatch effect, one finished response stored under ``key``; answers the operation."""
    ops = await sc.staged_ops(db, story)
    committed = [o for o in ops if o.status == "committed"]
    assert len(committed) == 1, ops
    (op,) = committed
    assert op.turn_stage == "completed"
    assert sc.logged(op) == STAGES
    (row,) = await ps.outbox_rows(db, story.story)
    assert row.operation_id == op.operation_id
    keys = await rows(
        db,
        "SELECT status, operation_id, result IS NOT NULL AS stored FROM idempotency_keys "
        "WHERE scope_id = :s AND key = :k",
        s=story.story,
        k=key,
    )
    assert [(k.status, k.operation_id, k.stored) for k in keys] == [("done", op.operation_id, True)]
    async with httpx.AsyncClient(base_url=child.base_url, timeout=30) as client:
        view = await client.get(f"/operations/{op.operation_id}", headers=READ)
    assert view.status_code == 200, view.text
    assert [(s["name"], s["status"]) for s in view.json()["stages"]] == [
        (name, "done") for name in STAGES
    ]
    return op


async def _at_crash(db: AsyncEngine, story: sc.Staged, stage: str | None) -> None:
    """The turn as the crash left it: at ``stage`` (``None``: nothing of it committed), its
    operation view showing the stages done so far."""
    ops = await sc.staged_ops(db, story)
    if stage is None:
        assert [o.status for o in ops] == ["in_flight"], ops
        assert await ps.outbox_rows(db, story.story) == []
        return
    (op,) = ops
    assert (op.status, op.turn_stage) == ("committed", stage)
    view = await read_operation(db, op.operation_id)
    assert view.staged is True
    done = [s.name for s in view.stages if s.status.value == "done"]
    assert done == DONE_AT[stage], view.stages


@pytest.mark.part("M6", "FR-EXT-2,16,17,18,19,20; FR-CONC-6; FR-RESP-8")
async def test_s_effect_crash(pg_url: str, engine: AsyncEngine, states_delivered: None) -> None:
    """#213 (FR-EXT-17): a staged turn resumes from its last committed stage after a crash.

    A child engine plays "I buy the sword." on a synchronous, deduplicating provider (the
    ``GameServer`` in this process); it is killed at each stage boundary (``crash_at``) and the
    request retried with the same key:

    1. before intent: nothing committed; one fresh turn, one dispatch;
    2. after intent, before the claim: no regeneration; the dispatcher claims and sends once;
    3. after the claim, before the send: re-sent under the same provider key; one purchase;
    4. after the provider succeeded, before the outcome was recorded: the re-send is
       deduplicated (two requests, one purchase) and ``confirmed`` is recorded once;
    5. after the outcome, the outcome-step render failing every attempt: the purchase stays
       confirmed and the turn completes with the deterministic outcome narration, routed to
       the actor's place; and without a crash, the in-process render failing every attempt
       completes the same way;
    6. after completion, before the response was stored: the response is recomposed from the
       committed beats for the stored request's output viewpoint, identical on a second retry.

    In every case exactly one turn, one dispatch effect and one finished response exist, and
    the operation view shows the stage the crash left the turn at. **Non-deduplicating
    provider:** a crash after the claim moves the row to ``unknown``, listed for
    reconciliation, never re-sent (the server's count stays at its pre-crash value), and the turn
    completes narrating the purchase as still pending. Complement: a crash in an ordinary turn
    leaves no partial turn, and its retry commits one.

    The non-deduplicating case runs on a child of its own in ``api`` mode (#834): no job worker
    races the request's claim. In ``all`` mode the child's dispatcher could lock the row first;
    the request's claim then skipped it, the crash rolled the worker's claim back, and the row
    stayed ``intended``, never claimed or sent (correct, but not the case under test)."""
    with migrated_database(pg_url, f"epistrel_sec_{secrets.token_hex(4)}") as url:
        settings = auth_settings(url, model_adapter="stub")
        db = make_engine(settings)
        child = EngineProcess(url, CREDENTIAL_A, lease_s=LEASE_S, mode="all", deliver=["states"])
        try:
            async with ps.allowed_hosts(db, [ps.LOOPBACK]):
                await child.start()
                await _deduped_cases(db, child)
                await _render_failing(db, child)
                await child.kill()
                async with EngineProcess(
                    url, CREDENTIAL_A, lease_s=LEASE_S, mode="api", deliver=["states"]
                ) as alone:
                    await _non_deduplicating(db, alone)
                await child.start()
                await _ordinary_turn(db, child)
        finally:
            await child.kill()
            await db.dispose()


async def _deduped_cases(db: AsyncEngine, child: EngineProcess) -> None:
    for point, stage in DEDUPED_CASES:
        with ps.game_server() as server:
            failing = point == "staged.after_outcome"
            async with httpx.AsyncClient(base_url=child.base_url, timeout=60) as client:
                story = await sc.http_story(client, db, server, perceived=failing)
            rules = sc.purchase_rules(story.cal, failing=failing)
            await _restart(child, rules, crash.crash_at(point))
            key = f"sec-{point}"
            await _crashed(child, story, key)
            await _at_crash(db, story, stage)
            sent_before = len(server.keys)
            await _restart(child, rules)
            played = await _retried(child, story, key)
            op = await _finished(db, child, story, key)
            (row,) = await ps.outbox_rows(db, story.story)
            assert server.purchases == 1, point
            assert set(server.keys) == {row.id}, point  # one provider key throughout
            assert row.status == "confirmed", point
            confirmed = [
                o for o in await ps.outbox_outcomes(db, story.story) if o.kind == "confirmed"
            ]
            assert len(confirmed) == 1, point
            outcomes = await ps.external_outcomes(db, story.story)
            assert [o.payload["status"] for o in outcomes] == ["confirmed"], point
            text = played["message"]["text"]
            if point == "staged.after_claim":
                assert sent_before == 0
                assert len(server.keys) == 1  # sent once, by the resume
            elif point == "staged.after_send":
                assert sent_before == 1
                assert len(server.keys) == 2  # re-sent under the same key, deduplicated
            if point == "staged.after_outcome":
                assert DETERMINISTIC in text
                report = (await sc.staged_ops(db, story))[0]
                assert sc.logged(report) == STAGES
                await _located(db, story, op.operation_id)
            if point == "staged.after_completion":
                again = await sc.buy(child, story, key)
                assert again is not None
                assert again.status_code == 200, again.text
                assert again.headers.get("Idempotent-Replay") == "true"
                assert again.json() == played  # identical on a second retry
                assert played["output_viewpoint"] == f"character:{story.cal}"
                assert sp.WENT in text  # the original's rendered outcome step, recomposed
            await ps.drop_outbox_jobs(db, story.story)


DETERMINISTIC = f"Cal's {sp.REASON} went through."
PENDING = f"Cal's {sp.REASON} is still pending."


async def _located(db: AsyncEngine, story: sc.Staged, operation_id: str) -> None:
    """The deterministic narration is one narrator beat at the actor's place (the forge),
    routed per perceiver: a memory for each character there."""
    found = await rows(
        db,
        "SELECT id, payload FROM events WHERE operation_id = :o AND type = 'beat_narration' "
        "AND payload->>'text' = :t",
        o=operation_id,
        t=DETERMINISTIC,
    )
    assert len(found) == 1
    (forge,) = await rows(
        db, "SELECT id FROM entities WHERE story_id = :s AND kind = 'place'", s=story.story
    )
    assert found[0].payload["perception"]["location"] == forge.id
    holders = await rows(
        db,
        "SELECT holder_character_id AS h, fidelity FROM memories WHERE story_id = :s "
        "AND :e = ANY(source_event_ids) ORDER BY 1",
        s=story.story,
        e=found[0].id,
    )
    mira = next(i for i in await _cast(db, story.story) if i != story.cal)
    assert {r.h for r in holders} == {story.cal, mira}
    assert all(r.fidelity is not None for r in holders)


async def _cast(db: AsyncEngine, story_id: str) -> list[str]:
    found = await rows(db, "SELECT id FROM characters WHERE story_id = :s", s=story_id)
    return [r.id for r in found]


async def _render_failing(db: AsyncEngine, child: EngineProcess) -> None:
    """(5), without a crash: the outcome render fails every attempt in the request itself."""
    with ps.game_server() as server:
        async with httpx.AsyncClient(base_url=child.base_url, timeout=60) as client:
            story = await sc.http_story(client, db, server)
        await _restart(child, sc.purchase_rules(story.cal, failing=True))
        answer = await sc.buy(child, story, "sec-render")
        assert answer is not None
        assert answer.status_code == 200, answer.text
        assert DETERMINISTIC in answer.json()["message"]["text"]
        await _finished(db, child, story, "sec-render")
        report = await rows(
            db,
            "SELECT turn_report->'staged'->'outcome_step' AS step FROM operations "
            "WHERE story_id = :s AND turn_stage IS NOT NULL",
            s=story.story,
        )
        assert report[0].step["deterministic"] is True
        assert report[0].step["cause"] == "render_failed"
        assert server.purchases == 1
        await ps.drop_outbox_jobs(db, story.story)


async def _non_deduplicating(db: AsyncEngine, child: EngineProcess) -> None:
    with ps.game_server(dedupe=False) as server:
        async with httpx.AsyncClient(base_url=child.base_url, timeout=60) as client:
            story = await sc.http_story(client, db, server)
        rules = sc.purchase_rules(story.cal)
        await _restart(child, rules, crash.crash_at("staged.after_claim"))
        await _crashed(child, story, "sec-nodedupe")
        await _at_crash(db, story, "awaiting_outcome")
        assert server.keys == []
        await _restart(child, rules)
        played = await _retried(child, story, "sec-nodedupe")
        await _finished(db, child, story, "sec-nodedupe")
        assert PENDING in played["message"]["text"]
        (row,) = await ps.outbox_rows(db, story.story)
        assert row.status == "unknown"
        async with httpx.AsyncClient(base_url=child.base_url, timeout=30) as client:
            listed = await client.get(f"/stories/{story.story}/reconciliation", headers=READ)
        assert (row.id, "unknown") in {
            (e["outbox_id"], e["status"]) for e in listed.json()["listings"]
        }
        # The lease timer's turn: a non-deduplicating provider is never re-sent.
        await execute_in_tx(
            db,
            (
                "UPDATE external_effect_outbox SET claim_lease_until = now() - interval '1 s' "
                "WHERE id = :o",
                {"o": row.id},
            ),
        )
        assert await dispatcher.expire(db, row.id) == "unknown"
        assert server.keys == []  # never sent: the pre-crash count
        assert server.purchases == 0
        await ps.drop_outbox_jobs(db, story.story)


async def _ordinary_turn(db: AsyncEngine, child: EngineProcess) -> None:
    """Complement (#75): a crash in an ordinary turn's commit leaves nothing of it."""
    async with httpx.AsyncClient(base_url=child.base_url, timeout=60) as client:
        story_id, ids = await structured_story(client, "Cal", "Mira")
    story = await sc.settled(db, sc.Staged(story_id, ids["Cal"], ""))
    await _restart(child, sc.plain_rules(), crash.crash_at("before_commit", 1, "turn"))
    await _crashed(child, story, "sec-plain")
    (dead,) = await sc.staged_ops(db, story)
    assert dead.status == "in_flight"
    assert (
        await rows(db, "SELECT id FROM events WHERE operation_id = :o", o=dead.operation_id) == []
    )
    await _restart(child, sc.plain_rules())
    await _retried(child, story, "sec-plain")
    ops = await sc.staged_ops(db, story)
    committed = [o for o in ops if o.status == "committed"]
    assert len(committed) == 1
    assert committed[0].turn_stage is None


HOLD_S = 0.5
"""How long the race's held claim keeps the row locked while the truncation waits on it."""


async def _holder(engine: AsyncEngine, story: str) -> Any:
    (found,) = await rows(engine, "SELECT in_flight_turn FROM stories WHERE id = :s", s=story)
    return found.in_flight_turn


async def _truncate(client: Any, story: str, turn: int) -> Any:
    return await client.post(
        f"/stories/{story}/truncate", json={"to_turn": turn - 1}, headers=WRITE
    )


@pytest.mark.part("M6", "FR-EXT-17,18")
async def test_s_effect_crash__race(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#213 (FR-EXT-17): a discard racing the staged turn's claim, on two connections with
    injected delays.

    1. **One instant before the claim.** The turn's intent committed; its claim is delayed until
       a truncation committed. The effect is cancelled (never sent, nothing to reconcile), the
       turn abandoned, its slot given back at the truncation, and the respond answers 409
       ``timeline_changed``.
    2. **One instant after the claim.** The claim's transaction holds the row lock while a
       truncation starts: the truncation waits on the lock (the row lock serializes the two),
       then returns the effect ``reconciliation_required`` although no bytes were sent yet; the
       turn is abandoned, its slot given back, the row listed. Complement: the effect is never
       ``cancelled``."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        # -- 1. A truncation one instant before the claim.
        story, cal, _, fake = await _sync_story(client, engine, epistrel)
        sp.script_purchase(stub, cal)
        arrived, gate = asyncio.Event(), asyncio.Event()
        claim = dispatcher.claim

        async def late(db: Any, outbox_id: str, **kwargs: Any) -> Any:
            arrived.set()
            await gate.wait()
            return await claim(db, outbox_id, **kwargs)

        with monkeypatch.context() as patched:
            patched.setattr(dispatcher, "claim", late)
            playing = asyncio.create_task(sp.buy(client, story, cal))
            await asyncio.wait_for(arrived.wait(), 60)
            op = await sp.staged_op(engine, story)
            assert op.turn_stage == "intent_committed"
            cut = await _truncate(client, story, op.narrative_turn)
            assert cut.status_code == 200, cut.text
            assert cut.json()["reconciliation_required"] == []
            assert await _holder(engine, story) is None  # the slot given back at once
            gate.set()
            answer = await asyncio.wait_for(playing, 60)
        assert answer.status_code == 409, answer.text
        assert answer.json()["error"]["code"] == "timeline_changed"
        (row,) = await ps.outbox_rows(engine, story)
        assert row.status == "cancelled"
        assert fake.writes == []
        assert (await sp.staged_op(engine, story)).turn_stage == "abandoned"
        await ps.drop_outbox_jobs(engine, story)

        # -- 2. A truncation one instant after the claim, before any byte is sent.
        story, cal, _, fake = await _sync_story(client, engine, epistrel)
        sp.script_purchase(stub, cal)
        locked, hold = asyncio.Event(), asyncio.Event()
        sending, send_gate = asyncio.Event(), asyncio.Event()
        mark_claimed = core_outbox.mark_claimed
        send = dispatcher.send

        async def held(conn: Any, *args: Any, **kwargs: Any) -> Any:
            found = await mark_claimed(conn, *args, **kwargs)
            locked.set()
            await hold.wait()  # the claim's transaction keeps the row locked
            return found

        async def gated(db: Any, claimed: Any) -> Any:
            sending.set()
            await send_gate.wait()
            return await send(db, claimed)

        with monkeypatch.context() as patched:
            patched.setattr(core_outbox, "mark_claimed", held)
            patched.setattr(dispatcher, "send", gated)
            playing = asyncio.create_task(sp.buy(client, story, cal))
            await asyncio.wait_for(locked.wait(), 60)
            op = await sp.staged_op(engine, story)
            cutting = asyncio.create_task(_truncate(client, story, op.narrative_turn))
            await asyncio.sleep(HOLD_S)
            assert not cutting.done()  # waiting on the claim's row lock
            hold.set()
            cut = await asyncio.wait_for(cutting, 60)
            assert cut.status_code == 200, cut.text
            (row,) = await ps.outbox_rows(engine, story)
            assert [e["outbox_id"] for e in cut.json()["reconciliation_required"]] == [row.id]
            assert fake.writes == []  # no byte sent yet
            assert row.status == "claimed"
            assert await _holder(engine, story) is None
            send_gate.set()
            answer = await asyncio.wait_for(playing, 60)
        assert answer.status_code == 409, answer.text
        assert (await sp.staged_op(engine, story)).turn_stage == "abandoned"
        listed = (await core_outbox.listings(engine, story)).listings
        assert ("reconciliation_required", row.id) in {(e.reason, e.outbox_id) for e in listed}
        (row,) = await ps.outbox_rows(engine, story)
        assert row.status != "cancelled"
        await ps.drop_outbox_jobs(engine, story)
        await ss.set_level(engine, story, None)
