"""#1020 (FR-IMG-14, ADR-215): a subject image request's ``reference_image_ids``, as units.

The field takes 1 to ``MAX_REFERENCE_IMAGES`` (4) strings (``[]`` and five are 422 at
``body.reference_image_ids``; ``null`` means omitted). ``check_references`` judges each id
against the rows its one query found (a fake connection here): a duplicate, an id the query did
not find, a declaration without a true age and a different true age are refused per index in
that precedence, one ``errors[]`` entry each; the subject check runs only when every id passed,
and names the first id whose subject differs. ``subject_view`` carries the ids on the subject,
and ``image_prompt``'s payload never does. The end-to-end request is
``tests/integration/test_i_30_m9.py``. Unit level: no part marker."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx2 as httpx
import pytest
from fastapi import FastAPI

from epistrel.api.errors import install
from epistrel.core.errors import InvalidRequest
from epistrel.orchestrator.images.port import MAX_REFERENCE_IMAGES
from epistrel.orchestrator.images.prompt_builder import prompt_inputs
from epistrel.orchestrator.images.targets.subject import (
    AGE_MISMATCH,
    AGE_UNKNOWN,
    NOT_SUBJECT_IMAGE,
    REFERENCE_DUPLICATE,
    SUBJECT_MISMATCH,
    SubjectImageRequest,
    SubjectReferences,
    check_references,
    job_origin,
    subject_view,
)

AGE = 34
SUBJECT = {"description": "Tall woman in a green coat", "true_age": AGE, "label": "W"}
A, B, C, D = (f"01REF{n}".ljust(26, "0") for n in "ABCD")
S1, S2 = "01SUBJECTONE0000000000000A", "01SUBJECTTWO0000000000000B"


def request(**fields: Any) -> SubjectImageRequest:
    return SubjectImageRequest.model_validate({"subject": SUBJECT, **fields})


def app() -> FastAPI:
    found = FastAPI()
    install(found)

    @found.post("/subject-images")
    async def take(body: SubjectImageRequest) -> dict[str, Any]:
        return {"reference_image_ids": body.reference_image_ids}

    return found


async def post(payload: dict[str, Any]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.post("/subject-images", json=payload)


class FakeConn:
    """Answers ``check_references``'s one query with the given ``{id: depicted_subjects}``."""

    def __init__(self, rows: dict[str, Any]) -> None:
        self.rows = rows
        self.queries = 0

    async def execute(self, _statement: object) -> list[SimpleNamespace]:
        self.queries += 1
        return [SimpleNamespace(id=k, depicted_subjects=v) for k, v in self.rows.items()]


def declared(subject_id: str = S1, age: int | None = AGE) -> list[dict[str, Any]]:
    entry: dict[str, Any] = {"kind": "subject", "id": subject_id, "label": "W"}
    if age is not None:
        entry["true_age"] = age
    return [entry]


async def refused(ids: list[str], rows: dict[str, Any], **fields: Any) -> list[tuple[str, str]]:
    with pytest.raises(InvalidRequest) as caught:
        await check_references(FakeConn(rows), request(reference_image_ids=ids, **fields), "p")  # type: ignore[arg-type]
    details = caught.value.details
    found = [(str(e["field"]), str(e["type"])) for e in details["errors"]]
    assert (details["field"], details["reason"]) == found[0]
    return found


def at(index: int, reason: str) -> tuple[str, str]:
    return (f"body.reference_image_ids.{index}", reason)


# -- the field --------------------------------------------------------------------------------


async def test_one_to_four_ids_validate() -> None:
    assert MAX_REFERENCE_IMAGES == 4
    for ids in ([A], [A, B, C, D]):
        answered = await post({"subject": SUBJECT, "reference_image_ids": ids})
        assert answered.status_code == 200, answered.text
        assert answered.json() == {"reference_image_ids": ids}


@pytest.mark.parametrize("payload", [{}, {"reference_image_ids": None}])
async def test_omitted_or_null_is_none(payload: dict[str, Any]) -> None:
    answered = await post({"subject": SUBJECT, **payload})
    assert answered.status_code == 200, answered.text
    assert answered.json() == {"reference_image_ids": None}


@pytest.mark.parametrize(
    ("ids", "kind"),
    [([], "too_short"), ([A, B, C, D, A], "too_long"), ([A, B, C, D, "01E"], "too_long")],
)
async def test_zero_or_five_ids_are_refused(ids: list[str], kind: str) -> None:
    answered = await post({"subject": SUBJECT, "reference_image_ids": ids})
    assert answered.status_code == 422, answered.text
    error = answered.json()["error"]
    assert (error["code"], error["details"]["field"]) == (
        "validation_error",
        "body.reference_image_ids",
    )
    assert error["details"]["errors"][0]["type"] == kind


@pytest.mark.parametrize("ids", [A, [7], [None]])
async def test_a_non_list_or_non_string_is_refused(ids: object) -> None:
    answered = await post({"subject": SUBJECT, "reference_image_ids": ids})
    assert answered.status_code == 422, answered.text
    assert answered.json()["error"]["details"]["field"].startswith("body.reference_image_ids")


# -- check_references -------------------------------------------------------------------------


async def test_no_references_reads_nothing() -> None:
    conn = FakeConn({})
    assert await check_references(conn, request(), "p") is None  # type: ignore[arg-type]
    assert conn.queries == 0


async def test_valid_references_keep_order_and_subject() -> None:
    rows = {A: declared(), B: declared()}
    found = await check_references(FakeConn(rows), request(reference_image_ids=[B, A]), "p")  # type: ignore[arg-type]
    assert found == SubjectReferences((B, A), S1)


async def test_a_duplicate_is_refused_at_its_index() -> None:
    rows = {A: declared(), B: declared()}
    assert await refused([A, B, A], rows) == [at(2, REFERENCE_DUPLICATE)]


async def test_every_failing_index_is_reported_in_precedence() -> None:
    """Per id: duplicate, then not found, then no true age, then another true age. A duplicate
    of a refused id is still the duplicate."""
    rows = {A: declared(), B: declared(age=None), C: declared(age=AGE + 1)}
    assert await refused([D, B, C, D], rows) == [
        at(0, NOT_SUBJECT_IMAGE),
        at(1, AGE_UNKNOWN),
        at(2, AGE_MISMATCH),
        at(3, REFERENCE_DUPLICATE),
    ]


@pytest.mark.parametrize("depicted", [[], None, [{"kind": "subject"}], ["x"]])
async def test_a_row_with_no_subject_declaration_is_not_a_subject_image(depicted: Any) -> None:
    assert await refused([A], {A: depicted}) == [at(0, NOT_SUBJECT_IMAGE)]


async def test_a_boolean_age_is_unknown() -> None:
    assert await refused([A], {A: declared(age=True)}) == [at(0, AGE_UNKNOWN)]


async def test_the_request_age_is_compared() -> None:
    rows = {A: declared(age=20)}
    assert await refused([A], rows) == [at(0, AGE_MISMATCH)]
    young = {**SUBJECT, "true_age": 20}
    found = await check_references(
        FakeConn(rows),  # type: ignore[arg-type]
        SubjectImageRequest.model_validate({"subject": young, "reference_image_ids": [A]}),
        "p",
    )
    assert found == SubjectReferences((A,), S1)


async def test_a_minor_matching_reference_is_not_refused() -> None:
    minor = {**SUBJECT, "true_age": 15}
    found = await check_references(
        FakeConn({A: declared(age=15)}),  # type: ignore[arg-type]
        SubjectImageRequest.model_validate({"subject": minor, "reference_image_ids": [A]}),
        "p",
    )
    assert found == SubjectReferences((A,), S1)


async def test_a_subject_mismatch_names_the_first_differing_index() -> None:
    rows = {A: declared(), B: declared(), C: declared(S2), D: declared(S2)}
    assert await refused([A, B, C, D], rows) == [at(2, SUBJECT_MISMATCH)]


async def test_the_subject_check_waits_for_every_id() -> None:
    rows = {A: declared(), C: declared(S2)}
    assert await refused([A, C, B], rows) == [at(2, NOT_SUBJECT_IMAGE)]


# -- the view and the origin ------------------------------------------------------------------


def test_the_view_carries_the_ids_and_the_prompt_payload_does_not() -> None:
    view = subject_view(request(reference_image_ids=[B, A]), "All ages.")
    [perceived] = view.subjects
    assert perceived.reference_image_ids == (B, A)
    assert (perceived.seed, perceived.base_appearance_prompt) == (None, None)
    payload = prompt_inputs(view).model_dump_json()
    assert A not in payload
    assert B not in payload
    assert subject_view(request(), "All ages.").subjects[0].reference_image_ids == ()


def test_the_origin_records_the_ids() -> None:
    assert job_origin(S1, request(reference_image_ids=[A]))["reference_image_ids"] == [A]
    assert job_origin(S1, request())["reference_image_ids"] == []
