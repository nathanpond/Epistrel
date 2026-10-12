"""The catalog's pure parts (#103): RFC 7386 merge patches, the draft document and its
validation, the publish report, the owner rule, the pause points and the ports' defaults."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from epistrel.core.catalog.characters import (
    CharacterCreate,
    CharacterDraft,
    CharacterPatch,
    PublishReport,
    ReportEntry,
    merge_patch,
    publish_report,
)
from epistrel.core.ports import (
    NoJobs,
    NoModel,
    NoPolicies,
    PassingGate,
    Ports,
    PortText,
)
from epistrel.testing import pauses

# RFC 7386 appendix A: (target, patch, result).
RFC_7386_EXAMPLES: list[tuple[Any, Any, Any]] = [
    ({"a": "b"}, {"a": "c"}, {"a": "c"}),
    ({"a": "b"}, {"b": "c"}, {"a": "b", "b": "c"}),
    ({"a": "b"}, {"a": None}, {}),
    ({"a": "b", "b": "c"}, {"a": None}, {"b": "c"}),
    ({"a": ["b"]}, {"a": "c"}, {"a": "c"}),
    ({"a": "c"}, {"a": ["b"]}, {"a": ["b"]}),
    ({"a": {"b": "c"}}, {"a": {"b": "d", "c": None}}, {"a": {"b": "d"}}),
    ({"a": [{"b": "c"}]}, {"a": [1]}, {"a": [1]}),
    (["a", "b"], ["c", "d"], ["c", "d"]),
    ({"a": "b"}, ["c"], ["c"]),
    ({"a": "foo"}, None, None),
    ({"a": "foo"}, "bar", "bar"),
    ({"e": None}, {"a": 1}, {"e": None, "a": 1}),
    ([1, 2], {"a": "b", "c": None}, {"a": "b"}),
    ({}, {"a": {"bb": {"ccc": None}}}, {"a": {"bb": {}}}),
]


@pytest.mark.parametrize(("target", "patch", "result"), RFC_7386_EXAMPLES)
def test_merge_patch_follows_rfc_7386(target: Any, patch: Any, result: Any) -> None:
    before = repr(target)
    assert merge_patch(target, patch) == result
    assert repr(target) == before  # the target is not changed in place


def test_draft_document_leaves_out_what_is_not_set() -> None:
    """The stored draft is JSON with unset fields left out; a creation's ``owner`` is not part
    of it, and the owner follows the principal rule."""
    assert CharacterDraft().document() == {"definition": {}}
    create = CharacterCreate(name="  Mara  ", definition={"personality": "Wry."}, owner=" p-1 ")
    assert create.document() == {"name": "Mara", "definition": {"personality": "Wry."}}
    assert create.owner == "p-1"
    for owner in ("system", "", "\t", "a\nb", "x" * 257):
        with pytest.raises(ValidationError):
            CharacterCreate(owner=owner)
    with pytest.raises(ValidationError):
        CharacterDraft.model_validate({"name": "Mara", "secret_past": "x"})


def test_patch_document_keeps_nulls_and_drops_the_revision() -> None:
    patch = CharacterPatch.model_validate(
        {"draft_revision": 3, "public_description": None, "definition": {"personality": None}}
    )
    assert patch.document() == {"public_description": None, "definition": {"personality": None}}
    assert CharacterPatch(draft_revision=1).document() == {}
    for revision in (0, 2**31):
        with pytest.raises(ValidationError):
            CharacterPatch(draft_revision=revision)


def test_publish_report() -> None:
    """A valid, named draft has no errors; a nameless one names ``name``; an invalid one names
    each failing field."""
    assert publish_report({"name": "Mara", "definition": {}}) == PublishReport()
    assert publish_report({"definition": {}}).errors == (
        ReportEntry("field_required", "name", "a published character needs a name"),
    )
    report = publish_report({"name": "Mara", "definition": {"personality": 7}, "mood": "x"})
    assert sorted((e.code, e.field) for e in report.errors) == [
        ("extra_forbidden", "mood"),
        ("string_type", "definition.personality"),
    ]
    assert report.as_json()["warnings"] == []


async def test_pause_points_hold_and_release() -> None:
    """A held point stops every call that reaches it until the release; an unheld point is a
    no-op; unknown and doubly held points are refused."""
    await pauses.pause(pauses.CATALOG_SAVE_BEFORE_CAS)  # not held: returns at once
    reached: list[str] = []

    async def call(name: str) -> None:
        await pauses.pause(pauses.CATALOG_SAVE_BEFORE_CAS)
        reached.append(name)

    with pauses.held(pauses.CATALOG_SAVE_BEFORE_CAS) as hold:
        tasks = [asyncio.ensure_future(call(n)) for n in ("a", "b")]
        await hold.arrived(2)
        assert (hold.arrivals, reached) == (2, [])
        hold.release()
        await asyncio.gather(*tasks)
        await pauses.pause(pauses.CATALOG_SAVE_BEFORE_CAS)  # released: passes straight on
    assert sorted(reached) == ["a", "b"]
    with pytest.raises(ValueError, match="unknown pause point"):
        pauses.held("catalog.nowhere").__enter__()
    with (
        pauses.held(pauses.CATALOG_PUBLISH_LOCKED),
        pytest.raises(RuntimeError, match="already held"),
    ):
        pauses.held(pauses.CATALOG_PUBLISH_LOCKED).__enter__()

    with pauses.held(pauses.CATALOG_PUBLISH_LOCKED) as hold:
        waiting = asyncio.ensure_future(pauses.pause(pauses.CATALOG_PUBLISH_LOCKED))
        await hold.arrived(1)
        assert not waiting.done()
    await asyncio.wait_for(waiting, 1)  # leaving the block released it


async def test_ports_default_to_no_ops() -> None:
    """Until a story wires the Orchestrator's implementation, every port does nothing: the
    gate passes, no policy hits, no model answers and no job starts."""
    ports = Ports()
    texts = [PortText("definition.personality", "anything at all")]
    assert isinstance(ports.input_gate, PassingGate)
    await ports.input_gate.enforce(texts, entry_point="publish", principal="p")  # passes
    assert isinstance(ports.policy_evaluator, NoPolicies)
    assert await ports.policy_evaluator.evaluate(texts) == ()
    assert isinstance(ports.model_caller, NoModel)
    assert await ports.model_caller.complete("generation", "prompt") is None
    assert isinstance(ports.job_starter, NoJobs)
    assert await NoJobs().start(None, "kind", {}) is None  # type: ignore[arg-type]
