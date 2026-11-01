"""Measure one model tier and record it in the model matrix (NFR-PORT-3, NFR-PERF-8;
docs/04 C-07, E-06, P-08).

    uv run python -m epistrel.tools.model_matrix --base-url http://localhost:1234/v1 \\
        --model <loaded model> [--api-key lm-studio] [--tier local] \\
        [--hardware "Apple M3 Max, 128 GB"] [--server-version "LM Studio 0.4"] \\
        [--purpose-model guardrail_check=<model>] [--database-url postgresql+psycopg://...]

The tool runs an in-process ``Engine`` on the ``openai`` adapter against the endpoint. The
database is a throwaway PostgreSQL started through testcontainers (Docker), or
``--database-url``, which should point at a scratch database because the run writes stories
into it. The tool then:

1. probes **protocol** compatibility (``GET /models`` and one completion). An unreachable or
   incompatible endpoint exits 1 and writes nothing;
2. checks each delivered purpose's **capability floor** (``capability.assess``) and E-06's
   prompt-parameter checks (``capability.check_prompt_params``);
3. for each row, *policies off* and *minors policy on*, runs one warm-up turn and then
   ``--turns`` (default 20) Lite turns with scripted, benign inputs, timing each one and
   counting its model calls by purpose (P-08) against the calls the turn was designed to make
   (#303); then the same for single-character structured turns (``--turn-kinds``, default
   ``lite,structured``; P-08 M4, #153).

**Turn kinds** (#153). A structured row plays ``StructuredDriver``'s turns on a two-character
story on the seeded ``Structured`` profile's latest version, with planted canon
(``testing.structured_load``: each character owns a brass lantern); the player acts and the
other character renders. A repair-heavy row plays the same turns while half the stub's renders deny
owning the lantern, so the repair ladder reaches rung 3 (a ``regeneration`` call). It needs the
stub's scripted contradictions, so only ``run_matrix(turn_kinds=…)`` on a stub Engine makes it
(``tests/perf/test_p_08.py::test_p_08__structured``); a row's key carries its kind unless Lite.
Structured and repair-heavy rows run under a recorded per-story turn deadline override of
``TURN_DEADLINE_S`` (#454), so a slow tier is measured rather than cut by the profile's deadline;
the override scales with the turn's primaries (``measurement_deadline_ms``, #588).

**Three-character turns** (P-08 M5, #182). A ``three_character_sequential`` row plays
``ThreeCharacterDriver``'s turns: S-SEQUENTIAL's key room (Cal, the player, with Bobby and Jenni)
plus Marcus, all four placed in the study, on the seeded ``Structured`` profile's latest version
(per-character renders, perception ``full``); the player speaks and the three primaries render
``sequential``ly in request order, under a recorded story override of ``budgets.model_calls``
(``THREE_CHARACTER_CALLS``) and the measurement deadline scaled to three primaries
(``THREE_CHARACTER_DEADLINE_MS``, #588). Each turn's design is #156's plan for
three renders (``three_character_design``: a render and an extraction per primary, the composed
message's detection, the salience pass per render and the polish, plus the reserved guardrail
calls), so on the stub its calls per turn equal the plan. A ``three_character_director`` row adds
the director's ordering call (``sequence_order = director``), and a ``three_character_default``
row runs under the profile's own ``budgets.model_calls`` (no override), reporting how often
budget planning reduced or deferred a primary (``Row.planning``). Each row records
the profile and version its turns ran under and the perception level of their beats, from the
respond results. ``--turn-kinds three_character_sequential`` runs it against a live tier.

**Embedding** (#138, C-07 M4). With ``--embedding-base-url`` and ``--embedding-model`` the run
also measures that ``/embeddings`` endpoint through the embedding port
(``ModelAccessLayer.embeddings``): protocol compatibility (``GET /models`` and one embedding),
the model it reports, its dimension, the largest batch (of 64, 16 and 1 texts) it embeds, and
the p50 of ``EMBEDDING_CALLS`` single-text calls. Its row (``embedding_rows`` in the JSON) is
keyed by tier and model; ``embedding_row`` measures any ``EmbeddingService``, the stub's in CI.

The rows go to ``docs/model-matrix.json`` (raw timings included) and are rendered into
``docs/model-matrix.md``. A row is keyed by tier, model and policies, so a re-run replaces its
own rows and keeps every other tier's. The matrix is **reported, not gated**: CI never runs
it, and the owner commits the files after a local run. Percentiles are nearest-rank.

**Turn path.** A Lite turn is a real ``Engine.respond`` turn (``RespondDriver``, #57): the
input gate for every model-assisted input policy, one ``generation`` call, then the output gate
(#68) for every model-assisted output policy. The *minors policy on* row casts a 12-year-old, so
the deployment's seeded ``minors_no_sexual_content`` policy (#55) applies through the story's
own guard context: one ``guardrail_check`` on the input and one on the output per turn. Rows
measured before #57 went through #53's guarded reply (``GuardedReplyDriver``, an in-memory
unconditioned policy checked on input and output). Each row records the turn path it measured,
and the md notes a pending re-run only while a guarded-reply row remains.

**Call breakdown (#303).** The mean calls per turn folds every attempt into one number, so each
turn also reports its design (``TurnCalls``): one ``generation`` call plus the guardrail calls G
its budget reserved (#69). A row records, beside the mean, the designed calls per turn and the
extra calls beyond them by purpose, the failed calls by purpose and outcome, the turns that hit
a model-call deadline or error, and the safe fallbacks by cause (``call_breakdown``). The extra
calls are structured retries, the empty-reply retry, output-gate regenerations with their
re-checks, and revision regenerations. A call the deadline cuts is never retried. If it is an
output check or a regeneration, the turn releases the safe fallback (cause ``deadline``). If it
is the input check or the first generation, the turn fails: the run writes the rows that
finished and marks that row failed (``RowFailure``, ``failed_rows`` in the JSON, #454), never a
partial row. A row
measured before the breakdown holds ``null`` in those fields, and the md shows them as *not
recorded* until that tier's next live run. P-08 asserts each row's design with
``design_problems`` and reports the extra calls instead of failing on them (#308): they are
bounded by each turn's call cap, and a check skipped or a draft released unchecked without a
deadline or error is still a failure.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from cryptography.fernet import Fernet

from epistrel.config import Settings
from epistrel.core.context import RequestContext
from epistrel.core.db import make_engine
from epistrel.core.errors import EpistrelError, RevisionConflict
from epistrel.core.policies.model import ModelAssistedRule, Policy
from epistrel.core.policies.model import Target as PolicyTarget
from epistrel.core.profiles.schema import MAX_DEADLINE_MS
from epistrel.core.profiles.story import ProfileOverridesPatch
from epistrel.core.stories import CharacterSpec, StorySpec, create_story_from
from epistrel.engine import Engine
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.embeddings.service import EmbeddingService
from epistrel.orchestrator.lite.schemas import SAFE_FALLBACK, PlayerInputBody, RespondRequest
from epistrel.orchestrator.models import config as model_config
from epistrel.orchestrator.models.capability import (
    CapabilityReport,
    PromptParamsResult,
    assess,
    check_prompt_params,
)
from epistrel.orchestrator.models.errors import ModelError
from epistrel.orchestrator.models.metrics import CallLog, ModelCall, recording
from epistrel.orchestrator.models.registry import (
    EXTRACTION_BACKSTOP,
    GENERATION,
    GUARDRAIL_CHECK,
    SALIENCE_DETECTION,
    STORY_PURPOSES,
)
from epistrel.orchestrator.models.stub import StubAdapter
from epistrel.orchestrator.policies.output_gate import DEADLINE
from epistrel.tools.generated import REPO_ROOT

MATRIX_PATH = REPO_ROOT / "docs" / "model-matrix.md"
RAW_PATH = REPO_ROOT / "docs" / "model-matrix.json"
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
PG_IMAGE = "pgvector/pgvector:pg16"
PRINCIPAL = "model-matrix"
DEFAULT_TURNS = 20
TURN_DEADLINE_S = 300.0
"""The measurement deadline per rendered primary, in seconds (#454, #588)."""


def measurement_deadline_ms(primaries: int) -> int:
    """The per-story ``budgets.deadline_ms`` override a structured row's turns run under (#454):
    the matrix measures how long a structured turn takes, so the seeded ``Structured`` profile's
    own deadline must not cut it. It scales with the turn's primaries (#588, the owner's option
    (b)), ``TURN_DEADLINE_S`` per primary, up to the profile schema's largest ``deadline_ms``
    (``MAX_DEADLINE_MS``). The row records the override it used (``Row.deadline_override_ms``)."""
    if primaries < 1:
        raise ValueError("a structured turn renders at least one primary")
    return min(round(TURN_DEADLINE_S * primaries * 1000), MAX_DEADLINE_MS)


MEASUREMENT_DEADLINE_MS = measurement_deadline_ms(1)
"""A single-character structured row's override (one primary): ``TURN_DEADLINE_S``."""
TURN_PATH = "respond (#57)"
GUARDED_REPLY_PATH = "guarded reply (#53), until respond (#57)"
EX_UNAVAILABLE = 1
EMBEDDING_CALLS = 10
EMBEDDING_BATCHES = (64, 16, 1)
EMBEDDING_TEXT = "The lighthouse keeper keeps a lamp lit through the storm."

type PolicySet = Literal["off", "minors"]
POLICY_SETS: tuple[PolicySet, ...] = ("off", "minors")
POLICY_LABELS: Mapping[PolicySet, str] = {"off": "policies off", "minors": "minors policy on"}

type TurnKind = Literal[
    "lite",
    "structured",
    "repair",
    "three_character_sequential",
    "three_character_director",
    "three_character_default",
]
THREE_CHARACTER_KINDS: tuple[TurnKind, ...] = (
    "three_character_sequential",
    "three_character_director",
    "three_character_default",
)
TURN_KINDS: tuple[TurnKind, ...] = ("lite", "structured", "repair", *THREE_CHARACTER_KINDS)
TURN_KIND_LABELS: Mapping[TurnKind, str] = {
    "lite": "Lite",
    "structured": "structured",
    "repair": "repair-heavy",
    "three_character_sequential": "three-character sequential",
    "three_character_director": "three-character, director order",
    "three_character_default": "three-character, default budget",
}
STRUCTURED_PATH = "structured respond (#150)"
THREE_CHARACTER_PATH = "three-character sequential respond (#174)"
STUB_TIER = "stub"
"""The tier of the rows the stub adapter measures (P-08's own runs)."""
THREE_CHARACTER_CAST = ("Cal", "Bobby", "Jenni", "Marcus")
"""S-SEQUENTIAL's key room (Cal the player, Bobby and Jenni) and Marcus, the third primary."""
THREE_CHARACTER_PRIMARIES = 3
THREE_CHARACTER_DEADLINE_MS = measurement_deadline_ms(THREE_CHARACTER_PRIMARIES)
"""A three-character row's override (#588): ``TURN_DEADLINE_S`` per primary, capped at the
profile schema's largest ``deadline_ms``."""
THREE_CHARACTER_CALLS = 16
"""The story override of ``budgets.model_calls`` the three-character rows run under (the planner's
Discretion on #182; the seeded ``Structured`` v2's own cap, recorded as an override so the row
does not depend on the profile's default)."""
PLANNING = ("reduced_characters", "deferred_characters", "escalation_deferred")
type ThreeCharacterVariant = Literal["sequential", "director", "default"]
THREE_CHARACTER_VARIANTS: Mapping[TurnKind, ThreeCharacterVariant] = {
    "three_character_sequential": "sequential",
    "three_character_director": "director",
    "three_character_default": "default",
}

MINORS_PROMPT = (
    "Flag the text if it describes sexual content involving anyone under 18, or sexualises a "
    "minor in any way. Anything else, including ordinary scenes with children, is not a "
    "violation."
)

SCRIPT = (
    "Hello? Is anyone home?",
    "I brought bread from the market. Where should I put it?",
    "What happened to the old mill by the river?",
    "Can you show me the way to the library?",
    "That's a lovely garden. Who looks after it?",
    "I heard thunder earlier. Do you think it will rain?",
    "Let's sit by the fire for a while.",
    "Tell me about the festival next week.",
    "I found this map in the attic. Does it mean anything to you?",
    "Shall we walk down to the harbour?",
    "The boats look busy today. What are they bringing in?",
    "I'd like to learn how to fish. Could you teach me?",
    "Where did you grow up?",
    "Let's make some tea before we go.",
    "Do you know any stories about the lighthouse?",
    "I think I hear someone at the door.",
    "Let's write a letter to my aunt together.",
    "What should we cook for supper tonight?",
    "The stars are bright tonight. Do you know their names?",
    "It's getting late. Shall we plan tomorrow?",
    "Good morning! Did you sleep well?",
)
"""One warm-up input, then the measured turns (benign: the minors row must not trip)."""


# -- the measured turn ------------------------------------------------------------------------


class TurnCalls(CallLog):
    """One measured turn (#303): its model calls in the order they were made (every attempt,
    failed ones included), the calls it was designed to make by purpose, the cause of each
    safe fallback it released (``deadline``, ``retry_budget`` or ``call_budget``), and its
    budget's call cap (``max_calls``, #69; ``None`` when the turn had none or it is unknown,
    #308)."""

    def __init__(
        self,
        calls: Iterable[ModelCall],
        designed: Mapping[str, int],
        fallback_causes: Iterable[str] = (),
        max_calls: int | None = None,
        planning: Mapping[str, int] | None = None,
        steps: Iterable[str] = (),
    ) -> None:
        super().__init__()
        self.calls.extend(calls)
        self.designed = dict(designed)
        self.fallback_causes = list(fallback_causes)
        self.max_calls = max_calls
        self.planning = dict(planning or {})
        """A multi-character turn's budget-planning entries (``PLANNING``), by name (#182)."""
        self.steps = list(steps)
        """The turn's degradation steps (a three-character row records them, #182)."""

    def extra(self) -> dict[str, int]:
        """The calls made beyond the design, by purpose. A designed call that was never made
        (a check the deadline skipped) offsets nothing."""
        made = self.by_purpose()
        return {
            purpose: count - self.designed.get(purpose, 0)
            for purpose, count in made.items()
            if count > self.designed.get(purpose, 0)
        }

    def failed(self) -> list[ModelCall]:
        """The calls whose outcome was not ``ok``."""
        return [call for call in self.calls if call.outcome != "ok"]

    @property
    def faulted(self) -> bool:
        """Whether the turn hit a model-call deadline or error: a call failed, or the deadline
        released the safe fallback (it can pass before a check is sent, with no call to fail)."""
        return bool(self.failed()) or DEADLINE in self.fallback_causes


def designed_calls(guardrail_calls: int) -> dict[str, int]:
    """A Lite turn's design (#303): one ``generation`` call plus the ``guardrail_calls`` (G)
    that its budget reserves (#69). G is one call per model-assisted input policy for the input,
    plus one per model-assisted output policy for the first output check."""
    design = {GENERATION.name: 1}
    if guardrail_calls:
        design[GUARDRAIL_CHECK.name] = guardrail_calls
    return design


def three_character_design(
    guardrail_calls: int, *, renders: int = THREE_CHARACTER_PRIMARIES, director: bool = False
) -> dict[str, int]:
    """A three-character sequential turn's design (#182, #156's plan): one ``generation`` and one
    ``extraction_backstop`` per planned render, the composed message's detection (an
    ``extraction_backstop``), a ``salience_detection`` per render and the polish (a
    ``generation``), with the director's ordering call (a ``generation``) when the order is the
    director's, plus the ``guardrail_calls`` (G) the budget reserves."""
    design = {
        GENERATION.name: renders + 1 + (1 if director else 0),
        EXTRACTION_BACKSTOP.name: renders + 1,
        SALIENCE_DETECTION.name: renders,
    }
    if guardrail_calls:
        design[GUARDRAIL_CHECK.name] = guardrail_calls
    return design


class TurnDriver(Protocol):
    """Plays one Lite turn of a prepared story and returns its model calls and design."""

    name: str

    async def prepare(self, engine: Engine, policies: PolicySet) -> None: ...

    async def turn(self, said: str) -> TurnCalls: ...


def cast(policies: PolicySet) -> list[CharacterSpec]:
    """Two characters. The minors row casts a minor, so the policy has someone to protect."""
    young = policies == "minors"
    return [
        CharacterSpec(name="Wren", definition="A traveller, curious.", true_age=29, is_player=True),
        CharacterSpec(
            name="Ada" if young else "Maren",
            definition=(
                "A cheerful twelve-year-old who lives at the inn." if young else "The innkeeper."
            ),
            true_age=12 if young else 46,
        ),
    ]


def policy_set(policies: PolicySet) -> list[Policy]:
    if policies == "off":
        return []
    from epistrel.testing.ops import make_policy  # lazy: test-only module

    rule = {"type": "model_assisted", "prompt": MINORS_PROMPT, "on_uncertain": "violation"}
    return [make_policy(rule, name="minors")]


@dataclass
class RespondDriver:
    """A Lite turn through ``Engine.respond`` (#57). The minors row's policy is the seeded one,
    found by the turn's own guard context: nothing is passed in."""

    name: str = TURN_PATH
    engine: Engine | None = None
    story_id: str = ""
    actor_id: str = ""

    async def prepare(self, engine: Engine, policies: PolicySet) -> None:
        spec = StorySpec(name=f"model matrix ({policies})", characters=cast(policies))
        created = await create_story_from(engine.db, spec, principal=PRINCIPAL)
        self.engine, self.story_id = engine, created.story_id
        self.actor_id = next(c.id for c in created.characters if c.name == spec.characters[0].name)

    async def turn(self, said: str) -> TurnCalls:
        assert self.engine is not None  # noqa: S101 - prepare() ran
        request = RespondRequest(
            actor_character_id=self.actor_id, player_input=PlayerInputBody(say=said)
        )
        result = await self.engine.respond(
            RequestContext(principal=PRINCIPAL), self.story_id, request
        )
        assert result.budget is not None  # noqa: S101 - a played turn reports its budget (#69)
        return TurnCalls(
            result.model_calls,
            designed_calls(sum(result.budget.reserved.values())),
            [d.cause for d in result.degradation_details if d.step == SAFE_FALLBACK],
            result.budget.max_calls,
        )


def _stub_of(engine: Engine) -> StubAdapter | None:
    adapter = engine.models.adapter
    return adapter if isinstance(adapter, StubAdapter) else None


@dataclass
class StructuredDriver:
    """A single-character structured turn through ``Engine.respond`` (#150, P-08 M4): the
    story is on the seeded ``Structured`` profile with ``structured_load``'s planted canon
    (each character owns the brass lantern), and each turn renders the other character alone.

    ``repair`` makes it P-08's repair-heavy row: on the stub adapter half the renders deny
    owning the lantern (``structured_load.REPAIR_HEAVY_SHARE``), a contradiction of planted
    canon that the repair ladder takes to rung 3 (a ``regeneration`` call). A live model is
    not scripted: its repair-heavy row measures what the model's own claims set off. The
    stub's claim mix is restored after the row.

    The story overrides ``budgets.deadline_ms`` with ``deadline_override_ms`` (#454, the
    owner's option (a)): a measurement deadline, so a slow tier's turn is measured rather than
    cut by the profile's 45 s default. The row records the override."""

    repair: bool = False
    deadline_override_ms: int | None = MEASUREMENT_DEADLINE_MS
    name: str = STRUCTURED_PATH
    engine: Engine | None = None
    story_id: str = ""
    actor_id: str = ""
    render_id: str = ""
    _claims: list[dict[str, Any]] | None = None

    async def prepare(self, engine: Engine, policies: PolicySet) -> None:
        from epistrel.testing import structured_load  # lazy: test-only module

        spec = StorySpec(name=f"model matrix ({policies}, structured)", characters=cast(policies))
        created = await create_story_from(engine.db, spec, principal=PRINCIPAL)
        self.engine, self.story_id = engine, created.story_id
        ids = {c.name: c.id for c in created.characters}
        self.actor_id = ids[spec.characters[0].name]
        self.render_id = ids[spec.characters[1].name]
        await structured_load.use_structured(engine.db, self.story_id)
        if self.deadline_override_ms is not None:
            await engine.set_profile_overrides(
                RequestContext(principal=PRINCIPAL),
                self.story_id,
                ProfileOverridesPatch(
                    settings={"budgets": {"deadline_ms": self.deadline_override_ms}},
                    ignore_warnings=True,
                ),
            )
        await structured_load.plant(
            engine, RequestContext(principal=PRINCIPAL), self.story_id, ids.values()
        )
        stub = _stub_of(engine)
        if self.repair and stub is not None:
            self._claims = list(stub.render_claims)
            stub.render_claims = structured_load.render_claims(structured_load.REPAIR_HEAVY_SHARE)

    def restore(self) -> None:
        """Put back the stub's claim mix that ``prepare`` replaced."""
        stub = _stub_of(self.engine) if self.engine is not None else None
        if stub is not None and self._claims is not None:
            stub.render_claims = self._claims
            self._claims = None

    async def turn(self, said: str) -> TurnCalls:
        assert self.engine is not None  # noqa: S101 - prepare() ran
        request = RespondRequest(
            actor_character_id=self.actor_id,
            player_input=PlayerInputBody(say=said),
            render_character_ids=[self.render_id],
        )
        result = await self.engine.respond(
            RequestContext(principal=PRINCIPAL), self.story_id, request
        )
        assert result.budget is not None  # noqa: S101 - a played turn reports its budget (#69)
        return TurnCalls(
            result.model_calls,
            designed_calls(sum(result.budget.reserved.values())),
            [d.cause for d in result.degradation_details if d.step == SAFE_FALLBACK],
            result.budget.max_calls,
        )


@dataclass
class ThreeCharacterDriver:
    """A three-character sequential turn through ``Engine.respond`` (P-08 M5, #182): see the
    module docstring. ``variant`` is ``sequential`` (request order), ``director`` (the
    director's order) or ``default`` (the profile's own call cap, no override). The minors row
    casts Jenni as a
    twelve-year-old, so the seeded minors policy applies. ``profile`` and ``perception`` are the
    last turn's: the engine profile and version it ran under and the perception level its
    beats were filtered at."""

    variant: ThreeCharacterVariant = "sequential"
    deadline_override_ms: int | None = THREE_CHARACTER_DEADLINE_MS
    name: str = THREE_CHARACTER_PATH
    engine: Engine | None = None
    story_id: str = ""
    actor_id: str = ""
    primaries: list[str] = field(default_factory=list)
    profile: str | None = None
    perception: str | None = None

    async def prepare(self, engine: Engine, policies: PolicySet) -> None:
        from epistrel.core.mutation import FactRequest  # lazy: the tool's own imports
        from epistrel.core.propositions import store
        from epistrel.core.propositions.model import Interval, Proposition
        from epistrel.core.propositions.vocabulary import CORE_BY_NAME
        from epistrel.testing import structured_load  # lazy: test-only module

        young = policies == "minors"
        cast_ = [
            CharacterSpec(
                name="Cal", definition="A traveller, curious.", true_age=29, is_player=True
            ),
            CharacterSpec(name="Bobby", definition="The innkeeper's son, restless.", true_age=31),
            CharacterSpec(
                name="Jenni",
                definition=(
                    "A cheerful twelve-year-old who lives at the inn."
                    if young
                    else "The innkeeper, careful with her keys."
                ),
                true_age=12 if young else 44,
            ),
            CharacterSpec(name="Marcus", definition="A carter waiting out the rain.", true_age=52),
        ]
        spec = StorySpec(name=f"model matrix ({policies}, three characters)", characters=cast_)
        created = await create_story_from(engine.db, spec, principal=PRINCIPAL)
        self.engine, self.story_id = engine, created.story_id
        ids = {c.name: c.id for c in created.characters}
        self.actor_id = ids["Cal"]
        self.primaries = [ids[n] for n in THREE_CHARACTER_CAST[1:]]
        await structured_load.use_structured(engine.db, self.story_id)
        budgets: dict[str, Any] = (
            {} if self.variant == "default" else {"model_calls": THREE_CHARACTER_CALLS}
        )
        if self.deadline_override_ms is not None:
            budgets["deadline_ms"] = self.deadline_override_ms
        ctx = RequestContext(principal=PRINCIPAL)
        await engine.set_profile_overrides(
            ctx,
            self.story_id,
            ProfileOverridesPatch(settings={"budgets": budgets}, ignore_warnings=True),
        )
        async with engine.db.begin() as conn:
            study = await store.add_entity(conn, self.story_id, "place", "the study")
        for character in ids.values():
            await engine.commit_fact(
                ctx,
                self.story_id,
                FactRequest(
                    proposition=Proposition(
                        subject_id=character,
                        predicate_id=CORE_BY_NAME["located_at"].id,
                        object_id=study,
                        valid_time=Interval.of(None, None),
                    )
                ),
            )

    async def turn(self, said: str) -> TurnCalls:
        assert self.engine is not None  # noqa: S101 - prepare() ran
        request = RespondRequest(
            actor_character_id=self.actor_id,
            player_input=PlayerInputBody(say=said),
            render_character_ids=list(self.primaries),
            interaction_mode="sequential",
            sequence_order="director" if self.variant == "director" else None,
        )
        ctx = RequestContext(principal=PRINCIPAL)
        result = await self.engine.respond(ctx, self.story_id, request)
        assert result.budget is not None  # noqa: S101 - a played turn reports its budget (#69)
        if result.profile is not None:
            found = await self.engine.get_profile(ctx, result.profile.id)
            self.profile = f"{found.name} v{result.profile.version}"
        self.perception = result.audience_filter
        planning = {
            "reduced_characters": len(result.reduced_characters),
            "deferred_characters": len(result.deferred_characters),
            "escalation_deferred": len(result.escalation_deferred),
        }
        renders = THREE_CHARACTER_PRIMARIES - planning["reduced_characters"]
        renders -= planning["deferred_characters"]
        return TurnCalls(
            result.model_calls,
            three_character_design(
                sum(result.budget.reserved.values()),
                renders=renders,
                director=self.variant == "director",
            ),
            [d.cause for d in result.degradation_details if d.step == SAFE_FALLBACK],
            result.budget.max_calls,
            planning={k: v for k, v in planning.items() if v},
            steps=result.degradation,
        )


def driver_for(kind: TurnKind) -> TurnDriver:
    """The driver of a row's turn kind."""
    if kind == "lite":
        return RespondDriver()
    variant = THREE_CHARACTER_VARIANTS.get(kind)
    if variant is not None:
        return ThreeCharacterDriver(variant=variant)
    return StructuredDriver(repair=kind == "repair")


def guarded_checks(policies: Sequence[Policy]) -> int:
    """The guarded reply's ``guardrail_check`` calls: one per model-assisted policy for each
    text it applies to, the input and the reply."""
    return sum(
        target in policy.applies_to
        for policy in policies
        if isinstance(policy.rule, ModelAssistedRule)
        for target in (PolicyTarget.TEXT_INPUT, PolicyTarget.TEXT_OUTPUT)
    )


@dataclass
class GuardedReplyDriver:
    """A Lite turn through #53's guarded reply (input check, ``generation``, output check): the
    turn path of the rows measured before #57."""

    name: str = GUARDED_REPLY_PATH
    engine: Engine | None = None
    story_id: str = ""
    policies: list[Policy] = field(default_factory=list)

    async def prepare(self, engine: Engine, policies: PolicySet) -> None:
        spec = StorySpec(name=f"model matrix ({policies})", characters=cast(policies))
        created = await create_story_from(engine.db, spec, principal=PRINCIPAL)
        self.engine, self.story_id = engine, created.story_id
        self.policies = policy_set(policies)

    async def turn(self, said: str) -> TurnCalls:
        from epistrel.testing.ops import guarded_reply  # lazy: test-only module

        assert self.engine is not None  # noqa: S101 - prepare() ran
        with recording() as calls:
            await guarded_reply(
                self.engine,
                self.story_id,
                said,
                principal=PRINCIPAL,
                policies=self.policies,
                budget=TurnBudget(deadline_s=TURN_DEADLINE_S),
            )
        return TurnCalls(calls.calls, designed_calls(guarded_checks(self.policies)))


# -- statistics -------------------------------------------------------------------------------


def nearest_rank(values: Sequence[float], percentile: float) -> float:
    """The nearest-rank percentile: the smallest value with at least ``percentile`` % of the
    values at or below it."""
    if not values:
        raise ValueError("no values")
    if not 0 < percentile <= 100:
        raise ValueError("percentile must be in (0, 100]")
    ordered = sorted(values)
    rank = math.ceil(percentile / 100 * len(ordered))
    return ordered[rank - 1]


def calls_per_turn(logs: Iterable[CallLog]) -> dict[str, float]:
    """The mean number of model calls per turn, by purpose (every attempt counted)."""
    logs = list(logs)
    totals: dict[str, int] = {}
    for log in logs:
        for purpose, count in log.by_purpose().items():
            totals[purpose] = totals.get(purpose, 0) + count
    return {purpose: round(total / len(logs), 2) for purpose, total in sorted(totals.items())}


@dataclass(frozen=True)
class CallBreakdown:
    """The measured turns' calls set against their design (#303); the fields are ``Row``'s."""

    designed_calls_per_turn: dict[str, float]
    extra_calls: dict[str, int]
    failed_calls: dict[str, dict[str, int]]
    faulted_turns: list[int]
    fallbacks: dict[str, int]


def _tally(counts: dict[str, int], key: str, n: int = 1) -> None:
    counts[key] = counts.get(key, 0) + n


def call_breakdown(turns: Sequence[TurnCalls]) -> CallBreakdown:
    """What the mean calls per turn folds together, taken apart (#303). It gives the designed
    calls per turn, by purpose (a mean over the turns: a whole number while every turn has the
    same plan), the calls beyond the design, by purpose, summed over the turns, the failed calls
    by purpose and outcome, the turns (numbered from 1) that hit a model-call deadline or error,
    and the safe fallbacks by cause."""
    designed: dict[str, int] = {}
    extra: dict[str, int] = {}
    failed: dict[str, dict[str, int]] = {}
    fallbacks: dict[str, int] = {}
    faulted: list[int] = []
    for number, turn in enumerate(turns, start=1):
        for purpose, count in turn.designed.items():
            _tally(designed, purpose, count)
        for purpose, count in turn.extra().items():
            _tally(extra, purpose, count)
        for call in turn.failed():
            _tally(failed.setdefault(call.purpose, {}), call.outcome)
        for cause in turn.fallback_causes:
            _tally(fallbacks, cause)
        if turn.faulted:
            faulted.append(number)
    return CallBreakdown(
        designed_calls_per_turn={
            purpose: round(total / len(turns), 2) for purpose, total in sorted(designed.items())
        },
        extra_calls=dict(sorted(extra.items())),
        failed_calls={purpose: dict(sorted(by.items())) for purpose, by in sorted(failed.items())},
        faulted_turns=faulted,
        fallbacks=dict(sorted(fallbacks.items())),
    )


def design_problems(
    turns: Sequence[TurnCalls], design: Mapping[str, int], *, output_checked: bool | None = None
) -> list[str]:
    """What is wrong with measured turns against the ``design`` a row expects (P-08, #308);
    empty when nothing is. Extra calls (a flagged draft's regeneration and re-check, a retry,
    a call the deadline cut) are not wrong by themselves; ``call_breakdown`` reports them.
    A turn (numbered from 1) is wrong when:

    - its design is not ``design`` (the turn was planned with other calls);
    - it made fewer calls of a designed purpose than designed and hit no model-call deadline
      or error (a call was skipped);
    - it made a call of a purpose outside ``design``;
    - it made more calls than its budget's ``max_calls``: the bound on extra calls
      (NFR-PERF-6), checked when the turn recorded its cap;
    - with ``output_checked`` (by default, when ``design`` has a ``guardrail_check``: the
      matrix's minors policy checks the input and the output), it hit no deadline or error and
      its last ``generation`` call was not followed by a ``guardrail_check``: a draft released
      unchecked."""
    if output_checked is None:
        output_checked = GUARDRAIL_CHECK.name in design
    expected = dict(design)
    problems: list[str] = []
    for number, turn in enumerate(turns, start=1):
        made = turn.by_purpose()
        if turn.designed != expected:
            problems.append(f"turn {number}: designed {turn.designed}, expected {expected}")
        for purpose, count in sorted(expected.items()):
            if made.get(purpose, 0) < count and not turn.faulted:
                problems.append(
                    f"turn {number}: {made.get(purpose, 0)} of {count} designed {purpose} "
                    "calls, with no deadline or error"
                )
        for purpose in sorted(set(made) - set(expected)):
            problems.append(f"turn {number}: {made[purpose]} {purpose} calls outside the design")
        if turn.max_calls is not None and len(turn.calls) > turn.max_calls:
            problems.append(
                f"turn {number}: {len(turn.calls)} calls, over its budget of {turn.max_calls}"
            )
        if output_checked and not turn.faulted:
            purposes = [call.purpose for call in turn.calls]
            generated = [i for i, p in enumerate(purposes) if p == GENERATION.name]
            checked = [i for i, p in enumerate(purposes) if p == GUARDRAIL_CHECK.name]
            if generated and not (checked and checked[-1] > generated[-1]):
                problems.append(f"turn {number}: its last draft was released unchecked")
    return problems


BREAKDOWN = tuple(f.name for f in fields(CallBreakdown))
"""The ``Row`` fields of the call breakdown: ``None`` in a row measured before #303."""


# -- rows -------------------------------------------------------------------------------------


@dataclass
class Row:
    tier: str
    model: str
    policies: PolicySet
    measured_at: str
    sha: str
    hardware: str
    server_version: str
    turn_path: str
    protocol: bool
    protocol_reason: str | None
    capability: dict[str, dict[str, Any]]
    e06: dict[str, Any]
    turns: int
    p50_ms: int | None
    p95_ms: int | None
    calls_per_turn: dict[str, float]
    latencies_ms: list[int]
    designed_calls_per_turn: dict[str, float] | None = None
    """The call breakdown (#303, ``call_breakdown``) starts here. Every field is ``None`` (not
    recorded) in a row measured before it. This one holds the calls each turn was designed to
    make, by purpose."""
    extra_calls: dict[str, int] | None = None
    """The calls beyond the design, by purpose, summed over the measured turns."""
    failed_calls: dict[str, dict[str, int]] | None = None
    """The calls whose outcome was not ``ok``, by purpose and outcome."""
    faulted_turns: list[int] | None = None
    """The measured turns that hit a model-call deadline or error, numbered from 1 in the order
    of ``latencies_ms``."""
    fallbacks: dict[str, int] | None = None
    """The turns whose reply is the safe fallback, by cause."""
    turn_kind: TurnKind = "lite"
    """What the row's turns are (P-08 M4, #153): ``lite``, ``structured`` (single-character
    structured turns) or ``repair`` (structured turns whose renders contradict planted canon
    half the time). A row measured before turn kinds is ``lite``."""
    deadline_override_ms: int | None = None
    """The per-story ``budgets.deadline_ms`` override the row's turns ran under (#454), or
    ``None`` when they ran under the profile's own deadline. A structured row sets one
    (``MEASUREMENT_DEADLINE_MS``), so a reader knows the profile default was not used."""
    profile: str | None = None
    """The engine profile and version the row's turns ran under (``<name> v<version>``, from the
    respond results; a three-character row records it, #182)."""
    perception: str | None = None
    """The perception level the row's beats were filtered at (a three-character row, #182)."""
    planning: dict[str, int] | None = None
    """A three-character row's budget-planning entries over its measured turns, by name: the
    turns that reduced or deferred a primary, or deferred an escalation (#182)."""
    degradation: dict[str, int] | None = None
    """A three-character row's degradation steps over its measured turns, by name (#182)."""

    @property
    def key(self) -> str:
        return row_key(self.tier, self.model, self.policies, self.turn_kind)


def row_key(tier: str, model: str, policies: str, turn_kind: str = "lite") -> str:
    """A row's key. A Lite row keeps the key it had before turn kinds."""
    key = f"{tier}|{model}|{policies}"
    return key if turn_kind == "lite" else f"{key}|{turn_kind}"


@dataclass(frozen=True)
class RowFailure:
    """A row whose turns stopped on a model failure partway (#454): the run writes the rows
    that finished and marks this one failed, rather than dropping everything. The row's
    previous measurement, if any, is kept: a failure replaces no timings."""

    tier: str
    model: str
    policies: PolicySet
    turn_kind: TurnKind
    measured_at: str
    sha: str
    turn_path: str
    turn: int
    """The turn that failed: 0 is the warm-up turn, then the measured turns from 1."""
    code: str
    purpose: str | None
    reason: str | None
    deadline_override_ms: int | None = None
    structural_errors: list[str] | None = None
    """The failure's ``details.structural_errors`` (what was wrong, as codes and counts, never
    the answer's text), or ``None`` when the error carries none."""

    @property
    def key(self) -> str:
        return row_key(self.tier, self.model, self.policies, self.turn_kind)


TURN_FAILURES = (ModelError, RevisionConflict)
"""What stops a row partway (#454): a model failure, or a turn whose read set changed and whose
budget could not pay to generate again (409 ``revision_conflict``)."""


class TurnFailed(Exception):
    """``measure_turns``: turn ``turn`` (0 = the warm-up) failed with ``error``."""

    def __init__(self, turn: int, error: EpistrelError) -> None:
        super().__init__(f"turn {turn}: {error.code}")
        self.turn = turn
        self.error = error


class MatrixInterrupted(Exception):
    """``run_matrix``: a row's turns failed partway. ``rows`` are the rows that finished
    before it, ``failure`` the row that did not."""

    def __init__(self, rows: list[Row], failure: RowFailure) -> None:
        super().__init__(f"{failure.key}: turn {failure.turn} failed with {failure.code}")
        self.rows = rows
        self.failure = failure


def merge_failures(
    existing: Iterable[Mapping[str, Any]], rows: Iterable[Row], failure: RowFailure | None
) -> list[dict[str, Any]]:
    """``existing`` failure marks without those that ``rows`` (measured since) or ``failure``
    replace, plus ``failure``; sorted by key."""
    replaced = {row.key for row in rows}
    kept = {
        row_key(f["tier"], f["model"], f["policies"], f.get("turn_kind", "lite")): dict(f)
        for f in existing
    }
    kept = {key: f for key, f in kept.items() if key not in replaced}
    if failure is not None:
        kept[failure.key] = asdict(failure)
    return [kept[key] for key in sorted(kept)]


def capability_cells(report: CapabilityReport) -> dict[str, dict[str, Any]]:
    cells: dict[str, dict[str, Any]] = {}
    for name, result in report.purposes.items():
        cells[name] = {
            "capable": result.capable,
            "checks": [
                {k: v for k, v in asdict(check).items() if v is not None} for check in result.checks
            ],
        }
    return cells


def e06_cell(result: PromptParamsResult) -> dict[str, Any]:
    return {
        "passed": result.passed,
        "lengths": {str(cap): out for cap, out in result.lengths.items()},
        "identical": result.identical,
        "samples": result.samples,
    }


def merge(existing: Iterable[Mapping[str, Any]], rows: Iterable[Row]) -> list[dict[str, Any]]:
    """``existing`` rows with every row of the same key replaced, sorted by key."""
    merged = {
        row_key(r["tier"], r["model"], r["policies"], r.get("turn_kind", "lite")): dict(r)
        for r in existing
    }
    for row in rows:
        merged[row.key] = asdict(row)
    return [merged[key] for key in sorted(merged)]


def load_rows(path: Path) -> list[dict[str, Any]]:
    """The rows of ``path``. A row measured before #303 gets every breakdown field it lacks as
    ``None``, so it is written back explicitly as not recorded (``null``)."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = data.get("rows", [])
    return [{**dict.fromkeys(BREAKDOWN), "turn_kind": "lite", **row} for row in rows]


def _mark(value: bool | None) -> str:
    return "—" if value is None else ("✓" if value else "✗")


def _capability_text(cell: Mapping[str, Any]) -> str:
    details = "; ".join(
        f"{c['name']} {_mark(c.get('passed'))}"
        for c in cell["checks"]
        if c.get("passed") is not None
    )
    return f"{_mark(cell['capable'])} ({details})" if details else _mark(cell["capable"])


def _e06_text(cell: Mapping[str, Any]) -> str:
    # By cap, numerically: the json's keys are sorted as strings ("128" before "32").
    lengths = ", ".join(
        f"{out}/{cap}" for cap, out in sorted(cell["lengths"].items(), key=lambda kv: int(kv[0]))
    )
    return (
        f"{_mark(cell['passed'])} (tokens {lengths}; temp 0: "
        f"{cell['identical']}/{cell['samples']} identical)"
    )


def _calls_text(calls: Mapping[str, float]) -> str:
    total = round(sum(calls.values()), 2)
    parts = ", ".join(f"{purpose} {count:g}" for purpose, count in calls.items())
    return f"{total:g} ({parts})" if parts else "0"


def _seconds(ms: float | None) -> str:
    return "—" if ms is None else f"{ms / 1000:.2f} s"


def _failed_text(failed: Mapping[str, Mapping[str, int]]) -> str:
    parts = [f"{p} {outcome} {n}" for p, by in failed.items() for outcome, n in by.items()]
    total = sum(n for by in failed.values() for n in by.values())
    return f"{total} ({', '.join(parts)})" if parts else "0"


def _faulted_text(faulted: Sequence[int], turns: int) -> str:
    if not faulted:
        return f"0 of {turns}"
    label = "turn" if len(faulted) == 1 else "turns"
    return f"{len(faulted)} of {turns} ({label} {', '.join(str(n) for n in faulted)})"


def _without_text(latencies: Sequence[int], faulted: Sequence[int]) -> str:
    """The nearest-rank p50 / p95 of the turns not in ``faulted``."""
    skipped = set(faulted)
    others = [ms for number, ms in enumerate(latencies, start=1) if number not in skipped]
    if not others:
        return "—"
    return f"{_seconds(nearest_rank(others, 50))} / {_seconds(nearest_rank(others, 95))}"


NOT_RECORDED = "not recorded"


def _label(row: Mapping[str, Any]) -> str:
    """A row's first cell: its tier and model, and its turn kind unless Lite (#153)."""
    label = f"{row['tier']}: {row['model']}"
    kind = row.get("turn_kind", "lite")
    return label if kind == "lite" else f"{label} ({TURN_KIND_LABELS.get(kind, kind)} turns)"


def _breakdown_cells(row: Mapping[str, Any]) -> list[str]:
    """``row``'s **Calls** columns (#303); *not recorded* for each field it lacks."""
    designed = row.get("designed_calls_per_turn")
    extra = row.get("extra_calls")
    failed = row.get("failed_calls")
    faulted = row.get("faulted_turns")
    fallbacks = row.get("fallbacks")
    return [
        NOT_RECORDED if designed is None else _calls_text(designed),
        NOT_RECORDED if extra is None else _calls_text(extra),
        NOT_RECORDED if failed is None else _failed_text(failed),
        NOT_RECORDED if faulted is None else _faulted_text(faulted, row["turns"]),
        NOT_RECORDED if faulted is None else _without_text(row["latencies_ms"], faulted),
        NOT_RECORDED if fallbacks is None else _calls_text(fallbacks),
    ]


INTRO = """\
# Model matrix

**Reported, not gated.** Each row is one model tier measured by
`uv run python -m epistrel.tools.model_matrix` on the owner's hardware, and committed after a
local run. CI does not run it.

**Protocol** means the endpoint speaks the OpenAI-compatible format: `GET /models` and one
completion work. It is not a quality claim. **Capability** is reported separately for each
purpose against that purpose's floor in [models.md](models.md) (NFR-PORT-3). The context check
is an ~8k-token prompt that completes. The structured-output check needs ≥ 95 % valid results
over 20 calls (`json_schema`, or the schema in the prompt when the endpoint refuses it).
**E-06** checks the prompt parameters: `tokens_out`/`max_tokens` per length, and how many of 10
samples at temperature 0 are identical (≥ 8 passes). **p50 / p95** are nearest-rank end-to-end
turn latencies over the measured Lite turns, after one warm-up turn (NFR-PERF-8). **Calls per
turn** is the mean number of model calls by purpose, counting every attempt (failed ones too).

**Calls** breaks those calls down (#303):

- **Designed per turn**: what each turn planned before its first call. That is one `generation`
  plus one `guardrail_check` per model-assisted policy on the input and on the output, the
  guardrail calls its budget reserved (#69).
- **Extra calls**: the calls the measured turns made beyond that design, summed over the turns.
  They are structured-output retries, the empty-reply retry, regenerations after an output
  policy flagged the draft (with their re-checks), and regenerations after the story changed
  under the turn.
- **Failed calls**: the calls whose outcome was not `ok`, by purpose and outcome. The outcome is
  `timeout` (the call's timeout or the turn's deadline), `http_error`, `invalid_output` or
  `cancelled`.
- **Turns with a deadline or error**: the turns that had a failed call, or whose deadline
  released the safe fallback. They are numbered from 1 in the order they were measured, which is
  the order of the raw timings.
- **p50 / p95 without them**: the percentiles of the other turns, so the latency those turns
  added stands out against the rest.
- **Safe fallbacks**: the turns whose reply is the safe fallback line, by cause: `deadline`,
  `retry_budget` (every regeneration the story allows was flagged too) or `call_budget`.

A call the deadline cuts is never retried. If it is an output check or a regeneration, the turn
releases the safe fallback. If it is the input check or the first generation, the turn fails:
the run writes no row for it, keeps the rows that finished, and lists the row under **Failed
runs** (#454).
"""

NOT_RECORDED_NOTE = """\
**Not recorded.** Rows measured before the breakdown existed show *not recorded* in the
**Calls** columns (`null` in the JSON). Those runs kept only the mean calls per turn, and
nothing is derived from it. The next live run of the tier fills them.
"""

TURN_PATH_NOTE = """\
**Turn path.** Rows whose turn path is the guarded reply (#53) were measured before `respond`
existed; a live re-run through `respond` (#57) is pending and will replace them.
"""


TURN_KIND_NOTE = """\
**Turn kinds (P-08 M4).** A row marked *structured turns* plays single-character structured
turns (#150) on the `Structured (M4)` profile: render, detection, verification, composition and
commit. One marked *repair-heavy turns* plays the same turns on a story with planted canon
(each character owns a brass lantern), and on the stub half its renders deny it, so the repair
ladder reaches rung 3 (a `regeneration` call). For these rows the **Designed per turn** column
is the first render and the reserved guardrail calls, so **Extra calls** holds the stages a
structured turn adds (salience, entailment, composition, regeneration) as well as retries.
"""

THREE_CHARACTER_NOTE = """\
**Three-character turns (P-08 M5, #182).** A row marked *three-character sequential turns* plays
S-SEQUENTIAL's key room with a third primary: the player speaks and three co-located characters
render one after another (per-character renders) on the seeded `Structured` profile, under a
story override of `budgets.model_calls` = 16. Its **Designed per turn** is #156's plan for three
renders: a render and an extraction per primary, the composed message's detection, a salience
pass per render and the polish (11 calls), plus the reserved guardrail calls. *Director order*
adds the director's ordering call; *default budget* runs under the profile's own call cap (no
override). The turn path names the profile and version and the perception level the turns ran
under, how often budget planning reduced or deferred a primary, and the degradation steps the
turns took (an optional call the budget could not fit is skipped, never a failed turn).
"""

THREE_CHARACTER_PENDING = """\
**Pending.** The three-character rows above are the stub's; a live tier's three-character rows
are pending until its next run (`--turn-kinds three_character_sequential`).
"""

DEADLINE_OVERRIDE_NOTE = f"""\
**Turn deadline override (#454).** A row whose turn path says *turn deadline … s (override)*
ran its turns under that per-story `budgets.deadline_ms` override, not its profile's own
deadline (the `Structured (M4)` profile's is 45 s). The matrix measures how long a turn takes,
so the profile's deadline must not cut it. The override is {TURN_DEADLINE_S:g} s per primary
the turn renders, up to the profile's largest deadline of {MAX_DEADLINE_MS / 1000:g} s (#588), so
a three-character row measured since #588 runs under {THREE_CHARACTER_DEADLINE_MS / 1000:g} s.
Whether the profile's deadline fits a tier is read from the p50 / p95 against it.
"""

FAILED_NOTE = """\
A row whose turns stopped on a model failure partway. The run wrote the rows that finished
before it, and kept this row's previous measurement (if any) in the tables above. The turn is
numbered from 1 in the measured turns; turn 0 is the warm-up. The next run that measures the
row clears its entry.
"""

EMBEDDING_NOTE = """\
The embedding endpoint (the `embedding` port, #138), measured when the run names one:
**Protocol** is `GET /models` and one embedding; **Dimension** is what every vector of that
model has (the deployment pins it at the first embedding); **Max batch** is the largest of 64,
16 and 1 texts one request embeds; **p50** is over single-text calls.
"""


def _turn_path_text(row: Mapping[str, Any]) -> str:
    """A row's turn path, with its turn deadline override if it ran under one (#454), and the
    profile, perception level and budget-planning entries a three-character row records."""
    override = row.get("deadline_override_ms")
    path = str(row["turn_path"])
    if override is not None:
        path += f"; turn deadline {override / 1000:g} s (override)"
    if row.get("profile"):
        path += f"; profile {row['profile']}, perception {row.get('perception') or '—'}"
    planning = row.get("planning")
    if planning is not None:
        path += "; " + (
            ", ".join(f"{name} {n}" for name, n in sorted(planning.items()))
            or "no primary reduced or deferred"
        )
    steps = row.get("degradation")
    if steps:
        path += "; degradation " + ", ".join(f"{name} {n}" for name, n in sorted(steps.items()))
    return path


def _failed_lines(failed_rows: Sequence[Mapping[str, Any]]) -> list[str]:
    if not failed_rows:
        return []
    lines = ["## Failed runs", "", FAILED_NOTE]
    lines += ["| Tier | Policies | Measured | Engine SHA | Turn | Error | Turn path |"]
    lines += ["|---|---|---|---|---|---|---|"]
    for f in failed_rows:
        error = f["code"] + "".join(
            f", {name} `{f[name]}`" for name in ("purpose", "reason") if f.get(name)
        )
        if f.get("structural_errors"):
            error += "; " + ", ".join(f"`{e}`" for e in f["structural_errors"])
        lines.append(
            f"| {_label(f)} | {POLICY_LABELS.get(f['policies'], f['policies'])} | "
            f"{f['measured_at']} | `{f['sha']}` | {f['turn']} | {error} | {_turn_path_text(f)} |"
        )
    return [*lines, ""]


def render(
    rows: Sequence[Mapping[str, Any]],
    embedding_rows: Sequence[Mapping[str, Any]] = (),
    failed_rows: Sequence[Mapping[str, Any]] = (),
) -> str:
    """``docs/model-matrix.md`` for ``rows``. The notes appear only while they apply: *not
    recorded* while a row lacks the call breakdown, the turn-path note while a row was
    measured on the guarded reply (#303), and the override note while a row ran under a turn
    deadline override (#454). ``failed_rows`` are the rows a run could not finish (#454)."""
    purposes = list(STORY_PURPOSES)  # a turn's purposes: the matrix measures turns
    header = ["Tier", "Policies", "Protocol", *[f"`{p}`" for p in purposes]]
    header += ["E-06", "Turns", "p50", "p95", "Calls per turn"]
    calls_header = ["Tier", "Policies", "Designed per turn", "Extra calls", "Failed calls"]
    calls_header += ["Turns with a deadline or error", "p50 / p95 without them", "Safe fallbacks"]
    lines = [INTRO]
    if any(row.get(name) is None for row in rows for name in BREAKDOWN):
        lines.append(NOT_RECORDED_NOTE)
    if any(row["turn_path"] == GUARDED_REPLY_PATH for row in rows):
        lines.append(TURN_PATH_NOTE)
    if any(row.get("turn_kind", "lite") != "lite" for row in rows):
        lines.append(TURN_KIND_NOTE)
    three = [r for r in rows if r.get("turn_kind", "lite") in THREE_CHARACTER_KINDS]
    if three:
        lines.append(THREE_CHARACTER_NOTE)
        if all(r["tier"] == STUB_TIER for r in three):
            lines.append(THREE_CHARACTER_PENDING)
    if any(row.get("deadline_override_ms") is not None for row in [*rows, *failed_rows]):
        lines.append(DEADLINE_OVERRIDE_NOTE)
    lines += ["## Results", ""]
    if not rows:
        lines += ["No tier has been measured yet.", ""]
    else:
        lines += ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
        for row in rows:
            protocol = _mark(row["protocol"])
            if row.get("protocol_reason"):
                protocol += f" ({row['protocol_reason']})"
            cells = [
                _label(row),
                POLICY_LABELS.get(row["policies"], row["policies"]),
                protocol,
            ]
            for purpose in purposes:
                cell = row["capability"].get(purpose)
                cells.append(_capability_text(cell) if cell else "not run")
            cells += [
                _e06_text(row["e06"]) if row["e06"] else "not run",
                str(row["turns"]),
                _seconds(row["p50_ms"]),
                _seconds(row["p95_ms"]),
                _calls_text(row["calls_per_turn"]),
            ]
            lines.append("| " + " | ".join(cells) + " |")
        lines += ["", "## Calls", ""]
        lines += ["| " + " | ".join(calls_header) + " |", "|" + "---|" * len(calls_header)]
        for row in rows:
            label = _label(row)
            policies = POLICY_LABELS.get(row["policies"], row["policies"])
            lines.append("| " + " | ".join([label, policies, *_breakdown_cells(row)]) + " |")
        lines += ["", "## Runs", ""]
        lines += ["| Tier | Policies | Measured | Engine SHA | Hardware | Server | Turn path |"]
        lines += ["|---|---|---|---|---|---|---|"]
        for row in rows:
            lines.append(
                f"| {_label(row)} | "
                f"{POLICY_LABELS.get(row['policies'], row['policies'])} | {row['measured_at']} | "
                f"`{row['sha']}` | {row['hardware'] or '—'} | {row['server_version'] or '—'} | "
                f"{_turn_path_text(row)} |"
            )
        lines.append("")
    lines += _failed_lines(failed_rows)
    lines += _embedding_lines(embedding_rows)
    lines.append("Raw timings for every row are in [model-matrix.json](model-matrix.json).")
    return "\n".join(lines) + "\n"


def write_matrix(
    rows: Sequence[Mapping[str, Any]],
    markdown: Path,
    raw: Path,
    embedding_rows: Sequence[Mapping[str, Any]] = (),
    failed_rows: Sequence[Mapping[str, Any]] = (),
) -> None:
    document: dict[str, Any] = {"rows": list(rows), "embedding_rows": list(embedding_rows)}
    if failed_rows:
        document["failed_rows"] = list(failed_rows)
    raw.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", "utf-8")
    markdown.write_text(render(rows, embedding_rows, failed_rows), encoding="utf-8")


# -- the embedding row (#138) -------------------------------------------------------------------


@dataclass(frozen=True)
class EmbeddingRow:
    """One embedding endpoint as measured: protocol, what it reports, its batch limit and
    single-call latency."""

    tier: str
    model: str
    measured_at: str
    sha: str
    protocol: bool
    protocol_reason: str | None
    reported_model: str | None
    dimension: int | None
    max_batch: int | None
    p50_ms: int | None
    latencies_ms: list[int]

    @property
    def key(self) -> str:
        return f"{self.tier}|{self.model}"


async def embedding_row(
    service: EmbeddingService,
    *,
    tier: str,
    model: str,
    calls: int = EMBEDDING_CALLS,
    now: datetime | None = None,
) -> EmbeddingRow:
    """Measure ``service``'s configured endpoint (see the module docstring)."""
    measured_at = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
    sha = engine_sha()
    try:
        probe = await service.probe()
    except ModelError as exc:
        reason = str(exc.details.get("reason", exc.code))
        return EmbeddingRow(
            tier, model, measured_at, sha, False, reason, None, None, None, None, []
        )
    max_batch: int | None = None
    for size in EMBEDDING_BATCHES:
        try:
            await service.embed([f"{EMBEDDING_TEXT} ({i})" for i in range(size)])
        except ModelError:
            continue
        max_batch = size
        break
    latencies: list[int] = []
    for _ in range(calls):
        started = time.perf_counter()
        await service.embed([EMBEDDING_TEXT])
        latencies.append(round((time.perf_counter() - started) * 1000))
    return EmbeddingRow(
        tier=tier,
        model=model,
        measured_at=measured_at,
        sha=sha,
        protocol=probe.protocol_ok,
        protocol_reason=None,
        reported_model=probe.model,
        dimension=probe.dimension,
        max_batch=max_batch,
        p50_ms=round(nearest_rank(latencies, 50)) if latencies else None,
        latencies_ms=latencies,
    )


def merge_embedding(
    existing: Iterable[Mapping[str, Any]], rows: Iterable[EmbeddingRow]
) -> list[dict[str, Any]]:
    merged = {f"{r['tier']}|{r['model']}": dict(r) for r in existing}
    for row in rows:
        merged[row.key] = asdict(row)
    return [merged[key] for key in sorted(merged)]


def load_failed_rows(path: Path) -> list[dict[str, Any]]:
    """The failure marks of ``path`` (``failed_rows``, #454); none in a file written before."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    found: list[dict[str, Any]] = data.get("failed_rows", [])
    return found


def load_embedding_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    found: list[dict[str, Any]] = data.get("embedding_rows", [])
    return found


def _embedding_lines(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = ["## Embedding", "", EMBEDDING_NOTE]
    if not rows:
        return [*lines, "No embedding endpoint has been measured yet.", ""]
    lines += [
        "| Tier | Protocol | Reported model | Dimension | Max batch | p50 | Measured "
        "| Engine SHA |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        protocol = _mark(row["protocol"])
        if row.get("protocol_reason"):
            protocol += f" ({row['protocol_reason']})"
        lines.append(
            f"| {row['tier']}: {row['model']} | {protocol} | {row['reported_model'] or '—'} | "
            f"{row['dimension'] if row['dimension'] is not None else '—'} | "
            f"{row['max_batch'] if row['max_batch'] is not None else '—'} | "
            f"{_seconds(row['p50_ms'])} | {row['measured_at']} | `{row['sha']}` |"
        )
    return [*lines, ""]


# -- the run ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    base_url: str
    model: str
    api_key: str
    tier: str = "local"
    hardware: str = ""
    server_version: str = ""
    purpose_models: Mapping[str, str] = field(default_factory=dict)
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    embedding_api_key: str | None = None


def engine_sha() -> str:
    """The engine's short commit SHA, ``-dirty`` when the working tree has changes."""
    git = shutil.which("git")
    if git is None:
        return "unknown"
    try:
        head = subprocess.run(  # noqa: S603 - fixed argv, no user input
            [git, "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status = subprocess.run(  # noqa: S603 - fixed argv, no user input
            [git, "status", "--porcelain"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except OSError, subprocess.CalledProcessError:
        return "unknown"
    if not head:
        return "unknown"
    return f"{head}-dirty" if status else head


async def configure(engine: Engine, target: Target) -> None:
    """Point ``default`` (and any ``--purpose-model`` override) at the target endpoint."""
    keybox = engine.models.keybox
    await model_config.write_config(
        engine.db,
        "default",
        keybox=keybox,
        principal=PRINCIPAL,
        base_url=target.base_url,
        model=target.model,
        api_key=target.api_key,
    )
    for purpose, model in target.purpose_models.items():
        await model_config.write_config(
            engine.db, purpose, keybox=keybox, principal=PRINCIPAL, model=model
        )
    if target.embedding_base_url and target.embedding_model:
        await model_config.write_config(
            engine.db,
            "embedding",
            keybox=keybox,
            principal=PRINCIPAL,
            base_url=target.embedding_base_url,
            model=target.embedding_model,
            api_key=target.embedding_api_key or target.api_key,
        )


async def measure_turns(
    engine: Engine, driver: TurnDriver, policies: PolicySet, turns: int
) -> tuple[list[int], list[TurnCalls]]:
    """One warm-up turn, then ``turns`` timed turns: latencies (ms) and each turn's calls."""
    await driver.prepare(engine, policies)
    script = [SCRIPT[i % len(SCRIPT)] for i in range(turns + 1)]
    try:
        await driver.turn(script[0])
    except TURN_FAILURES as exc:
        raise TurnFailed(0, exc) from exc
    latencies: list[int] = []
    logs: list[TurnCalls] = []
    for n, said in enumerate(script[1:], start=1):
        started = time.perf_counter()
        try:
            logs.append(await driver.turn(said))
        except TURN_FAILURES as exc:
            raise TurnFailed(n, exc) from exc
        latencies.append(round((time.perf_counter() - started) * 1000))
    return latencies, logs


def _detail(error: EpistrelError, name: str) -> str | None:
    value = error.details.get(name)
    return None if value is None else str(value)


def _errors(error: EpistrelError) -> list[str] | None:
    value = error.details.get("structural_errors")
    return None if not value else [str(v) for v in value]


async def run_matrix(
    engine: Engine,
    target: Target,
    *,
    turns: int = DEFAULT_TURNS,
    driver_factory: Callable[[], TurnDriver] = RespondDriver,
    turn_kinds: Sequence[TurnKind] = ("lite",),
    now: datetime | None = None,
) -> list[Row] | None:
    """The rows for ``target``, one per turn kind and policy set, or ``None`` when the endpoint
    fails the protocol probe. ``driver_factory`` plays the Lite rows; the structured and
    repair-heavy rows play ``StructuredDriver`` (#153). A row whose turn fails on a model
    error raises ``MatrixInterrupted`` with the rows finished before it (#454)."""
    await configure(engine, target)
    report = await assess(engine.models)
    if not report.protocol.ok:
        print(f"protocol check failed: {report.protocol.reason}", file=sys.stderr)
        return None
    e06 = await check_prompt_params(engine.models)
    capability = capability_cells(report)
    measured_at = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
    sha = engine_sha()
    rows: list[Row] = []
    for kind, policies in [(k, p) for k in turn_kinds for p in POLICY_SETS]:
        driver = driver_factory() if kind == "lite" else driver_for(kind)
        override = (
            driver.deadline_override_ms
            if isinstance(driver, StructuredDriver | ThreeCharacterDriver)
            else None
        )
        try:
            latencies, logs = await measure_turns(engine, driver, policies, turns)
        except TurnFailed as failed:
            failure = RowFailure(
                tier=target.tier,
                model=target.model,
                policies=policies,
                turn_kind=kind,
                measured_at=measured_at,
                sha=sha,
                turn_path=driver.name,
                turn=failed.turn,
                code=failed.error.code,
                purpose=_detail(failed.error, "purpose"),
                reason=_detail(failed.error, "reason"),
                deadline_override_ms=override,
                structural_errors=_errors(failed.error),
            )
            raise MatrixInterrupted(rows, failure) from failed.error
        finally:
            if isinstance(driver, StructuredDriver):
                driver.restore()
        breakdown = call_breakdown(logs)
        three = driver if isinstance(driver, ThreeCharacterDriver) else None
        planning: dict[str, int] | None = None
        steps: dict[str, int] | None = None
        if three is not None:
            planning, steps = {}, {}
            for log in logs:
                for name, n in log.planning.items():
                    planning[name] = planning.get(name, 0) + n
                for step in log.steps:
                    steps[step] = steps.get(step, 0) + 1
        rows.append(
            Row(
                tier=target.tier,
                model=target.model,
                policies=policies,
                measured_at=measured_at,
                sha=sha,
                hardware=target.hardware,
                server_version=target.server_version,
                turn_path=driver.name,
                protocol=True,
                protocol_reason=None,
                capability=capability,
                e06=e06_cell(e06),
                turns=len(latencies),
                p50_ms=round(nearest_rank(latencies, 50)) if latencies else None,
                p95_ms=round(nearest_rank(latencies, 95)) if latencies else None,
                calls_per_turn=calls_per_turn(logs),
                latencies_ms=latencies,
                designed_calls_per_turn=breakdown.designed_calls_per_turn,
                extra_calls=breakdown.extra_calls,
                failed_calls=breakdown.failed_calls,
                faulted_turns=breakdown.faulted_turns,
                fallbacks=breakdown.fallbacks,
                turn_kind=kind,
                deadline_override_ms=override,
                profile=None if three is None else three.profile,
                perception=None if three is None else three.perception,
                planning=planning,
                degradation=None if steps is None else dict(sorted(steps.items())),
            )
        )
    return rows


@contextlib.asynccontextmanager
async def database(url: str | None) -> AsyncIterator[str]:
    """``url``, or a throwaway PostgreSQL started through testcontainers; migrated to head."""
    from alembic import command  # lazy: only the tool needs it
    from alembic.config import Config

    def migrate(dsn: str) -> None:
        config = Config(str(ALEMBIC_INI))
        config.set_main_option("sqlalchemy.url", dsn.replace("%", "%%"))
        command.upgrade(config, "head")

    if url is not None:
        await asyncio.to_thread(migrate, url)
        yield url
        return
    from testcontainers.community.postgres import PostgresContainer  # lazy: dev only

    container = PostgresContainer(PG_IMAGE, driver="psycopg")
    await asyncio.to_thread(container.start)
    try:
        dsn = container.get_connection_url()
        await asyncio.to_thread(migrate, dsn)
        yield dsn
    finally:
        await asyncio.to_thread(container.stop)


def make_settings(dsn: str) -> Settings:
    return Settings.model_validate(
        {
            "database_url": dsn,
            "model_adapter": "openai",
            "config_key": Fernet.generate_key().decode(),
            # The tool calls the library in process; no REST call needs this credential.
            "service_credentials": [secrets.token_urlsafe(32)],
        }
    )


async def _main(args: argparse.Namespace) -> int:
    target = Target(
        base_url=args.base_url.rstrip("/"),
        model=args.model,
        api_key=args.api_key,
        tier=args.tier,
        hardware=args.hardware,
        server_version=args.server_version,
        purpose_models=dict(args.purpose_model),
        embedding_base_url=getattr(args, "embedding_base_url", None),
        embedding_model=getattr(args, "embedding_model", None),
        embedding_api_key=getattr(args, "embedding_api_key", None),
    )
    embedded: list[EmbeddingRow] = []
    failure: RowFailure | None = None
    async with database(args.database_url) as dsn:
        settings = make_settings(dsn)
        db = make_engine(settings)
        engine = Engine(settings, db)
        try:
            rows = await run_matrix(
                engine,
                target,
                turns=args.turns,
                turn_kinds=tuple(getattr(args, "turn_kinds", ("lite",))),
            )
            if rows is not None and target.embedding_model:
                embedded.append(
                    await embedding_row(
                        engine.models.embeddings, tier=target.tier, model=target.embedding_model
                    )
                )
        except MatrixInterrupted as stopped:
            # #454: the finished rows are written, and the failed row is marked failed. No
            # partial row is written: its timings would mislead.
            rows, failure = stopped.rows, stopped.failure
            print(
                f"model call failed in the {failure.turn_kind} {failure.policies} row at turn "
                f"{failure.turn}: {failure.code} (purpose {failure.purpose}, reason "
                f"{failure.reason}); writing {len(rows)} finished rows",
                file=sys.stderr,
            )
        except ModelError as exc:
            # A failure outside the rows' turns writes nothing.
            print(f"model call failed: {exc.code} {exc.details}", file=sys.stderr)
            rows = None
        finally:
            await engine.models.aclose()
            await db.dispose()
    if rows is None:
        return EX_UNAVAILABLE
    merged = merge(load_rows(args.raw), rows)
    write_matrix(
        merged,
        args.output,
        args.raw,
        merge_embedding(load_embedding_rows(args.raw), embedded),
        merge_failures(load_failed_rows(args.raw), rows, failure),
    )
    print(f"wrote {args.output} and {args.raw} ({len(rows)} rows for {target.tier})")
    return 0 if failure is None else EX_UNAVAILABLE


LIVE_TURN_KINDS: tuple[TurnKind, ...] = ("lite", "structured")
LIVE_CHOICES: tuple[TurnKind, ...] = (*LIVE_TURN_KINDS, *THREE_CHARACTER_KINDS)
"""What ``--turn-kinds`` accepts: the default kinds and the three-character ones (#182)."""
"""The turn kinds a live run measures: a repair-heavy row needs the stub's scripted
contradictions (``test_p_08__structured`` generates it)."""


def _turn_kinds(value: str) -> list[TurnKind]:
    kinds = [k.strip() for k in value.split(",") if k.strip()]
    wrong = [k for k in kinds if k not in LIVE_CHOICES]
    if not kinds or wrong:
        raise argparse.ArgumentTypeError(
            f"expected a comma-separated subset of {', '.join(LIVE_CHOICES)}, got {value!r}"
        )
    return [k for k in LIVE_CHOICES if k in kinds]


def _purpose_model(value: str) -> tuple[str, str]:
    purpose, sep, model = value.partition("=")
    if not sep or purpose not in STORY_PURPOSES or not model:
        raise argparse.ArgumentTypeError(f"expected <story purpose>=<model>, got {value!r}")
    return purpose, model


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m epistrel.tools.model_matrix")
    p.add_argument("--base-url", required=True, help="the endpoint, e.g. http://localhost:1234/v1")
    p.add_argument("--model", required=True, help="the model every purpose uses by default")
    p.add_argument("--api-key", default="lm-studio", help="the endpoint's API key")
    p.add_argument("--tier", default="local", help="the tier label of the rows (default local)")
    p.add_argument("--hardware", default="", help="where the model ran, e.g. 'Apple M3 Max'")
    p.add_argument("--server-version", default="", help="the model server and its version")
    p.add_argument(
        "--purpose-model",
        type=_purpose_model,
        action="append",
        default=[],
        metavar="PURPOSE=MODEL",
        help="a per-purpose model override (repeatable)",
    )
    p.add_argument("--embedding-base-url", help="an /embeddings endpoint to measure too")
    p.add_argument("--embedding-model", help="the embedding model at --embedding-base-url")
    p.add_argument("--embedding-api-key", help="its API key (default: --api-key)")
    p.add_argument("--database-url", help="a scratch database instead of a testcontainer")
    p.add_argument("--turns", type=int, default=DEFAULT_TURNS, help="measured turns per row")
    p.add_argument(
        "--turn-kinds",
        type=_turn_kinds,
        default=list(LIVE_TURN_KINDS),
        help="comma-separated: lite, structured (default both; repair-heavy rows are stub-only)",
    )
    p.add_argument("--output", type=Path, default=MATRIX_PATH, help=argparse.SUPPRESS)
    p.add_argument("--raw", type=Path, default=RAW_PATH, help=argparse.SUPPRESS)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.turns < 1:
        print("error: --turns must be at least 1", file=sys.stderr)
        return 2
    return asyncio.run(_main(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
