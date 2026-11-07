"""I-24's M8 parts: the image safety path's two data paths and its payloads (FR-IMG-12,13; #257).

The prompt check runs for real (``safety_checks.PromptChecks``) with scripted output checks,
the scripted image adapter (``tests.image_support.StubImageGenerator``) and the test target, so
the safety path is proven before #259's prompt builder and #263's story target exist: a prompt
the builder would write is given with its ``depicted`` subjects, a consumer's with its own
declaration. The cast is the whisper scene's (``tests.image_safety_support``); the stub
guardrail model flags the fixture's sexual phrases only in a check that lists subjects, and the
figure classification is scripted by its template.

Every I-24 M8 part lives in this file (the traceability gate takes one file per test ID and
milestone): #254's ``test_i_24__m8_data_flow_rows`` moved here from ``tests/test_images_port.py``
with #257's parts; #258 adds the output check's (``_checker_true_ages`` through the default
checks, ``_upload_check_payload``), and #259/#263 add theirs.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.errors import GuardrailViolation
from epistrel.core.tables.images import ImageJobKind
from epistrel.engine import Engine
from epistrel.orchestrator.images import jobs as image_jobs
from epistrel.orchestrator.images.port import ImageRequest
from epistrel.orchestrator.images.safety_checks import UNRESOLVED_FIGURE
from epistrel.orchestrator.models import registry
from epistrel.orchestrator.models.registry import (
    FIGURES_TEMPLATE,
    HOSTED_SAFETY_RETENTION,
    ContentCategory,
    field_categories,
)
from epistrel.orchestrator.policies.check import MINOR_INVOLVED
from epistrel.orchestrator.policies.seeds.policies import MINORS_POLICY_ID
from epistrel.testing.images import TestImageTarget, registered_target
from epistrel.tools import model_guide
from tests import perception_support as ps
from tests.image_job_support import CTX, PRINCIPAL, configure, image_engine, image_settings_for
from tests.image_safety_support import (
    BOBBY,
    JENNI,
    LILY,
    LILY_AGE,
    MARCUS,
    MARCUS_AGE,
    STORY_TEXT,
    WREN,
    WREN_AGE,
    figures,
    jobs_of,
    origin,
    payload,
    prompt_checked,
    recognise_sexual,
    refusals,
    safety_calls,
    subject,
    whisper_story,
    with_rooms,
)
from tests.image_support import Outputs, StubImageChecker, StubImageGenerator, registered
from tests.media_support import uploaded
from tests.minors_support import SUBJECTS_HEADING
from tests.model_support import admin_client

GUIDE = Path(__file__).resolve().parents[2] / "docs" / "models.md"

ADAPTER = "stub_sync"
CAST = (BOBBY, JENNI, MARCUS, LILY, WREN)
POLICY_TEXT = "no sexual content involving, or in the presence of, anyone under 18"
MARCUS_PROMPT = "Marcus, a man in his mid thirties, mends a net by the stove."
LILY_PROMPT = "Lily, a woman in her twenties, lounges in lingerie on the bed."
JENNI_PROMPT = "Jenni, a woman in her twenties, lounges in lingerie on the bed."
ADULTS_TOGETHER = "Jenni and Marcus make love by the stove."
GIRL_PROMPT = "A young girl in lingerie on the bed."
WOMAN_PROMPT = "A middle-aged woman in lingerie on the bed."


@pytest.fixture
def image_settings(pg_url: str, tmp_path: Path) -> Settings:
    return image_settings_for(pg_url, tmp_path)


@pytest.fixture
async def lib(image_settings: Settings, engine: AsyncEngine) -> AsyncIterator[Engine]:
    async with image_engine(image_settings, engine) as epistrel:
        try:
            yield epistrel
        finally:
            await ps.forget_perception(engine)


@pytest.fixture
def target() -> Iterator[TestImageTarget]:
    with registered_target() as found:
        yield found


async def _enqueue(
    lib: Engine, story_id: str, prompt: str, *, target: dict[str, Any] | None = None, **request: Any
) -> str:
    queued = await image_jobs.enqueue_image_job(
        lib.image_runtime(),
        ImageJobKind.STORY,
        target or {},
        await origin(lib.db, story_id),
        {"prompt": prompt, **request},
        None,
        PRINCIPAL,
    )
    return queued.job_id


async def _refused(lib: Engine, story_id: str, prompt: str, **request: Any) -> GuardrailViolation:
    before = await jobs_of(lib.db, story_id)
    with pytest.raises(GuardrailViolation) as refused:
        await _enqueue(lib, story_id, prompt, **request)
    assert await jobs_of(lib.db, story_id) == before, "a refused prompt created a job"
    return refused.value


def _body(error: GuardrailViolation) -> str:
    return json.dumps({"message": error.message, "details": error.details})


def _redacted(error: GuardrailViolation, *withheld: str) -> None:
    """The refusal names the minors policy and says a minor is involved or present, never who
    or how old."""
    assert error.code == "guardrail_violation"
    assert error.http_status == 422
    assert [v["policy_id"] for v in error.details["violations"]] == [MINORS_POLICY_ID]
    assert MINOR_INVOLVED in error.message
    body = _body(error)
    for value in withheld:
        assert value not in body, value


async def _safety_context(lib: Engine, job_id: str) -> dict[str, Any]:
    async with lib.db.connect() as conn:
        found = await conn.scalar(
            text("SELECT safety_context FROM image_jobs WHERE id = :j"), {"j": job_id}
        )
    assert isinstance(found, dict)
    return found


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-12,13")
async def test_i_24__m8_two_paths(
    lib: Engine, image_settings: Settings, engine: AsyncEngine, target: TestImageTarget
) -> None:
    """The safety path knows true ages; the generation path does not (FR-IMG-12).

    1. Marcus is 34: the adapter's request carries the band the prompt uses ("mid thirties")
       and never 34, its type has no age field, and the job's safety context holds 34.
    2. Lily is 17 and looks older: a sexual request involving her is refused by the minors
       policy, and no job row is created; a consumer prompt naming "Lily" with an empty
       subject list is resolved to her and refused too. Complement: the same prompt about
       Jenni (29), with Lily in the barn, passes.
    3. "A young girl" in a sexual context is classified minor and refused; "a middle-aged
       woman" in the same context is classified adult and passes.
    4. A sexual request between two adults is refused while Wren (16) is authoritatively in
       the room. Complement: with Wren in the barn and only in the request's own roster, it is
       not refused on presence.
    5. Payloads (FR-IMG-13): the stub ``guardrail_check`` calls carry the prompt, the policy's
       text and labels with true ages, and no story text and no name of a character absent
       from the prompt; the refusals say "a minor is involved or present" and name neither Lily
       nor her age, and each wrote an ``image_prompt_rejected`` audit row."""
    stub = StubImageGenerator(script=[Outputs()] * 4)
    guard = lib.models.stub
    story, ids = await whisper_story(engine)
    places = await with_rooms(engine, story, ids)
    with registered(ADAPTER, stub), prompt_checked():
        await configure(lib, image_settings, ADAPTER)
        guard.reset()
        recognise_sexual(guard, "lingerie", "make love")

        # 1. Marcus, 34, described by the band the prompt uses.
        marcus = await _enqueue(
            lib,
            story,
            MARCUS_PROMPT,
            depicted_subjects=[subject(ids[MARCUS], "a man in his mid thirties", "weathered")],
        )
        context = await _safety_context(lib, marcus)
        by_id = {s["character_id"]: s for s in context["subjects"]}
        assert by_id[ids[MARCUS]]["true_age"] == MARCUS_AGE
        assert by_id[ids[MARCUS]]["label"] == "subject_1"
        assert by_id[ids[MARCUS]]["source"] == "depicted"
        assert {ids[LILY], ids[WREN]}.isdisjoint(by_id), "the barn is not present"
        assert (await image_jobs.run_image_job(lib.image_runtime(), marcus, 0))[
            "image_job"
        ] == "promoted"
        (sent,) = stub.requests
        assert "mid thirties" in sent.request.prompt
        assert str(MARCUS_AGE) not in sent.request.model_dump_json(exclude={"seed"})
        assert not {"age", "true_age", "subjects", "safety_context"} & set(
            ImageRequest.model_fields
        )
        view = await lib.get_image_job(CTX, marcus)
        shown = json.dumps(dataclasses.asdict(view), default=str)
        assert "safety_context" not in shown
        assert str(MARCUS_AGE) not in shown

        # 2. Lily, 17, looks older: refused by her true age, declared or only named.
        declared = await _refused(
            lib,
            story,
            LILY_PROMPT,
            depicted_subjects=[subject(ids[LILY], "a woman in her twenties")],
        )
        _redacted(declared, LILY, str(LILY_AGE))
        named = await _refused(lib, story, LILY_PROMPT, depicted_subjects=[])
        _redacted(named, LILY, str(LILY_AGE))
        lily_checks = [payload(c) for c in safety_calls(guard) if LILY_PROMPT in payload(c)]
        assert any(f"subject_1: age {LILY_AGE}" in p for p in lily_checks), lily_checks
        assert await _enqueue(lib, story, JENNI_PROMPT, depicted_subjects=[])

        # 4. Presence: the adults together pass with Wren in the barn, even when the request's
        # own roster names him; with Wren in the cabin they are refused.
        assert await _enqueue(
            lib, story, ADULTS_TOGETHER, target={"present_character_ids": [ids[WREN]]}
        )
        await ps.put(engine, story, ids[WREN], places["cabin"])
        present = await _refused(lib, story, ADULTS_TOGETHER)
        _redacted(present, WREN, str(WREN_AGE))
        near = [payload(c) for c in safety_calls(guard) if ADULTS_TOGETHER in payload(c)]
        assert any(f"age {WREN_AGE}" in p and SUBJECTS_HEADING in p for p in near)

        # 5. Payloads: what every safety call carried, and what it never did.
        calls = safety_calls(guard)
        assert calls
        for call in calls:
            sent_text = payload(call)
            assert STORY_TEXT not in sent_text
            checked = call.messages[-1].content
            for name in CAST:
                if name in sent_text:
                    assert name in checked, f"{name} reached a call whose texts lack it"
        policy_calls = [payload(c) for c in calls if SUBJECTS_HEADING in payload(c)]
        assert policy_calls
        assert all(POLICY_TEXT in p for p in policy_calls)
        assert all("Prompt: " in p for p in policy_calls)
        rows = await refusals(engine, story)
        assert len(rows) == 3
        assert all(r.policy_ids == [MINORS_POLICY_ID] for r in rows)
        assert all(r.principal == PRINCIPAL for r in rows)
        assert all(r.details["target_kind"] == "story" for r in rows)
        assert all(LILY not in json.dumps(r.details) for r in rows)

    # 3. Figures, in an adults-only story under Lite (everyone present, nobody a minor).
    adults, _ = await whisper_story(engine, minors=False)
    with registered(ADAPTER, StubImageGenerator(script=[])), prompt_checked():
        guard.reset()
        recognise_sexual(guard, "lingerie")
        figures(guard, "A young girl", ("a young girl", "minor"))
        figures(guard, "A middle-aged woman", ("a middle-aged woman", "adult"))
        girl = await _refused(lib, adults, GIRL_PROMPT)
        _redacted(girl)
        assert await _enqueue(lib, adults, WOMAN_PROMPT)
        woman = [payload(c) for c in safety_calls(guard) if WOMAN_PROMPT in payload(c)]
        assert len(woman) == 1, "an adult figure needs no minors check"
        girl_checks = [payload(c) for c in safety_calls(guard) if GIRL_PROMPT in payload(c)]
        assert any("figure_1: minor figure; a young girl" in p for p in girl_checks)


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-12")
async def test_i_24__m8_figure_classification_fail_closed(
    lib: Engine, image_settings: Settings, engine: AsyncEngine, target: TestImageTarget
) -> None:
    """The figure classification fails (the model errors out after its retries, or answers
    nothing valid): the unclassified figure counts as indeterminate, so as a minor, and a sexual
    request is refused.

    Complement asserted: with the classification answering an adult couple, the same request
    passes, and so does a non-sexual one while the classifier is failing (indeterminate makes
    the minors policy apply; it is the policy check that refuses)."""
    stub = StubImageGenerator(script=[])
    guard = lib.models.stub
    story, _ = await whisper_story(engine, minors=False)
    prompt = "Two strangers make love in the hayloft."
    with registered(ADAPTER, stub), prompt_checked():
        await configure(lib, image_settings, ADAPTER)
        guard.reset()
        recognise_sexual(guard, "make love")
        guard.fault(FIGURES_TEMPLATE, "http_500", 50)
        errored = await _refused(lib, story, prompt)
        _redacted(errored)
        figure_checks = [p for p in map(payload, safety_calls(guard)) if SUBJECTS_HEADING in p]
        assert any("figure_1: indeterminate figure, treated as a minor" in p for p in figure_checks)
        assert UNRESOLVED_FIGURE.classification == "indeterminate"
        assert await _enqueue(lib, story, "Two strangers share bread in the hayloft.")

        guard.reset()
        recognise_sexual(guard, "make love")
        guard.script(FIGURES_TEMPLATE, ["not json at all"] * 20)
        _redacted(await _refused(lib, story, prompt))

        guard.reset()
        recognise_sexual(guard, "make love")
        figures(guard, "Two strangers", ("a stranger", "adult"), ("another stranger", "adult"))
        assert await _enqueue(lib, story, prompt)
    assert stub.requests == []


def _section(guide: str, name: str) -> str:
    return guide.split(f"### `{name}`", 1)[1].split("\n### ", 1)[0].split("\n## ", 1)[0]


def _rows(section: str) -> list[str]:
    receives = section.split("**Receives**", 1)[1].split("**Never receives:**", 1)[0]
    return [line.split("|")[1].strip().strip("`") for line in receives.splitlines()[4:] if line]


@pytest.mark.part("M8", "FR-IMG-13")
def test_i_24__m8_data_flow_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The generated guide's rows for ``guardrail_check`` and ``image_check`` list exactly the
    inputs their registry schemas declare (subject labels, true ages and the prompt's visual
    descriptors in both; the image, prompt, caption and alt text in ``image_check``'s) and
    carry the hosted-provider retention sentence verbatim; the image adapter has a row too,
    listing the prompt, its parameters and reference images and never the safety context.

    Complement, run first: with the sentence removed from the guide's row template, rendering
    fails and ``--check`` exits 1, so the sentence cannot silently drop out."""
    monkeypatch.setattr(model_guide, "SAFETY_RETENTION_LINE", "- **Hosted providers:** see terms")
    with pytest.raises(model_guide.GuideError, match="retention sentence"):
        model_guide.render()
    assert model_guide.main(["--check"]) == 1
    monkeypatch.undo()

    guide = GUIDE.read_text(encoding="utf-8")
    assert guide == model_guide.render()
    assert model_guide.main(["--check"]) == 0
    for name in registry.SAFETY_PURPOSES:
        section = _section(guide, name)
        purpose = registry.DELIVERED[name]
        assert _rows(section) == [
            path
            for path in field_categories(purpose.input_schema)
            if not any(
                other.startswith(f"{path}.") for other in field_categories(purpose.input_schema)
            )
        ]
        assert HOSTED_SAFETY_RETENTION in section
    check = _section(guide, "image_check")
    for row in (
        "| `image` | `image` |",
        "| `prompt` | `image_prompt` |",
        "| `caption` | `caption_alt_text` |",
        "| `alt_text` | `caption_alt_text` |",
        "| `subjects.label` | `subject_label` |",
        "| `subjects.true_age` | `subject_true_age` |",
        "| `subjects.visual_descriptors` | `visual_descriptor` |",
        "| `policies.text` | `policy_text` |",
    ):
        assert row in check, row
    assert "names the prompt lacks" in check
    assert "none: its own `image_check` row" in check
    guardrail = _section(guide, "guardrail_check")
    assert "| `image_subjects.true_age` | `subject_true_age` |" in guardrail
    assert "| `image_subjects.visual_descriptors` | `visual_descriptor` |" in guardrail
    adapter = _section(guide, "image_generation")
    assert "| `prompt` | `image_prompt` |" in adapter
    assert "| `reference_images` | `reference_image` |" in adapter
    never = adapter.split("**Never receives:**", 1)[1]
    assert "`subject_true_age`" in never
    assert "`subject_label`" in never
    assert "go to the image provider" in adapter
    prompt = _section(guide, "image_prompt")
    assert "`subject_true_age`" in prompt.split("**Never receives:**", 1)[1]
    assert ContentCategory.SUBJECT_TRUE_AGE not in registry.schema_categories(
        registry.DELIVERED["image_prompt"].input_schema
    )
    assert registry.SAFETY_PURPOSES == ("guardrail_check", "image_check")


def _checker_input(call: Any) -> tuple[str, tuple[str, ...]]:
    """What one ``image_check`` call sent: its messages' text, and the images it carried."""
    images = tuple(url for message in call.messages for url in message.images)
    return payload(call), images


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-12,13")
async def test_i_24__m8_checker_true_ages(
    lib: Engine, image_settings: Settings, engine: AsyncEngine, target: TestImageTarget
) -> None:
    """True ages reach the image checker (#258): an ordinary image of Marcus (34), with Lily
    (17) present under Lite, passes the prompt check, is generated by the stub generator and
    reaches the stub ``image_check`` model through the default output check. The checker's
    input carries the image as a ``data:`` URL part, the prompt, the minors policy's text and
    the resolved subjects as labels with their true ages, 34 and 17, and no story text; the
    generator's request carries only the band the prompt uses. The verdict is recorded per
    policy on the promoted asset.

    Complement asserted: the generator's request holds neither age, and the checker's text
    holds no name the prompt lacks (Lily is a label only)."""
    stub = StubImageGenerator(script=[Outputs()])
    story, ids = await whisper_story(engine)
    vision = StubImageChecker(lib.models.stub)
    with registered(ADAPTER, stub):
        await configure(lib, image_settings, ADAPTER)
        lib.models.stub.reset()
        vision.answer({MINORS_POLICY_ID: "pass"})
        job = await _enqueue(
            lib,
            story,
            MARCUS_PROMPT,
            depicted_subjects=[subject(ids[MARCUS], "a man in his mid thirties")],
        )
        assert (await image_jobs.run_image_job(lib.image_runtime(), job, 0))[
            "image_job"
        ] == "promoted"
    (sent,) = stub.requests
    assert "mid thirties" in sent.request.prompt
    assert str(MARCUS_AGE) not in sent.request.model_dump_json(exclude={"seed"})
    assert str(LILY_AGE) not in sent.request.model_dump_json(exclude={"seed"})
    (call,) = vision.calls
    text_sent, images = _checker_input(call)
    assert len(images) == 1
    assert images[0].startswith("data:image/png;base64,")
    assert f"Prompt: {MARCUS_PROMPT}" in text_sent
    assert POLICY_TEXT in text_sent
    assert f": age {MARCUS_AGE}" in text_sent
    assert f": age {LILY_AGE}" in text_sent
    assert STORY_TEXT not in text_sent
    assert LILY not in text_sent
    (asset_id,) = (await lib.get_image_job(CTX, job)).result_ids
    asset = await lib.get_media(CTX, asset_id)
    assert asset.check_status == "passed"
    assert asset.check is not None
    assert [(p.policy_id, p.result, p.certified) for p in asset.check.policies] == [
        (MINORS_POLICY_ID, "pass", False)
    ]


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-13")
async def test_i_24__m8_upload_check_payload(
    lib: Engine, image_settings: Settings, engine: AsyncEngine
) -> None:
    """An upload checked by a configured checker is sent the same way (#258): the stub
    ``image_check`` model's recorded input holds the image (a ``data:`` URL part), the caption,
    the minors policy's text and the declared subjects as labels with their true ages (Lily 17,
    Marcus 34, both story characters), and no story text and no name the caption lacks.

    Complement asserted: Marcus, declared but not named in the caption, is a label only; the
    upload is stored ``passed``."""
    _, ids = await whisper_story(engine)
    caption = "Lily at the window, late afternoon."
    vision = StubImageChecker(lib.models.stub)
    async with admin_client(image_settings, lib) as client:
        with registered(ADAPTER, StubImageGenerator(script=[])):
            await configure(lib, image_settings, ADAPTER)
        lib.models.stub.reset()
        vision.answer({MINORS_POLICY_ID: "pass"})
        body = await uploaded(
            client,
            caption=caption,
            depicted_subjects=[
                {"kind": "character", "id": ids[LILY]},
                {"kind": "character", "id": ids[MARCUS]},
            ],
        )
    assert body["check_status"] == "passed"
    (call,) = vision.calls
    text_sent, images = _checker_input(call)
    assert len(images) == 1
    assert images[0].startswith("data:image/png;base64,")
    assert f"Caption: {caption}" in text_sent
    assert POLICY_TEXT in text_sent
    assert f"subject_1: age {LILY_AGE}" in text_sent
    assert f"subject_2: age {MARCUS_AGE}" in text_sent
    assert STORY_TEXT not in text_sent
    for name in CAST:
        if name != LILY:
            assert name not in text_sent, name
