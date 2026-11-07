"""Helpers for the story image generation tests (#263): the REST app with the stub model adapter,
the stub image adapter and scripted checks; the whisper scene on the structured profile at
perception ``full``; the JSON generation request; the job run directly; and what the stub
``image_prompt`` model and image adapter received.

**The whisper scene** (``whisper``). Wren is the player; Jenni, Marcus and Bobby are in the
parlor, and a guard stands in the alcove behind a curtain (a ``thin_wall``: in canon, never
perceived from the parlor). In turn 1 Jenni whispers ``WORDS`` to Marcus (Bobby holds only the
envelope, "Jenni whispers something to Marcus") and leans in (``LEAN``, which everyone sees), and
the narrator's ``latent`` beat records her affair (``AFFAIR``): only ``omniscient`` receives it.
Jenni's catalog snapshot carries a visual identity with a ``perceivable`` reference and a
``restricted`` one (a hidden tattoo); Marcus's carries a seed and a ``perceivable`` reference.

**The masked stranger** (``stranger_scene``): Wren, Bobby and Marcus in the parlor, Marcus masked:
the person entity "masked stranger" is Marcus (``same_as`` in canon, visible to Marcus only), so
Bobby sees a masked stranger, not Marcus. Marcus's catalog snapshot carries his appearance, a seed
and a ``perceivable`` reference.

Every generated output gets a size of its own (``Outputs(size=…)``), so no two tests share a
checksum on the shared database."""

from __future__ import annotations

import itertools
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx2 as httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.canon.facts import Visibility
from epistrel.core.enums import OperationKind
from epistrel.core.idempotency import IDEMPOTENCY_HEADER
from epistrel.core.ids import new_ulid
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import SAME_AS
from epistrel.engine import Engine
from epistrel.orchestrator.images import jobs as image_jobs
from epistrel.orchestrator.models.stub import StubAdapter, StubCall
from epistrel.testing.images import ScriptedChecks, installed_checks
from tests import perception_support as ps
from tests.canon_support import commit
from tests.image_job_support import clean, configure
from tests.image_support import Outputs, StubImageGenerator, registered
from tests.media_support import png, uploaded
from tests.model_support import restored_model_config
from tests.respond_support import body, respond, respond_client
from tests.story_support import WRITE
from tests.structured_turn_support import beat, render, structured_story

ADAPTER = "stub_sync"
PURPOSE = "image_prompt"
WORDS = "The key is under the blue mat."
LEAN = "She leans in close and lowers her voice."
AFFAIR = "Jenni and Marcus have been lovers since the spring fair."
STRANGER = "masked stranger"
MARCUS_LOOK = "a broad-shouldered sailor with a salt-white beard"
JENNI_LOOK = "a slight woman with a red scarf"
TATTOO = "a hidden serpent tattoo along her spine"
GUARD_LOOK = "a guard in a dented helm with a halberd"
PARLOR_DESCRIPTION = "a low parlor whose curtained alcove hides an armed guard"
MARCUS_SEED = 424_242
STYLE = "inked woodcut, muted ochres"
STYLE_NEGATIVE = "photograph, neon"
_SIZES = itertools.count(101)


def character(character_id: str) -> str:
    return f"character:{character_id}"


def next_size() -> tuple[int, int]:
    """A fresh output size: no two outputs share a checksum."""
    n = next(_SIZES)
    return (n, n + 1)


@dataclass
class GenerationApp:
    client: httpx.AsyncClient
    lib: Engine
    generator: StubImageGenerator
    checks: ScriptedChecks
    settings: Settings
    configured: bool = False
    ids: dict[str, str] = field(default_factory=dict)

    @property
    def stub(self) -> StubAdapter:
        stub = self.lib.models.stub
        assert stub is not None
        return stub

    async def configure(self, *, check: bool = True) -> None:
        """The image adapter and (with ``check``) an ``image_check`` model, once: after the
        reference uploads, which an ``image_check`` model would check too."""
        if not self.configured:
            await configure(self.lib, self.settings, ADAPTER, check=check)
        self.configured = True

    def prompt_calls(self) -> list[StubCall]:
        return [c for c in self.stub.calls if c.purpose == PURPOSE]

    def adapter_prompts(self) -> list[str]:
        return [r.request.prompt for r in self.generator.requests]


@asynccontextmanager
async def generation_app(settings: Settings, engine: AsyncEngine) -> AsyncIterator[GenerationApp]:
    """The REST app with the stub model adapter, the stub image adapter and scripted checks
    that pass every prompt and output (``GenerationApp.configure`` sets the adapter up); the
    model configuration, the image job tables and the stories' perception overrides restored
    afterwards."""
    generator = StubImageGenerator()
    async with restored_model_config(engine), respond_client(settings, engine) as (client, lib):
        lib.prepare_media()
        try:
            with (
                registered(ADAPTER, generator),
                installed_checks(ScriptedChecks(fail_closed=False)) as checks,
            ):
                yield GenerationApp(client, lib, generator, checks, settings)
        finally:
            await clean(engine)
            await ps.forget_perception(engine)


async def snapshot(
    engine: AsyncEngine, story_id: str, character_id: str, document: dict[str, Any]
) -> None:
    """The catalog version a story character was made from (``character_snapshots``): its
    ``definition.appearance`` and ``visual_identity``."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO character_snapshots (story_id, character_id, source_catalog_id, "
                "version, content, published_at) VALUES (:s, :c, :k, 1, CAST(:d AS jsonb), "
                "now()) ON CONFLICT (story_id, character_id) DO UPDATE SET content = "
                "EXCLUDED.content"
            ),
            {"s": story_id, "c": character_id, "k": new_ulid(), "d": json.dumps(document)},
        )


def look(appearance: str, **identity: Any) -> dict[str, Any]:
    document: dict[str, Any] = {"definition": {"appearance": appearance}}
    if identity:
        document["visual_identity"] = identity
    return document


async def styled(engine: AsyncEngine, story_id: str, *, parlor: str | None = None) -> None:
    """The story's scenario's visual style (``STYLE``, ``STYLE_NEGATIVE``) and, with ``parlor``,
    its world's description of the parlor (canon: it may hold a concealed feature)."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE stories SET scenario_id = coalesce(scenario_id, :k), "
                "scenario_version = coalesce(scenario_version, 1), "
                "scenario_snapshot = coalesce(scenario_snapshot, '{}'::jsonb) "
                "|| jsonb_build_object('visual_style', jsonb_build_object('prompt', "
                "CAST(:p AS text), 'negative_prompt', CAST(:n AS text))) "
                "|| jsonb_build_object('world', CAST(:w AS jsonb)) WHERE id = :s"
            ),
            {
                "s": story_id,
                "k": new_ulid(),
                "p": STYLE,
                "n": STYLE_NEGATIVE,
                "w": json.dumps(
                    {"locations": []}
                    if parlor is None
                    else {"locations": [{"id": "parlor", "name": "parlor", "description": parlor}]}
                ),
            },
        )


@dataclass
class Scene:
    story_id: str
    ids: dict[str, str]
    places: dict[str, str]
    references: dict[str, str] = field(default_factory=dict)


async def whisper(app: GenerationApp, engine: AsyncEngine, *, check: bool = True) -> Scene:
    """The whisper scene (module docstring), turn 1 played; the adapter configured (and, with
    ``check``, an ``image_check`` model)."""
    story, ids = await structured_story(app.client, "Wren", "Jenni", "Marcus", "Bobby", "Guard")
    await ps.perceive(engine, story, "full")
    places = {n: await ps.place_entity(engine, story, n) for n in ("parlor", "alcove")}
    await ps.adjoin(engine, story, places["parlor"], places["alcove"], "thin_wall")
    for name in ("Wren", "Jenni", "Marcus", "Bobby"):
        await ps.put(engine, story, ids[name], places["parlor"])
    await ps.put(engine, story, ids["Guard"], places["alcove"])
    references = {
        "jenni": (await uploaded(app.client, png(nonce=new_ulid())))["id"],
        "tattoo": (await uploaded(app.client, png(nonce=new_ulid())))["id"],
        "marcus": (await uploaded(app.client, png(nonce=new_ulid())))["id"],
    }
    await snapshot(
        engine,
        story,
        ids["Jenni"],
        look(
            JENNI_LOOK,
            base_appearance_prompt="slight woman, red scarf",
            reference_images=[
                {"media_id": references["jenni"], "audience": "perceivable"},
                {"media_id": references["tattoo"], "audience": "restricted"},
            ],
            restricted_details_prompt=TATTOO,
        ),
    )
    await snapshot(
        engine,
        story,
        ids["Marcus"],
        look(
            MARCUS_LOOK,
            seed=MARCUS_SEED,
            reference_images=[{"media_id": references["marcus"], "audience": "perceivable"}],
        ),
    )
    await snapshot(engine, story, ids["Guard"], look(GUARD_LOOK))
    await styled(engine, story, parlor=PARLOR_DESCRIPTION)
    await app.configure(check=check)
    answer = render(
        beat(
            "say",
            WORDS,
            intent="sincere",
            whisper_to=[ids["Marcus"]],
            addressee=ids["Marcus"],
        ),
        beat("do", LEAN),
        beat("latent", AFFAIR),
    )
    app.stub.script("generation", [answer])
    played = await respond(app.client, story, body(ids["Wren"], "What's going on?"))
    assert played.status_code == 200, played.text
    return Scene(story, ids, places, references)


async def stranger_scene(app: GenerationApp, engine: AsyncEngine) -> Scene:
    """The masked stranger (module docstring), turn 1 played; the adapter configured."""
    story, ids = await structured_story(app.client, "Wren", "Bobby", "Marcus")
    await ps.perceive(engine, story, "full")
    places = {n: await ps.place_entity(engine, story, n) for n in ("parlor",)}
    for name in ("Wren", "Bobby", "Marcus"):
        await ps.put(engine, story, ids[name], places["parlor"])
    async with engine.begin() as conn:
        ids["Stranger"] = await store.add_entity(conn, story, "person", STRANGER)
    references = {"marcus": (await uploaded(app.client, png(nonce=new_ulid())))["id"]}
    await snapshot(
        engine,
        story,
        ids["Marcus"],
        look(
            MARCUS_LOOK,
            seed=MARCUS_SEED,
            reference_images=[{"media_id": references["marcus"], "audience": "perceivable"}],
        ),
    )
    await masked(engine, Scene(story, ids, places))
    await app.configure()
    app.stub.script("generation", [render(beat("do", "He bows without a word."))])
    played = await respond(app.client, story, body(ids["Wren"], "Who is that?"))
    assert played.status_code == 200, played.text
    return Scene(story, ids, places, references)


def _identity(scene: Scene) -> Proposition:
    return Proposition(
        subject_id=scene.ids["Stranger"],
        predicate_id=SAME_AS,
        object_id=scene.ids["Marcus"],
        valid_time=Interval.of(None, None),
    )


async def masked(engine: AsyncEngine, scene: Scene) -> None:
    """Canon: the masked stranger is Marcus, visible to Marcus only."""
    await commit(
        engine,
        scene.story_id,
        _identity(scene),
        visibility=Visibility(scope="characters", characters=(scene.ids["Marcus"],)),
        kind=OperationKind.CONTROL,
        provenance="author",
    )


async def learns_identity(engine: AsyncEngine, scene: Scene, holder: str) -> None:
    """``holder`` comes to believe that the stranger is Marcus (its own partition, #159)."""
    from tests.partition_support import believe

    await believe(engine, scene.story_id, holder, _identity(scene), kind=OperationKind.CONTROL)


async def request(
    app: GenerationApp, story_id: str, *, key: str | None = None, **fields: Any
) -> httpx.Response:
    """``POST /stories/{id}/images`` with a JSON generation request."""
    headers = {**WRITE, **({IDEMPOTENCY_HEADER: key} if key is not None else {})}
    return await app.client.post(f"/stories/{story_id}/images", json=fields, headers=headers)


async def queued(app: GenerationApp, story_id: str, **fields: Any) -> str:
    """A generation request that must answer 202; its job id."""
    response = await request(app, story_id, **fields)
    assert response.status_code == 202, response.text
    assert response.headers["Location"] == f"/image-jobs/{response.json()['job_id']}"
    return str(response.json()["job_id"])


async def run(app: GenerationApp, job_id: str, outputs: Outputs | None = None) -> dict[str, Any]:
    """Run the job's handler directly with one scripted output (no worker claims another
    test's job)."""
    app.generator.script.append(outputs or Outputs(size=next_size()))
    return await image_jobs.run_image_job(app.lib.image_runtime(), job_id, 0)


async def generated(app: GenerationApp, story_id: str, **fields: Any) -> dict[str, Any]:
    """Request and run a job that promotes; the job row."""
    job_id = await queued(app, story_id, **fields)
    done = await run(app, job_id)
    assert done["image_job"] == "promoted", done
    return await job_row(app.lib.db, job_id)


async def built(app: GenerationApp, story_id: str, **target: Any) -> httpx.Response:
    """``POST /image-prompts`` for a story target (``kind: story``)."""
    return await app.client.post(
        "/image-prompts",
        json={"target": {"kind": "story", "story_id": story_id, **target}},
        headers=WRITE,
    )


async def story_image_rows(engine: AsyncEngine, story_id: str) -> list[dict[str, Any]]:
    """The story's ``story_images`` rows with their asset's source and viewpoint basis."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT i.*, m.source AS asset_source, m.viewpoint_basis AS asset_basis, "
                "m.depicted_subjects AS asset_subjects, m.state AS asset_state "
                "FROM story_images i JOIN media_assets m ON m.id = i.asset_id "
                "WHERE i.story_id = :s ORDER BY i.created_at"
            ),
            {"s": story_id},
        )
        return [dict(r._mapping) for r in rows]


async def job_row(engine: AsyncEngine, job_id: str) -> dict[str, Any]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(text("SELECT * FROM image_jobs WHERE id = :j"), {"j": job_id})
        ).one()
    return dict(row._mapping)


def payload(call: StubCall) -> str:
    return "\n".join(m.content for m in call.messages)


async def counts(engine: AsyncEngine, story_id: str) -> dict[str, int]:
    """The story's image jobs and reservations."""
    async with engine.connect() as conn:
        jobs = await conn.scalar(
            text("SELECT count(*) FROM image_jobs WHERE story_id = :s"), {"s": story_id}
        )
        held = await conn.scalar(
            text("SELECT count(*) FROM image_reservations WHERE story_id = :s"), {"s": story_id}
        )
    return {"image_jobs": int(jobs or 0), "image_reservations": int(held or 0)}


__all__ = [
    "ADAPTER",
    "AFFAIR",
    "GUARD_LOOK",
    "JENNI_LOOK",
    "LEAN",
    "MARCUS_LOOK",
    "MARCUS_SEED",
    "PARLOR_DESCRIPTION",
    "STRANGER",
    "STYLE",
    "STYLE_NEGATIVE",
    "TATTOO",
    "WORDS",
    "GenerationApp",
    "Scene",
    "built",
    "character",
    "counts",
    "generated",
    "generation_app",
    "job_row",
    "learns_identity",
    "look",
    "next_size",
    "payload",
    "queued",
    "request",
    "run",
    "snapshot",
    "story_image_rows",
    "stranger_scene",
    "styled",
    "whisper",
]
