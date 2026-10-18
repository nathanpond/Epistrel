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
   (#303).

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
is the input check or the first generation, the turn fails and the run writes nothing. A row
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
from epistrel.core.policies.model import ModelAssistedRule, Policy
from epistrel.core.policies.model import Target as PolicyTarget
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
from epistrel.orchestrator.models.registry import GENERATION, GUARDRAIL_CHECK, STORY_PURPOSES
from epistrel.orchestrator.policies.output_gate import DEADLINE
from epistrel.tools.generated import REPO_ROOT

MATRIX_PATH = REPO_ROOT / "docs" / "model-matrix.md"
RAW_PATH = REPO_ROOT / "docs" / "model-matrix.json"
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
PG_IMAGE = "pgvector/pgvector:pg16"
PRINCIPAL = "model-matrix"
DEFAULT_TURNS = 20
TURN_DEADLINE_S = 300.0
TURN_PATH = "respond (#57)"
GUARDED_REPLY_PATH = "guarded reply (#53), until respond (#57)"
EX_UNAVAILABLE = 1
EMBEDDING_CALLS = 10
EMBEDDING_BATCHES = (64, 16, 1)
EMBEDDING_TEXT = "The lighthouse keeper keeps a lamp lit through the storm."

type PolicySet = Literal["off", "minors"]
POLICY_SETS: tuple[PolicySet, ...] = ("off", "minors")
POLICY_LABELS: Mapping[PolicySet, str] = {"off": "policies off", "minors": "minors policy on"}

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
    ) -> None:
        super().__init__()
        self.calls.extend(calls)
        self.designed = dict(designed)
        self.fallback_causes = list(fallback_causes)
        self.max_calls = max_calls

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

    @property
    def key(self) -> str:
        return row_key(self.tier, self.model, self.policies)


def row_key(tier: str, model: str, policies: str) -> str:
    return f"{tier}|{model}|{policies}"


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
    merged = {row_key(r["tier"], r["model"], r["policies"]): dict(r) for r in existing}
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
    return [{**dict.fromkeys(BREAKDOWN), **row} for row in rows]


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
releases the safe fallback. If it is the input check or the first generation, the turn fails and
the run writes no row.
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


EMBEDDING_NOTE = """\
The embedding endpoint (the `embedding` port, #138), measured when the run names one:
**Protocol** is `GET /models` and one embedding; **Dimension** is what every vector of that
model has (the deployment pins it at the first embedding); **Max batch** is the largest of 64,
16 and 1 texts one request embeds; **p50** is over single-text calls.
"""


def render(
    rows: Sequence[Mapping[str, Any]], embedding_rows: Sequence[Mapping[str, Any]] = ()
) -> str:
    """``docs/model-matrix.md`` for ``rows``. The notes appear only while they apply: *not
    recorded* while a row lacks the call breakdown, and the turn-path note while a row was
    measured on the guarded reply (#303)."""
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
                f"{row['tier']}: {row['model']}",
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
            label = f"{row['tier']}: {row['model']}"
            policies = POLICY_LABELS.get(row["policies"], row["policies"])
            lines.append("| " + " | ".join([label, policies, *_breakdown_cells(row)]) + " |")
        lines += ["", "## Runs", ""]
        lines += ["| Tier | Policies | Measured | Engine SHA | Hardware | Server | Turn path |"]
        lines += ["|---|---|---|---|---|---|---|"]
        for row in rows:
            lines.append(
                f"| {row['tier']}: {row['model']} | "
                f"{POLICY_LABELS.get(row['policies'], row['policies'])} | {row['measured_at']} | "
                f"`{row['sha']}` | {row['hardware'] or '—'} | {row['server_version'] or '—'} | "
                f"{row['turn_path']} |"
            )
        lines.append("")
    lines += _embedding_lines(embedding_rows)
    lines.append("Raw timings for every row are in [model-matrix.json](model-matrix.json).")
    return "\n".join(lines) + "\n"


def write_matrix(
    rows: Sequence[Mapping[str, Any]],
    markdown: Path,
    raw: Path,
    embedding_rows: Sequence[Mapping[str, Any]] = (),
) -> None:
    document = {"rows": list(rows), "embedding_rows": list(embedding_rows)}
    raw.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", "utf-8")
    markdown.write_text(render(rows, embedding_rows), encoding="utf-8")


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
    await driver.turn(script[0])
    latencies: list[int] = []
    logs: list[TurnCalls] = []
    for said in script[1:]:
        started = time.perf_counter()
        logs.append(await driver.turn(said))
        latencies.append(round((time.perf_counter() - started) * 1000))
    return latencies, logs


async def run_matrix(
    engine: Engine,
    target: Target,
    *,
    turns: int = DEFAULT_TURNS,
    driver_factory: Callable[[], TurnDriver] = RespondDriver,
    now: datetime | None = None,
) -> list[Row] | None:
    """The rows for ``target``, or ``None`` when the endpoint fails the protocol probe."""
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
    for policies in POLICY_SETS:
        driver = driver_factory()
        latencies, logs = await measure_turns(engine, driver, policies, turns)
        breakdown = call_breakdown(logs)
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
    async with database(args.database_url) as dsn:
        settings = make_settings(dsn)
        db = make_engine(settings)
        engine = Engine(settings, db)
        try:
            rows = await run_matrix(engine, target, turns=args.turns)
            if rows is not None and target.embedding_model:
                embedded.append(
                    await embedding_row(
                        engine.models.embeddings, tier=target.tier, model=target.embedding_model
                    )
                )
        except ModelError as exc:
            # A failure part-way through writes nothing: a partial row would mislead.
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
    )
    print(f"wrote {args.output} and {args.raw} ({len(rows)} rows for {target.tier})")
    return 0


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
