"""P-11's large-cast scenario (#829, FR-MULTI-12): the builder and the measurement.

``build`` makes a structured story of ``size`` full characters through REST on the stub: the
player, ``PRIMARIES`` primaries (participation ``primary``, fixed) and everyone else ``silent``
or, for the reactive variant, ``reactive`` (fixed). Everyone stands in one room. The story runs
per-character renders under per-character partitions, perception ``co_location`` and testimony
``defaulted`` (the key room's setup, ``tests/fixtures/key_room.py``), with the call budget raised
to ``MODEL_CALLS`` so no primary is reduced. Each turn the player speaks and the three primaries
render ``sequential``ly.

``measure`` plays ``WARMUP`` turns and then ``TURNS`` measured ones, and records the following:

- **engine-only turn time:** the respond call's wall clock with the stub at 0 ms, as nearest-rank
  percentiles (``epistrel.testing.perf.percentile``);
- **prompt size:** the mean primary render's messages, and the total over all of a turn's calls,
  in characters and in tokens by ``orchestrator.structured.prompt.tokens`` (the budgeter's
  counter);
- **commit statements:** the statements executed inside the structured commit
  (``write_turn_body`` on the awaiting stack), as mean and max per turn.

A failed, lost or reduced turn raises (``TurnProblem``). None of the figures is gated.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from statistics import mean
from typing import Any

import httpx2 as httpx
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.tables.stories import INITIAL_TURN_INDEX, stories
from epistrel.orchestrator.models.stub import StubAdapter, StubCall
from epistrel.orchestrator.structured import prompt
from epistrel.testing import perf
from epistrel.testing.fixtures import scale
from epistrel.testing.profiles import structured_m4
from tests import multi_support as ms
from tests import perception_support as ps
from tests import testimony_support as ts
from tests.auth_support import bearer
from tests.respond_support import body, respond
from tests.story_support import WRITE, character, create

PRIMARIES = 3
WARMUP = 3
TURNS = 20
SIZES = (10, 20, 50, 100)
REACTIVE_SIZE = 50
MODEL_CALLS = 50
"""The story's ``budgets.model_calls``, at its ceiling: well above three primaries' default
(4 + 5 per primary), so budget planning never reduces one."""
RENDER_SCHEMA = "CharacterRender"
OPERATOR = {**bearer(), "X-Epistrel-Principal": "p-operator"}
SAY = "Where were you all last night?"


class TurnProblem(AssertionError):
    """A turn failed, was lost or reduced a primary: the scenario is void."""


@dataclass(frozen=True)
class Cast:
    story_id: str
    size: int
    others_role: str
    player: str
    primaries: tuple[str, ...]
    others: tuple[str, ...]
    names: dict[str, str]
    """Character id → name."""


@dataclass
class Measured:
    """One cast's figures (``measure``)."""

    size: int
    others_role: str
    turns: int
    turn_ms: dict[str, float]
    primary_render: dict[str, float]
    turn_prompt: dict[str, float]
    calls_per_turn: dict[str, float]
    commit_statements: dict[str, float]
    samples_ms: list[float] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def roles(size: int, others_role: str = "silent") -> list[dict[str, Any]]:
    """The ``POST /stories`` cast: the player, ``PRIMARIES`` fixed primaries, and the rest fixed
    ``others_role``."""
    if size < PRIMARIES + 1:
        raise ValueError(f"a cast of {size} has no room for {PRIMARIES} primaries")
    found = []
    for n, name in enumerate(scale.person_names(size)):
        if n == 0:
            found.append(character(name, 30, is_player=True))
            continue
        role = "primary" if n <= PRIMARIES else others_role
        found.append(character(name, 30, participation={"role": role, "fixed": True}))
    return found


async def set_cast_limit(client: httpx.AsyncClient, value: int) -> int:
    """``max_story_cast`` set to ``value``; returns the value it replaced."""
    found = await client.get("/admin/settings", headers=bearer())
    assert found.status_code == 200, found.text
    before = int(found.json()["max_story_cast"])
    response = await client.put("/admin/settings", json={"max_story_cast": value}, headers=OPERATOR)
    assert response.status_code == 200, response.text
    return before


async def build(
    client: httpx.AsyncClient, engine: AsyncEngine, size: int, others_role: str = "silent"
) -> Cast:
    """The scenario's story (see the module docstring)."""
    created = await create(client, {"name": "A large cast", "characters": roles(size, others_role)})
    story = created["story_id"]
    response = await client.put(
        f"/stories/{story}/engine-profile",
        json={"profile_id": structured_m4(), "version": 1, "ignore_warnings": True},
        headers=WRITE,
    )
    assert response.status_code == 200, response.text
    await ts.enable_testimony(engine, story, "defaulted", level="co_location")
    await ms.per_character(engine, story, model_calls=MODEL_CALLS)
    room = await ps.place_entity(engine, story, "the long hall")
    ids = [c["id"] for c in created["characters"]]
    for character_id in ids:
        await ps.put(engine, story, character_id, room)
    return Cast(
        story_id=story,
        size=size,
        others_role=others_role,
        player=ids[0],
        primaries=tuple(ids[1 : 1 + PRIMARIES]),
        others=tuple(ids[1 + PRIMARIES :]),
        names={c["id"]: c["name"] for c in created["characters"]},
    )


def _text(call: StubCall) -> str:
    return "\n".join(m.content for m in call.messages)


def renders_of(stub: StubAdapter, name: str) -> list[StubCall]:
    """``name``'s character renders in the stub's call log."""
    marker = f"## You are {name}\n"
    return [
        c
        for c in stub.calls
        if c.purpose == "generation"
        and c.response_schema == RENDER_SCHEMA
        and any(marker in m.content for m in c.messages)
    ]


def _in_commit() -> bool:
    """Whether the statement being executed is the structured commit's (``write_turn_body`` is
    on the awaiting stack; SQLAlchemy runs it in a greenlet whose parent holds the coroutine
    frames). The same probe as ``tests/integration/test_turn_read_counts.py``'s (#620)."""
    import greenlet  # type: ignore[import-untyped]

    parent = greenlet.getcurrent().parent
    frame = parent.gr_frame if parent is not None else None
    while frame is not None:
        if frame.f_code.co_name == "write_turn_body":
            return True
        frame = frame.f_back
    return False


@contextmanager
def commit_statements(engine: AsyncEngine) -> Iterator[list[int]]:
    """Counts the statements the structured commit executes while inside."""
    found = [0]

    def count(*_: Any) -> None:
        if _in_commit():
            found[0] += 1

    event.listen(engine.sync_engine, "before_cursor_execute", count)
    try:
        yield found
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count)


def _problems(cast: Cast, turn: dict[str, Any]) -> list[str]:
    problems = []
    for key in ("reduced_characters", "deferred_characters", "escalation_deferred"):
        if turn.get(key):
            problems.append(f"{key}: {turn[key]}")
    if "primaries_reduced" in (turn.get("degradation") or []):
        problems.append("primaries_reduced")
    return problems


async def play(client: httpx.AsyncClient, cast: Cast) -> dict[str, Any]:
    """One turn: the player speaks, the three primaries render. Raises ``TurnProblem`` when it
    failed or reduced a primary."""
    answer = await respond(
        client,
        cast.story_id,
        body(cast.player, SAY, render_character_ids=list(cast.primaries)),
    )
    if answer.status_code != 200:
        raise TurnProblem(f"cast {cast.size}: {answer.status_code} {answer.text[:500]}")
    turn: dict[str, Any] = answer.json()
    problems = _problems(cast, turn)
    if problems:
        raise TurnProblem(f"cast {cast.size}: {problems}")
    return turn


async def committed_turns(engine: AsyncEngine, story_id: str) -> int:
    """The story's committed turns (``stories.next_turn_index`` less the first index)."""
    async with engine.connect() as conn:
        found = await conn.scalar(select(stories.c.next_turn_index).where(stories.c.id == story_id))
    assert found is not None, story_id
    return int(found) - INITIAL_TURN_INDEX


def _summary(values: Sequence[float]) -> dict[str, float]:
    return {"mean": round(mean(values), 1), "max": float(max(values))}


async def measure(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    stub: StubAdapter,
    cast: Cast,
    *,
    warmup: int = WARMUP,
    turns: int = TURNS,
) -> Measured:
    """The cast's figures over ``turns`` measured turns after ``warmup`` (module docstring)."""
    for _ in range(warmup):
        stub.reset()
        await play(client, cast)
    samples: list[float] = []
    render_chars: list[int] = []
    render_tokens: list[int] = []
    turn_chars: list[int] = []
    turn_tokens: list[int] = []
    calls: list[int] = []
    statements: list[int] = []
    primary_names = [cast.names[p] for p in cast.primaries]
    for _ in range(turns):
        stub.reset()
        with commit_statements(engine) as found:
            started = time.perf_counter()
            await play(client, cast)
            samples.append((time.perf_counter() - started) * 1000)
        statements.append(found[0])
        for name in primary_names:
            rendered = renders_of(stub, name)
            if len(rendered) != 1:
                raise TurnProblem(f"cast {cast.size}: {name} rendered {len(rendered)} times")
            text = _text(rendered[0])
            render_chars.append(len(text))
            render_tokens.append(prompt.tokens(text))
        texts = [_text(c) for c in stub.calls]
        turn_chars.append(sum(len(t) for t in texts))
        turn_tokens.append(sum(prompt.tokens(t) for t in texts))
        calls.append(len(stub.calls))
    played = await committed_turns(engine, cast.story_id)
    if played != warmup + turns:
        raise TurnProblem(f"cast {cast.size}: {played} turns committed of {warmup + turns}")
    return Measured(
        size=cast.size,
        others_role=cast.others_role,
        turns=turns,
        turn_ms={
            "p50": round(perf.percentile(samples, 50), 1),
            "p95": round(perf.percentile(samples, 95), 1),
        },
        primary_render={"chars": round(mean(render_chars)), "tokens": round(mean(render_tokens))},
        turn_prompt={"chars": round(mean(turn_chars)), "tokens": round(mean(turn_tokens))},
        calls_per_turn=_summary(calls),
        commit_statements=_summary(statements),
        samples_ms=[round(s, 1) for s in samples],
    )


# -- the results document and page -----------------------------------------------------------------

EXTRAPOLATE_TO = 200
"""``STORY_CAST_LIMIT_MAX``: the fit's stated extrapolation."""
FITTED = {
    "turn_ms_p50": ("turn_ms", "p50"),
    "turn_ms_p95": ("turn_ms", "p95"),
    "primary_render_tokens": ("primary_render", "tokens"),
    "turn_prompt_tokens": ("turn_prompt", "tokens"),
    "commit_statements_mean": ("commit_statements", "mean"),
}


def linear_fit(points: Sequence[tuple[float, float]]) -> dict[str, float]:
    """The least-squares line through ``points`` (cast size, figure), with its value at
    ``EXTRAPOLATE_TO``."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mx, my = mean(xs), mean(ys)
    spread = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in points) / spread
    intercept = my - slope * mx
    return {
        "per_character": round(slope, 3),
        "intercept": round(intercept, 1),
        f"at_{EXTRAPOLATE_TO}": round(intercept + slope * EXTRAPOLATE_TO, 1),
    }


def fits(silent: Sequence[Measured]) -> dict[str, dict[str, float]]:
    """``FITTED``'s lines over the silent casts."""
    return {
        name: linear_fit([(m.size, float(getattr(m, group)[key])) for m in silent])
        for name, (group, key) in FITTED.items()
    }


def document(measured: Sequence[Measured], host: dict[str, Any]) -> dict[str, Any]:
    silent = [m for m in measured if m.others_role == "silent"]
    return {
        "scenario": "P-11 large casts (#829)",
        "host": host,
        "setup": {
            "primaries": PRIMARIES,
            "warmup_turns": WARMUP,
            "measured_turns": measured[0].turns if measured else 0,
            "model_calls_budget": MODEL_CALLS,
            "renders": "per_character",
            "partitions": True,
            "perception": "co_location",
            "testimony": "defaulted",
            "stub_latency_ms": 0,
        },
        "casts": [m.as_dict() for m in measured],
        "fit": fits(silent) if len(silent) >= 2 else {},
    }


def _row(m: Measured) -> str:
    return (
        f"| {m.size} | {m.others_role} | {m.turn_ms['p50']:.0f} | {m.turn_ms['p95']:.0f} "
        f"| {m.primary_render['chars']:,} / {m.primary_render['tokens']:,} "
        f"| {m.turn_prompt['chars']:,} / {m.turn_prompt['tokens']:,} "
        f"| {m.calls_per_turn['mean']:g} "
        f"| {m.commit_statements['mean']:g} / {m.commit_statements['max']:g} |"
    )


def page(found: dict[str, Any], measured: Sequence[Measured]) -> str:
    """The Markdown report committed beside the JSON."""
    host = found["host"]
    setup = found["setup"]
    lines = [
        f"# P-11 Large casts: {found.get('measured_at', '')}",
        "",
        "Documented figures, not a gate (#829, FR-MULTI-12). "
        f"Host: {host['host']} (not the reference host); {host['platform']}, "
        f"{host['vcpus']} vCPUs, {host['ram_gb']} GB RAM; Postgres {host.get('postgres', '?')}.",
        "",
        f"Each cast is a fresh structured story: the player, {setup['primaries']} fixed "
        "primaries and every other character fixed `silent` (or `reactive` in the variant), "
        "all in one room. Per-character renders, per-character partitions, perception "
        "`co_location`, testimony `defaulted`, call budget "
        f"{setup['model_calls_budget']} (no primary reduced). Each turn the player speaks and "
        f"the {setup['primaries']} primaries render sequentially. {setup['warmup_turns']} "
        f"warm-up turns, then {setup['measured_turns']} measured turns, on the stub model at "
        "0 ms, so the time is the engine's own (nearest-rank percentiles).",
        "",
        "| Cast | Others | Turn p50 (ms) | Turn p95 (ms) | Primary render prompt (chars / tokens) "
        "| All calls in a turn (chars / tokens) | Model calls per turn "
        "| Commit statements (mean / max) |",
        "|---|---|---|---|---|---|---|---|",
        *(_row(m) for m in measured),
        "",
        "Tokens are counted by the engine's budgeter (`orchestrator/structured/prompt.tokens`, "
        "four characters a token), not a model's tokenizer.",
    ]
    if found["fit"]:
        lines += [
            "",
            f"## Linear fit over the silent casts, extrapolated to {EXTRAPOLATE_TO}",
            "",
            f"| Figure | Per extra character | At {EXTRAPOLATE_TO} characters (extrapolated) |",
            "|---|---|---|",
        ]
        for name, line in found["fit"].items():
            lines.append(
                f"| {name} | {line['per_character']:g} | {line[f'at_{EXTRAPOLATE_TO}']:g} |"
            )
    lines += ["", "Raw data: the JSON beside this file.", ""]
    return "\n".join(lines)
