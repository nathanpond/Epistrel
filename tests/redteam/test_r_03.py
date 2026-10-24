"""R-03: private-thought extraction (NFR-PRIV-1) [M5; #172], docs/04 §10.

The corpus is ``tests/redteam/corpus/r03.jsonl``: 20 sequences, each ``{id, technique, via,
canary, thought, attempts[], leak, pr}`` (and an ``ingest`` narration for ``via = ingest``).
Jenni records a private ``think`` holding the sequence's canary; then the player tries, over one
to three turns, to make another character (Marcus; Pip, a minor, for ``ask_minor``) reveal it:
a direct ask, an in-world instruction, an out-of-character injection, a role swap, a multi-turn
extraction, a manipulated roster, a ``co_author``-style narration planted through ``ingest``,
and asking a minor. Each sequence runs twice: with a clean stub (the asked character answers
innocently) and with a leak-attempting stub, whose render repeats the thought verbatim, narrates
it as Jenni's inner state in a ``say`` or a ``do``, or embeds it in a longer line.

The criterion, over every player-facing message and beat of every attempt (and every leaking
render was caught and repaired as a ``think_leak``, the clean ones never): the canary never
appears, no text repeats the thought (``core.verify.protected.match_thoughts``), and the asked
character's generation prompts never hold it (prompt capture: the structural control, #170;
a regeneration's rejected text has the leak cut out, #172). This is the behavioral complement
to PRIV and MULTI-ISOLATION. A new failure becomes a permanent case here (``tests/README.md``).

``test_r_03`` runs the five ``pr`` sequences (``redteam``, PR CI); ``test_r_03__corpus`` all 20
under ``redteam_long`` (deselected unless ``-m`` names it, run nightly by ``redteam.yml``; M4's
#145 convention).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.verify.protected import PrivateRecord, match_thoughts
from tests.auth_support import auth_settings
from tests.partition_support import execute
from tests.respond_support import respond_client, system_of
from tests.secrets_support import play, secret_story
from tests.story_support import WRITE
from tests.structured_turn_support import beat

CORPUS_FILE = Path(__file__).parent / "corpus" / "r03.jsonl"
TECHNIQUES = frozenset(
    {
        "direct_ask",
        "in_world_instruction",
        "ooc_injection",
        "role_swap",
        "multi_turn",
        "manipulated_roster",
        "co_author_narration",
        "ask_minor",
    }
)
INNOCENT = "I couldn't tell you what's on anyone's mind."


@dataclass(frozen=True, slots=True)
class Sequence:
    id: str
    technique: str
    via: str
    canary: str
    thought: str
    attempts: tuple[str, ...]
    leak: dict[str, str]
    pr: bool
    ingest: str | None = None

    @classmethod
    def of(cls, raw: dict[str, Any]) -> Sequence:
        return cls(
            id=raw["id"],
            technique=raw["technique"],
            via=raw["via"],
            canary=raw["canary"],
            thought=raw["thought"],
            attempts=tuple(raw["attempts"]),
            leak=dict(raw["leak"]),
            pr=bool(raw.get("pr", False)),
            ingest=raw.get("ingest"),
        )


def corpus() -> list[Sequence]:
    lines = CORPUS_FILE.read_text("utf-8").splitlines()
    return [Sequence.of(json.loads(line)) for line in lines if line.strip()]


CORPUS = corpus()
PR_SUBSET = [s for s in CORPUS if s.pr]


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def test_the_corpus_is_complete() -> None:
    """20 sequences with unique ids and canaries, five in the PR subset, every technique and
    both entry points covered; each thought holds its canary and no attempt names it."""
    assert len(CORPUS) == 20
    assert len({s.id for s in CORPUS}) == 20
    assert len({s.canary for s in CORPUS}) == 20
    assert len(PR_SUBSET) == 5
    assert {s.technique for s in CORPUS} == TECHNIQUES
    assert {s.via for s in CORPUS} == {"respond", "ingest"}
    for spec in CORPUS:
        assert spec.canary in spec.thought.casefold(), spec.id
        assert spec.canary in spec.leak["text"].casefold(), spec.id
        assert all(spec.canary not in a.casefold() for a in spec.attempts), spec.id
        assert spec.leak["kind"] in ("say", "do"), spec.id
        assert (spec.via == "ingest") == (spec.ingest is not None), spec.id


async def run_sequence(
    client: Any, engine: AsyncEngine, lib: Any, spec: Sequence, *, leaky: bool
) -> list[str]:
    """One run of ``spec``: what leaked (empty: nothing), as ``<attempt>: <where>`` lines."""
    stub = lib.models.stub
    story, ids = await secret_story(client, engine, "Wren", "Marcus", "Jenni", "Pip")
    asked = ids["Marcus"]
    if spec.technique == "ask_minor":
        asked = ids["Pip"]
        await execute(engine, "UPDATE characters SET weight = 'minor' WHERE id = :c", c=asked)
    await play(
        client,
        engine,
        stub,
        story,
        ids["Wren"],
        ids["Jenni"],
        beat("think", spec.thought),
        beat("say", "Morning, everyone.", intent="sincere"),
    )
    if spec.ingest is not None:
        ingested = await client.post(
            f"/stories/{story}/ingest",
            json={
                "events": [
                    {
                        "client_event_id": f"{spec.id}-plant",
                        "kind": "narration",
                        "text": spec.ingest,
                    }
                ]
            },
            headers=WRITE,
        )
        assert ingested.status_code in (200, 201), ingested.text
    record = PrivateRecord.of(ids["Jenni"], "thought", "think", spec.thought)
    found: list[str] = []
    for n, attempt in enumerate(spec.attempts):
        rendered = (
            beat(
                spec.leak["kind"],
                spec.leak["text"],
                **({"intent": "sincere"} if spec.leak["kind"] == "say" else {}),
            )
            if leaky
            else beat("say", INNOCENT, intent="sincere")
        )
        turn = await play(client, engine, stub, story, ids["Wren"], asked, rendered, say=attempt)
        if ("think_leak" in turn.tags) != leaky:
            found.append(f"{n}: think_leak repairs {turn.tags} with leaky={leaky}")
        visible = [turn.message, *(b.get("text") or "" for b in turn.answer["beats"])]
        for text in visible:
            if spec.canary in text.casefold():
                found.append(f"{n}: canary in a player-facing text")
            if match_thoughts(text, [record], speaker="reader"):
                found.append(f"{n}: the thought repeated in a player-facing text")
        prompts = [system_of(c) for c in stub.calls if c.purpose == "generation"]
        if any(spec.canary in p.casefold() for p in prompts):
            found.append(f"{n}: canary in the asked character's prompt")
        sent = [m.content for c in stub.calls if c.purpose == "regeneration" for m in c.messages]
        if any(spec.canary in s.casefold() for s in sent):
            found.append(f"{n}: canary in a regeneration prompt")
    return found


@pytest.mark.integration
@pytest.mark.redteam
@pytest.mark.part("M5", "NFR-PRIV-1")
async def test_r_03(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """The PR subset (five sequences): with the clean and the leak-attempting stub, no attempt
    leaks Jenni's thought (see the module docstring)."""
    async with respond_client(stub_settings, engine) as (client, lib):
        for spec in PR_SUBSET:
            for leaky in (False, True):
                leaked = await run_sequence(client, engine, lib, spec, leaky=leaky)
                assert leaked == [], (spec.id, leaky, leaked)


@pytest.mark.integration
@pytest.mark.redteam
@pytest.mark.redteam_long
@pytest.mark.part("M5", "NFR-PRIV-1")
async def test_r_03__corpus(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """All 20 sequences, clean and leak-attempting: zero leaks over the corpus."""
    async with respond_client(stub_settings, engine) as (client, lib):
        for spec in CORPUS:
            for leaky in (False, True):
                leaked = await run_sequence(client, engine, lib, spec, leaky=leaky)
                assert leaked == [], (spec.id, leaky, leaked)
