"""Seed P-04's ten Lite reference stories directly through the library (#76).

Each story is ``epistrel.testing.perf``'s reference story (cast 1 + 1, Teen, queued turns, 50
pins, 10,000 turns = 20,000 events), seeded with the stub model and no latency, and its rolling
summary is then brought up to date in this process (``drain_summaries``), so the measured turns
send the summary and the verbatim window as a long-running story does. ``python -m
perf.lite_smoke.seed`` seeds into ``EPISTREL_PERF_DSN`` and prints the stories as JSON.

``cache_key`` names the cached ``pg_dump`` of a finished seed: the migration head, a hash of the
seeding code and its arguments, and the engine's git SHA.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from epistrel.config import Settings
from epistrel.core.context import RequestContext
from epistrel.core.db import make_engine
from epistrel.engine import Engine
from epistrel.testing import perf
from perf.lite_smoke import REPO_ROOT, STORIES, Target


@dataclass(frozen=True, slots=True)
class StoryRef:
    """A seeded story as the driver needs it."""

    story_id: str
    player_id: str
    other_id: str
    turns: int
    index: int


def seed_settings(dsn: str) -> Settings:
    """Library settings for seeding: the stub model with no latency, no worker."""
    return Settings.model_validate(
        {
            "database_url": dsn,
            "service_credentials": ["perf-seed-credential-000000000000000000"],
            "model_adapter": "stub",
            "stub_latency_s": 0.0,
            "mode": "api",
        }
    )


async def seed(
    target: Target,
    *,
    stories: int = STORIES,
    turns: int = perf.TURNS,
    log: Callable[[str], None] = print,
) -> list[StoryRef]:
    """Seed ``stories`` reference stories and drain their summaries; return them."""
    settings = seed_settings(target.dsn)
    db = make_engine(settings)
    epistrel = Engine(settings, db=db)
    ctx = RequestContext(principal=target.principal)
    try:

        async def one(index: int) -> StoryRef:
            story = await perf.seed_story(
                epistrel,
                ctx,
                f"P-04 reference story {index + 1}",
                turns=turns,
                seed=index,
                progress=lambda done: (
                    log(f"story {index + 1}: {done} turns") if done % 2_000 == 0 else None
                ),
            )
            runs = await perf.drain_summaries(epistrel, story.story_id)
            log(f"story {index + 1}: seeded, {runs} summary runs")
            return StoryRef(story.story_id, story.player_id, story.other_id, story.turns, index)

        return list(await asyncio.gather(*(one(i) for i in range(stories))))
    finally:
        await epistrel.models.aclose()
        await db.dispose()


def _git_sha() -> str:
    found = subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607 - git from PATH
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return found.stdout.strip() or "unknown"


def _alembic_head() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    return str(script.get_current_head())


def cache_key(stories: int, turns: int) -> str:
    """The cached dump's name: migration head + seed hash + engine SHA (planner, #76)."""
    sources = [Path(perf.__file__), Path(__file__)]
    digest = hashlib.sha256()
    for source in sources:
        digest.update(source.read_bytes())
    digest.update(f"{perf.SEED_VERSION}:{stories}:{turns}".encode())
    return f"{_alembic_head()}-{digest.hexdigest()[:12]}-{_git_sha()[:12]}"


def write_stories(path: Path, found: list[StoryRef]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(s) for s in found], indent=2) + "\n")


def read_stories(path: Path) -> list[StoryRef]:
    return [StoryRef(**item) for item in json.loads(path.read_text())]


def main() -> None:
    found = asyncio.run(seed(Target.from_env(), log=lambda line: print(line, file=sys.stderr)))
    print(json.dumps([asdict(s) for s in found], indent=2))


if __name__ == "__main__":
    main()
