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

#261/#263 add the story-image parts (``viewpoint_basis``) here: #263's
``test_c_24__m8_consumer_prompt`` (a consumer-prompted story image's source, basis and declared
subjects). Every C-24 M8 part lives in this file (the traceability gate takes one file per test
ID and milestone)."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api.app import create_app
from epistrel.api.openapi import ERROR_STATUS
from epistrel.config import Settings
from epistrel.core.errors import ImageLimitReached
from epistrel.core.ids import new_ulid
from epistrel.core.media.checks import PASSED_STATEMENT, UNCHECKED_STATEMENT
from epistrel.core.ratings import RatingFields, RatingReplacement, create_rating, update_rating
from epistrel.core.tables.images import ImageJobKind
from epistrel.engine import Engine
from epistrel.orchestrator.images import jobs as image_jobs
from epistrel.orchestrator.images import output_check
from epistrel.orchestrator.policies.schemas import PolicyFields
from epistrel.testing.images import (
    PASS,
    ScriptedChecks,
    TestImageTarget,
    installed_checks,
    registered_target,
)
from tests.image_job_support import (
    CTX,
    MINORS,
    configure,
    image_engine,
    image_settings_for,
    insert,
    prepared,
    staged,
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


# -- #261: a story image's viewpoint fields ------------------------------------------------------


@pytest.mark.part("M8", "FR-IMG-5,10")
async def test_c_24__m8_viewpoint_fields(pg_url: str, engine: AsyncEngine, tmp_path: Path) -> None:
    """``audience_viewpoint`` and ``camera_pov`` are separate fields: a ``first_person`` camera
    for Bobby on a Jenni-audience image is returned only to Jenni's reads (complement: not to
    Bobby's, whose eyes it shows). In the whisper scene's rooms (the adults in the cabin, the
    minors in the barn) a camera for Lily, absent from Bobby's cabin, is 422
    ``camera_pov_not_present`` and stores nothing; complement: Jenni's, present, is 201. Every
    upload reads ``viewpoint_basis: none`` (the answer, the read and the asset)."""
    from tests import story_image_support as si
    from tests.auth_support import auth_settings
    from tests.image_safety_support import with_rooms
    from tests.respond_support import respond_client

    settings = auth_settings(pg_url, model_adapter="stub", media_root=tmp_path / "media")
    story, ids = await whisper_story(engine)
    await with_rooms(engine, story, ids)
    bobby, jenni = si.character(ids["Bobby"]), si.character(ids["Jenni"])
    async with respond_client(settings, engine) as (client, _):
        eyes = {"kind": "first_person", "character_id": ids["Bobby"]}
        shown = await si.placed(
            client, story, si.meta({"slot_after_turn": 0}, jenni, camera_pov=eyes)
        )
        assert (shown["audience_viewpoint"], shown["camera_pov"]) == (jenni, eyes)
        assert shown["viewpoint_basis"] == "none"
        jenni_items = await si.history(client, story, jenni)
        assert si.image_ids(jenni_items) == [shown["image_id"]]
        read = next(i for i in jenni_items if i.get("kind") == "image")
        assert (read["camera_pov"], read["viewpoint_basis"]) == (eyes, "none")
        assert si.image_ids(await si.history(client, story, bobby)) == []
        assert (await si.get(client, story, shown["image_id"], bobby)).status_code == 404

        absent = await si.upload(
            client,
            story,
            si.meta(
                {"slot_after_turn": 0},
                bobby,
                camera_pov={"kind": "over_the_shoulder", "character_id": ids["Lily"]},
            ),
        )
        assert absent.status_code == 422, absent.text
        assert absent.json()["error"]["code"] == "camera_pov_not_present"
        present = await si.placed(
            client,
            story,
            si.meta(
                {"slot_after_turn": 0},
                bobby,
                camera_pov={"kind": "over_the_shoulder", "character_id": ids["Jenni"]},
            ),
        )
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT i.audience_viewpoint, i.camera_pov, i.viewpoint_basis, "
                    "m.viewpoint_basis AS asset_basis FROM story_images i "
                    "JOIN media_assets m ON m.id = i.asset_id WHERE i.story_id = :s ORDER BY i.id"
                ),
                {"s": story},
            )
        ).all()
    assert [(r.audience_viewpoint, r.camera_pov) for r in rows] == [
        (jenni, eyes),
        (bobby, present["camera_pov"]),
    ]
    assert {(r.viewpoint_basis, r.asset_basis) for r in rows} == {("none", "none")}


class _GoneTarget(TestImageTarget):
    """The test target whose entry disappears during generation: promotion finds it gone."""

    async def promote(self, conn: Any, job: Any, outputs: Any) -> list[str]:
        raise image_jobs.TargetGone("gone")


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-11")
async def test_c_24__m8_limits(pg_url: str, tmp_path: Path) -> None:
    """FR-IMG-11's limits as amended by #262:

    - no story-scoped limit exists: ``image_limit_story_day`` and ``image_limit_turn`` in a
      story's settings are 422 ``validation_error`` (unknown keys, not ignored), and six images
      for one turn of one story are admitted while the deployment-day allowance lasts; the
      seventh is 409 ``image_limit_reached`` at the ``deployment`` scope;
    - a catalog request for four candidates with three remaining on its entry is refused whole
      at enqueue (409, ``scope: entry``), and no job is written;
    - a job whose retry would pass the limit ends ``cap_reached`` with its staged results
      deleted;
    - outputs discarded by a check, or as stale (``target_gone``), count against the limits.

    Complement asserted: the other entry's request is admitted, and with room left the retry
    runs (the counter's ``used`` counts every returned output)."""
    from tests.auth_support import bearer
    from tests.event_support import new_story
    from tests.image_limits_support import all_records, assert_clean, deployment, space, today
    from tests.image_support import catalog_origin

    write = {**bearer(), "X-Epistrel-Principal": "p-images"}
    async with space(pg_url, tmp_path, "c24_lim") as sp:
        story = await new_story(sp.db)
        for key in ("image_limit_story_day", "image_limit_turn"):
            response = await sp.client.patch(
                f"/stories/{story}/settings", json={key: 1}, headers=write
            )
            assert response.status_code == 422, (key, response.text)
            assert response.json()["error"]["code"] == "validation_error"

        stub = StubImageGenerator(script=[])
        with (
            registered(ADAPTER, stub),
            registered_target() as target,
            installed_checks(ScriptedChecks([MINORS], fail_closed=False)),
        ):
            await sp.configure()
            await sp.set(image_limit_deployment_day=6)
            for _ in range(6):
                await sp.insert(prepared(story, origin=_turn(story)))
            with pytest.raises(ImageLimitReached) as full:
                await sp.insert(prepared(story, origin=_turn(story)))
            assert (full.value.http_status, full.value.details["scope"]) == (409, "deployment")
            assert ERROR_STATUS["image_limit_reached"] == 409
            assert ERROR_STATUS["images_disabled"] == 422

            await sp.set(image_limit_deployment_day=200, image_limit_entry_day=7)
            entry, other = new_ulid(), new_ulid()
            await sp.insert(prepared(kind=ImageJobKind.CATALOG, origin=catalog_origin(entry), n=4))
            async with sp.db.connect() as conn:
                jobs_before = await conn.scalar(text("SELECT count(*) FROM image_jobs"))
            with pytest.raises(ImageLimitReached) as entry_full:
                await sp.insert(
                    prepared(kind=ImageJobKind.CATALOG, origin=catalog_origin(entry), n=4)
                )
            details = entry_full.value.details
            assert (details["scope"], details["limit"], details["requested"]) == ("entry", 7, 4)
            assert details["limit"] - details["used"] - details["reserved"] == 3
            async with sp.db.connect() as conn:
                assert await conn.scalar(text("SELECT count(*) FROM image_jobs")) == jobs_before
            await sp.insert(prepared(kind=ImageJobKind.CATALOG, origin=catalog_origin(other), n=4))

            # A retry that would pass the limit: the output is rejected, no unit remains.
            day = today()
            totals = await deployment(sp.db, day)
            await sp.set(image_limit_deployment_day=totals["used"] + totals["reserved"] + 1)
            capped = await sp.insert(prepared(story))
            stub.script.append(Outputs())
            assert (await sp.run(capped)) == {
                "image_job": "cap_reached",
                "reason": "image_limit_reached",
            }
            view = await sp.lib.get_image_job(CTX, capped)
            assert view.status_detail == {"scope": "deployment", "period": day}
            assert staged(sp.lib.media, capped) == []
            (rejected,) = [r for r in await all_records(sp.db) if r["job_id"] == capped]
            assert (rejected["used"], rejected["state"]) == (1, "settled")
            assert target.promoted == []

            # With room, the retry runs and promotes; both outputs count.
            await sp.set(image_limit_deployment_day=200)
            retried = await sp.insert(prepared(story))
            stub.script.extend([Outputs(), Outputs()])
            with installed_checks(ScriptedChecks([MINORS, PASS], fail_closed=False)):
                assert (await sp.run(retried))["image_job"] == "promoted"
            assert sum(r["used"] for r in await all_records(sp.db) if r["job_id"] == retried) == 2

        # Stale: the target is gone at promotion; its output still counts.
        with (
            registered(ADAPTER, stub),
            registered_target(_GoneTarget()),
            installed_checks(ScriptedChecks(fail_closed=False)),
        ):
            used = (await deployment(sp.db, day))["used"]
            gone = await sp.insert(prepared(story))
            stub.script.append(Outputs())
            assert (await sp.run(gone))["image_job"] == "target_gone"
            assert (await deployment(sp.db, day))["used"] == used + 1
        await assert_clean(sp.db)


def _turn(story_id: str) -> dict[str, Any]:
    from tests.image_support import story_origin

    return story_origin(story_id, turn=3)


@pytest.mark.integration
@pytest.mark.usefixtures("drained_embedding_jobs")
@pytest.mark.part("M8", "FR-IMG-9,10")
async def test_c_24__m8_consumer_prompt(image_settings: Settings, engine: AsyncEngine) -> None:
    """A story image generated from a consumer's prompt for Bobby: the adapter is sent the
    consumer's text as written (then the style and the guidance), with no visual identity, seed
    or reference image; the image reads ``source: generated_from_consumer_prompt``,
    ``viewpoint_basis: protected_term_match`` (its row and its asset), its asset depicts exactly
    the declared subjects, and its caption and alt text are stored as given. A ``prompt``
    without ``depicted_subjects`` is 422 ``subjects_required``; ``depicted_subjects`` without a
    ``prompt`` is 422 ``subjects_not_allowed``; an unknown field (``negative_prompt``) is 422
    ``validation_error``; none writes a job.

    Complement asserted: the built image at the same position reads ``generated`` /
    ``filtered_inputs``."""
    from tests import story_image_generation_support as sg

    async with sg.generation_app(image_settings, engine) as app:
        scene = await sg.whisper(app, engine)
        story, ids = scene.story_id, scene.ids
        bobby = sg.character(ids["Bobby"])
        at = {"turn": 1, "beat": 3}
        text_prompt = "A lamplit parlor; a woman leans toward a sailor."
        declared = [{"kind": "character", "id": ids["Jenni"]}]
        job = await sg.generated(
            app,
            story,
            position=at,
            audience_viewpoint=bobby,
            prompt=text_prompt,
            depicted_subjects=declared,
            caption="At the window",
            alt_text="Two people talking quietly",
        )
        [received] = app.generator.requests
        assert received.request.prompt.startswith(f"{text_prompt}\n\nVisual style: ")
        assert received.request.reference_images == ()
        assert job["params"]["seed"] is None
        [row] = await sg.story_image_rows(engine, story)
        assert (row["source"], row["viewpoint_basis"]) == (
            "generated_from_consumer_prompt",
            "protected_term_match",
        )
        assert (row["asset_source"], row["asset_basis"]) == (
            "generated_from_consumer_prompt",
            "protected_term_match",
        )
        assert row["asset_subjects"] == declared
        read = await app.client.get(
            f"/stories/{story}/images/{row['id']}", params={"viewpoint": bobby}, headers=READ
        )
        assert read.status_code == 200, read.text
        assert (read.json()["source"], read.json()["viewpoint_basis"]) == (
            "generated_from_consumer_prompt",
            "protected_term_match",
        )
        assert (read.json()["caption"], read.json()["alt_text"]) == (
            "At the window",
            "Two people talking quietly",
        )

        held = await sg.counts(engine, story)
        refusals = (
            ({"prompt": text_prompt}, "subjects_required", "body.depicted_subjects"),
            ({"depicted_subjects": declared}, "subjects_not_allowed", "body.depicted_subjects"),
            ({"negative_prompt": "blurry"}, "validation_error", "body.negative_prompt"),
        )
        for extra, code, field_name in refusals:
            refused = await sg.request(app, story, position=at, audience_viewpoint=bobby, **extra)
            assert refused.status_code == 422, refused.text
            assert refused.json()["error"]["code"] == code
            assert refused.json()["error"]["details"]["field"] == field_name
        assert await sg.counts(engine, story) == held

        await sg.generated(app, story, position=at, audience_viewpoint=bobby)
        built = (await sg.story_image_rows(engine, story))[-1]
        assert (built["source"], built["viewpoint_basis"]) == ("generated", "filtered_inputs")
