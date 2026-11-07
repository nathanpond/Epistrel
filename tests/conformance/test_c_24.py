"""C-24's M8 parts: what every image read says about its source and its output check
(FR-IMG-10; #258).

- Every image read (``GET /media/{id}``, ``GET /media``, the upload's own answer) returns
  ``source`` and ``check_status``; an upload with no checker configured reads ``unchecked``,
  never ``passed``, with ``check: null``.
- A ``passed`` image lists, per applicable policy, its result and whether its checker is
  certified for that policy (``image_check_certifications``): a checker certified for the minors
  policy but not for a rating policy reports ``certified: true`` for the first and ``false`` for
  the second. No schema has a single, top-level certification field.
- The API documentation (the media and image schemas) and ``docs/OPERATIONS.md`` § Images state
  both of FR-IMG-10's sentences verbatim.

#261/#263 add the story-image parts (``viewpoint_basis``) here; every C-24 M8 part lives in this
file (the traceability gate takes one file per test ID and milestone)."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api.app import create_app
from epistrel.config import Settings
from epistrel.core.ids import new_ulid
from epistrel.core.media.checks import PASSED_STATEMENT, UNCHECKED_STATEMENT
from epistrel.core.ratings import RatingFields, RatingReplacement, create_rating, update_rating
from epistrel.engine import Engine
from epistrel.orchestrator.images import jobs as image_jobs
from epistrel.orchestrator.images import output_check
from epistrel.orchestrator.policies.schemas import PolicyFields
from epistrel.testing.images import TestImageTarget, registered_target
from tests.image_job_support import (
    CTX,
    MINORS,
    configure,
    image_engine,
    image_settings_for,
    insert,
    prepared,
)
from tests.image_safety_support import whisper_story
from tests.image_support import Outputs, StubImageChecker, StubImageGenerator, registered
from tests.media_support import READ, uploaded
from tests.model_support import admin_client
from tests.policy_support import OPERATOR, unique

ADAPTER = "stub_sync"
REPO = Path(__file__).resolve().parents[2]
OPERATIONS = REPO / "docs" / "OPERATIONS.md"


@pytest.fixture
def image_settings(pg_url: str, tmp_path: Path) -> Settings:
    return image_settings_for(pg_url, tmp_path)


@pytest.fixture
async def lib(image_settings: Settings, engine: AsyncEngine) -> AsyncIterator[Engine]:
    async with image_engine(image_settings, engine) as epistrel:
        yield epistrel


@pytest.fixture
def target() -> Iterator[TestImageTarget]:
    with registered_target() as found:
        yield found


def _read_fields(asset: dict[str, Any]) -> tuple[Any, Any, Any]:
    return asset["source"], asset["check_status"], asset["check"]


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-10")
async def test_c_24__m8_metadata(
    lib: Engine, image_settings: Settings, engine: AsyncEngine, target: TestImageTarget
) -> None:
    """Every image read returns ``source`` and ``check_status``. An upload with no checker is
    ``unchecked`` with ``check: null`` in its upload answer, ``GET /media/{id}`` and the
    ``GET /media`` listing, never ``passed``; the stored row agrees.

    Complement asserted: a generated image promoted under a checker that passed it reads
    ``generated`` and ``passed`` with its checker's model and per-policy results, in the same
    reads."""
    async with admin_client(image_settings, lib) as client:
        body = await uploaded(client, caption="the lighthouse at dusk")
        assert _read_fields(body) == ("uploaded", "unchecked", None)
        one = (await client.get(f"/media/{body['id']}", headers=READ)).json()
        assert _read_fields(one) == ("uploaded", "unchecked", None)
        listed = (await client.get("/media", params={"limit": 200}, headers=READ)).json()
        assert listed["items"]
        assert all({"source", "check_status", "check"} <= set(a) for a in listed["items"])
        mine = next(a for a in listed["items"] if a["id"] == body["id"])
        assert _read_fields(mine) == ("uploaded", "unchecked", None)
        assert all(
            (a["check_status"] == "passed") == (a["check"] is not None) for a in listed["items"]
        )
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT check_status, check_model, policy_results FROM media_assets "
                        "WHERE id = :m"
                    ),
                    {"m": body["id"]},
                )
            ).one()
        assert (row.check_status, row.check_model, row.policy_results) == ("unchecked", None, [])

        stub = StubImageGenerator(script=[Outputs()])
        story = (await whisper_story(engine, minors=False))[0]
        with registered(ADAPTER, stub):
            await configure(lib, image_settings, ADAPTER)
            lib.models.stub.reset()
            StubImageChecker(lib.models.stub).answer({MINORS: "pass"})
            job = await insert(lib, prepared(story))
            assert (await image_jobs.run_image_job(lib.image_runtime(), job, 0))[
                "image_job"
            ] == "promoted"
        (asset_id,) = (await lib.get_image_job(CTX, job)).result_ids
        generated = (await client.get(f"/media/{asset_id}", headers=READ)).json()
        assert (generated["source"], generated["check_status"]) == ("generated", "passed")
        assert generated["check"]["model"] == "stub-vision"
        assert [(p["policy_id"], p["result"]) for p in generated["check"]["policies"]] == [
            (MINORS, "pass")
        ]


async def _rating_policy(lib: Engine, engine: AsyncEngine) -> tuple[str, str]:
    """A new rating whose one policy is model-assisted and applies to image outputs; the
    rating's id and the policy's."""
    rating = await create_rating(
        engine, RatingFields(name=unique("image-check-rating")), principal="p-operator"
    )
    body = PolicyFields.model_validate(
        {
            "scope": "rating",
            "scope_id": rating.rating.id,
            "name": unique("no-gore-pictures"),
            "rule": {"type": "model_assisted", "prompt": "No gore in pictures."},
            "applies_to": ["image_output"],
            "message": "No gore in pictures.",
        }
    )
    made = (await lib.policies_create(OPERATOR, body)).policy
    current = rating.rating
    await update_rating(
        engine,
        current.id,
        RatingReplacement(
            name=current.name,
            description=current.description,
            display_order=current.display_order,
            prompt_guidance=current.prompt_guidance,
            image_guidance=current.image_guidance,
            policy_ids=[made.id],
            inherits_from=[],
        ),
        principal="p-operator",
    )
    return current.id, made.id


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-10")
async def test_c_24__m8_certified(
    lib: Engine, image_settings: Settings, engine: AsyncEngine
) -> None:
    """A checker with an ``image_check_certifications`` record for the minors policy but not for
    a rating policy reports ``certified: true`` for the first and ``false`` for the second on a
    ``passed`` upload under that rating: per policy, never one status. The record is keyed by
    the checker's fingerprint (12 hex of its endpoint host, model, reported version and
    ``image_check`` template).

    Complement asserted: the same upload before the record existed reads ``certified: false``
    for both; a record for another fingerprint certifies nothing."""
    rating, policy_id = await _rating_policy(lib, engine)
    async with admin_client(image_settings, lib) as client:
        with registered(ADAPTER, StubImageGenerator(script=[])):
            await configure(lib, image_settings, ADAPTER)
        lib.models.stub.reset()
        StubImageChecker(lib.models.stub).answer({MINORS: "pass", policy_id: "pass"})
        found = await output_check.checker(lib.models, lib.db)
        assert found is not None
        assert re.fullmatch(r"[0-9a-f]{12}", found.fingerprint)

        def certified(asset: dict[str, Any]) -> dict[str, bool]:
            assert asset["check_status"] == "passed"
            return {p["policy_id"]: p["certified"] for p in asset["check"]["policies"]}

        before = await uploaded(client, content_rating_id=rating)
        assert certified(before) == {MINORS: False, policy_id: False}
        record = new_ulid()
        other = new_ulid()
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO image_check_certifications (id, checker_fingerprint, "
                        "policy_id) VALUES (:i, :f, :p), (:o, :x, :q)"
                    ),
                    {
                        "i": record,
                        "f": found.fingerprint,
                        "p": MINORS,
                        "o": other,
                        "x": "0" * 12,
                        "q": policy_id,
                    },
                )
            after = await uploaded(client, content_rating_id=rating)
            assert certified(after) == {MINORS: True, policy_id: False}
            names = {p["policy_id"]: p["name"] for p in after["check"]["policies"]}
            assert names[MINORS]
            assert "certified" not in after
            assert "certified" not in after["check"]
        finally:
            async with engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM image_check_certifications WHERE id IN (:i, :o)"),
                    {"i": record, "o": other},
                )


def _schemas_with(document: Any, name: str, path: str = "") -> list[tuple[str, dict[str, Any]]]:
    """Every schema object under ``document`` whose ``properties`` include ``name``."""
    found: list[tuple[str, dict[str, Any]]] = []
    if isinstance(document, dict):
        properties = document.get("properties")
        if isinstance(properties, dict) and name in properties:
            found.append((path, document))
        for key, value in document.items():
            found.extend(_schemas_with(value, name, f"{path}/{key}"))
    elif isinstance(document, list):
        for index, value in enumerate(document):
            found.extend(_schemas_with(value, name, f"{path}/{index}"))
    return found


def _normal(value: str) -> str:
    return " ".join(value.split())


def _section(markdown: str, heading: str) -> str:
    start = markdown.index(f"## {heading}\n")
    rest = markdown[start + len(heading) + 4 :]
    end = rest.find("\n## ")
    return rest if end < 0 else rest[:end]


@pytest.mark.part("M8", "FR-IMG-10")
def test_c_24__m8_statements(image_settings: Settings) -> None:
    """The OpenAPI document's media and image schemas (``MediaAsset``, the upload answer, the
    ``check`` block, ``full``'s image detail) and ``docs/OPERATIONS.md`` § Images state both
    sentences verbatim (whitespace-normalised). No component has a top-level ``certified``
    field: every schema with one is a per-policy result (it has ``policy_id``).

    Complement asserted: ``MediaAsset`` has ``check_status`` and ``check``, and the per-policy
    result schema has ``certified``."""
    document = create_app(image_settings).openapi()
    schemas = document["components"]["schemas"]
    for name in ("MediaAsset", "MediaUploadedBody", "ImageCheck", "CharacterImageDetail"):
        described = _normal(str(schemas[name].get("description", "")))
        for sentence in (UNCHECKED_STATEMENT, PASSED_STATEMENT):
            assert _normal(sentence) in described, (name, sentence)
    assert {"check_status", "check"} <= set(schemas["MediaAsset"]["properties"])
    with_certified = _schemas_with(document["components"], "certified")
    assert with_certified, "the per-policy result carries `certified`"
    for path, schema in with_certified:
        assert "policy_id" in schema["properties"], f"{path} reports certification on its own"
    images = _normal(_section(OPERATIONS.read_text(encoding="utf-8"), "Images"))
    for sentence in (UNCHECKED_STATEMENT, PASSED_STATEMENT):
        assert _normal(sentence) in images
