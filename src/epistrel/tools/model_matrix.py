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
   counting its model calls by purpose (P-08).

The rows go to ``docs/model-matrix.json`` (raw timings included) and are rendered into
``docs/model-matrix.md``. A row is keyed by tier, model and policies, so a re-run replaces its
own rows and keeps every other tier's. The matrix is **reported, not gated**: CI never runs
it, and the owner commits the files after a local run. Percentiles are nearest-rank.

**Turn path.** Until #57 delivers ``respond``, a Lite turn is #53's guarded reply
(``epistrel.testing.ops.guarded_reply``): the input check for every model-assisted policy, one
``generation`` call, and the output check. Each row records the turn path it measured, and #57
swaps in ``respond``.
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
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from cryptography.fernet import Fernet

from epistrel.config import Settings
from epistrel.core.db import make_engine
from epistrel.core.policies.model import Policy
from epistrel.core.stories import CharacterSpec, StorySpec, create_story_from
from epistrel.engine import Engine
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models import config as model_config
from epistrel.orchestrator.models.capability import (
    CapabilityReport,
    PromptParamsResult,
    assess,
    check_prompt_params,
)
from epistrel.orchestrator.models.errors import ModelError
from epistrel.orchestrator.models.metrics import CallLog, recording
from epistrel.orchestrator.models.registry import DELIVERED
from epistrel.tools.generated import REPO_ROOT

MATRIX_PATH = REPO_ROOT / "docs" / "model-matrix.md"
RAW_PATH = REPO_ROOT / "docs" / "model-matrix.json"
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
PG_IMAGE = "pgvector/pgvector:pg16"
PRINCIPAL = "model-matrix"
DEFAULT_TURNS = 20
TURN_DEADLINE_S = 300.0
TURN_PATH = "guarded reply (#53), until respond (#57)"
EX_UNAVAILABLE = 1

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


class TurnDriver(Protocol):
    """Plays one Lite turn of a prepared story and returns its model calls."""

    name: str

    async def prepare(self, engine: Engine, policies: PolicySet) -> None: ...

    async def turn(self, said: str) -> CallLog: ...


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
class GuardedReplyDriver:
    """A Lite turn through #53's guarded reply (input check, ``generation``, output check)."""

    name: str = TURN_PATH
    engine: Engine | None = None
    story_id: str = ""
    policies: list[Policy] = field(default_factory=list)

    async def prepare(self, engine: Engine, policies: PolicySet) -> None:
        spec = StorySpec(name=f"model matrix ({policies})", characters=cast(policies))
        created = await create_story_from(engine.db, spec, principal=PRINCIPAL)
        self.engine, self.story_id = engine, created.story_id
        self.policies = policy_set(policies)

    async def turn(self, said: str) -> CallLog:
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
        return calls


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
    """The mean number of model calls per turn, by purpose."""
    logs = list(logs)
    totals: dict[str, int] = {}
    for log in logs:
        for purpose, count in log.by_purpose().items():
            totals[purpose] = totals.get(purpose, 0) + count
    return {purpose: round(total / len(logs), 2) for purpose, total in sorted(totals.items())}


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
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = data.get("rows", [])
    return rows


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
    lengths = ", ".join(f"{out}/{cap}" for cap, out in cell["lengths"].items())
    return (
        f"{_mark(cell['passed'])} (tokens {lengths}; temp 0: "
        f"{cell['identical']}/{cell['samples']} identical)"
    )


def _calls_text(calls: Mapping[str, float]) -> str:
    total = round(sum(calls.values()), 2)
    parts = ", ".join(f"{purpose} {count:g}" for purpose, count in calls.items())
    return f"{total:g} ({parts})" if parts else "0"


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
turn** is the mean number of model calls by purpose.
"""


def render(rows: Sequence[Mapping[str, Any]]) -> str:
    """``docs/model-matrix.md`` for ``rows``."""
    purposes = list(DELIVERED)
    header = ["Tier", "Policies", "Protocol", *[f"`{p}`" for p in purposes]]
    header += ["E-06", "Turns", "p50", "p95", "Calls per turn"]
    lines = [INTRO, "## Results", ""]
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
            p50 = f"{row['p50_ms'] / 1000:.2f} s" if row["p50_ms"] is not None else "—"
            p95 = f"{row['p95_ms'] / 1000:.2f} s" if row["p95_ms"] is not None else "—"
            cells += [
                _e06_text(row["e06"]) if row["e06"] else "not run",
                str(row["turns"]),
                p50,
                p95,
                _calls_text(row["calls_per_turn"]),
            ]
            lines.append("| " + " | ".join(cells) + " |")
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
    lines.append("Raw timings for every row are in [model-matrix.json](model-matrix.json).")
    return "\n".join(lines) + "\n"


def write_matrix(rows: Sequence[Mapping[str, Any]], markdown: Path, raw: Path) -> None:
    raw.write_text(json.dumps({"rows": list(rows)}, indent=2, sort_keys=True) + "\n", "utf-8")
    markdown.write_text(render(rows), encoding="utf-8")


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


async def measure_turns(
    engine: Engine, driver: TurnDriver, policies: PolicySet, turns: int
) -> tuple[list[int], list[CallLog]]:
    """One warm-up turn, then ``turns`` timed turns: latencies (ms) and each turn's calls."""
    await driver.prepare(engine, policies)
    script = [SCRIPT[i % len(SCRIPT)] for i in range(turns + 1)]
    await driver.turn(script[0])
    latencies: list[int] = []
    logs: list[CallLog] = []
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
    driver_factory: type[GuardedReplyDriver] = GuardedReplyDriver,
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
    )
    async with database(args.database_url) as dsn:
        settings = make_settings(dsn)
        db = make_engine(settings)
        engine = Engine(settings, db)
        try:
            rows = await run_matrix(engine, target, turns=args.turns)
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
    write_matrix(merged, args.output, args.raw)
    print(f"wrote {args.output} and {args.raw} ({len(rows)} rows for {target.tier})")
    return 0


def _purpose_model(value: str) -> tuple[str, str]:
    purpose, sep, model = value.partition("=")
    if not sep or purpose not in DELIVERED or not model:
        raise argparse.ArgumentTypeError(f"expected <delivered purpose>=<model>, got {value!r}")
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
