"""C-14 (M4) (docs/04): the guarantee-boundary documentation (FR-VERIFY-15, FR-MODEL-2; #132).

- ``test_c_14__m4``: ``docs/guarantees.md`` is what ``GUARANTEES`` renders. It states the
  boundary, lists FR-VERIFY-15's deterministic and model-dependent steps, and lists every model
  purpose delivered so far as a model-dependent step with its NFR-QUAL metric, or as
  ``unmeasured``. A delivered purpose without a row fails the check (the complement).
- ``test_c_14__no_output_called_verified``: no user-facing document (README, the generated
  documents under ``docs/``, the OpenAPI descriptions and summaries) calls an output "fully
  verified", or "verified" at all outside the allowed phrases. This extends #57's "finished,
  never verified" test. The design documents 01 to 05 are not user-facing and are left out.

C-14 (M4) has exactly this file (traceability allows one file per test ID and milestone).
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterator
from typing import Any

import pytest

from epistrel.core.profiles.guarantees import (
    BOUNDARY,
    GUARANTEE,
    GUARANTEES,
    UNMEASURED,
    Kind,
)
from epistrel.orchestrator.models import registry
from epistrel.tools import guarantees as tool
from epistrel.tools import openapi
from epistrel.tools.generated import REPO_ROOT

SPEC = REPO_ROOT / "docs" / "03-requirements-spec.md"
DESIGN_DOCS = re.compile(r"^0[1-5]-")
ALLOWED = (
    "verified beats",
    "verified claims",
    "verified against",
    "re-verified",
    "unverified",
)
"""Phrases that do not call an output verified (they name what was checked, or deny it)."""
VERIFIED = re.compile(r"\bverified\b", re.IGNORECASE)
UNDELIVERED = next(row.id for row in GUARANTEES if not row.delivered)
"""A row that needs a module this deployment has not delivered (a later milestone's)."""


def _requirement(req: str) -> str:
    (line,) = [
        line
        for line in SPEC.read_text(encoding="utf-8").splitlines()
        if line.startswith(f"- **{req} (")
    ]
    return line


def _listed(text: str, marker: str) -> list[str]:
    """The comma-separated list in parentheses after ``marker`` in ``text``."""
    start = text.index(marker) + len(marker)
    assert text[start : start + 2] == " ("
    depth, end = 0, start + 1
    for end in range(start + 1, len(text)):
        depth += {"(": 1, ")": -1}.get(text[end], 0)
        if depth == 0:
            break
    return [item.strip() for item in text[start + 2 : end].split(", ")]


def _covered(item: str, steps: list[str]) -> bool:
    """Whether FR-VERIFY-15's ``item`` is a step of ``steps``, part of one, or a pair of them
    joined by "and" (context assembly and viewpoint filtering)."""
    if any(item in step for step in steps):
        return True
    return all(part in steps for part in item.split(" and "))


@pytest.mark.part("M4", "FR-VERIFY-15; FR-MODEL-2")
def test_c_14__m4(monkeypatch: pytest.MonkeyPatch) -> None:
    """The published table is current. It states the boundary, has every FR-VERIFY-15 step in
    its kind, and has a model-dependent row for every delivered model purpose, with an NFR-QUAL
    metric or ``unmeasured``, and the rendered document lists each one. A delivered purpose
    with no row fails ``--check``. So do a row naming a purpose the registry does not know, a
    model-dependent row with no metric, and a metric that is not an NFR-QUAL requirement."""
    rendered = tool.render()
    assert rendered == tool.TABLE_PATH.read_text(encoding="utf-8")
    assert f"**{BOUNDARY}**" in rendered

    text = _requirement("FR-VERIFY-15")
    deterministic = _listed(text, "are **deterministic**")
    measured = _listed(text, "are **model-dependent and measured**")
    assert (len(deterministic), len(measured)) == (9, 8)
    steps = {kind: [row.step for row in GUARANTEES if row.kind is kind] for kind in Kind}
    for item in deterministic:
        assert _covered(item, steps[Kind.deterministic]), item
    for item in measured:
        assert item in steps[Kind.model_dependent], item

    by_purpose = {
        purpose: row
        for row in GUARANTEES
        if row.kind is Kind.model_dependent
        for purpose in row.purposes
    }
    assert registry.DELIVERED
    for purpose in registry.DELIVERED:
        row = by_purpose[purpose]
        assert row.metric is not None
        assert row.metric == UNMEASURED or re.fullmatch(r"NFR-QUAL-\d+", row.metric), purpose
        (line,) = [
            line
            for line in rendered.splitlines()
            if f"| `{row.id}` | model-dependent | {row.metric} |" in line
        ]
        assert f"`{purpose}`" in line, line
    for row in GUARANTEES:
        if row.kind is Kind.deterministic:
            assert row.metric is None, row.id
    assert tool.problems() == []

    # The complement: a purpose delivered without a row fails the check, run once.
    monkeypatch.setattr(
        registry, "DELIVERED", {**registry.DELIVERED, "novel_purpose": registry.GENERATION}
    )
    with pytest.raises(tool.GuaranteeError, match="novel_purpose: a delivered model purpose"):
        tool.render()
    assert tool.main(["--check"]) == 1
    monkeypatch.undo()

    generation = GUARANTEE["generation"]
    broken = [
        dataclasses.replace(generation, purposes=("nonesuch",)),
        dataclasses.replace(GUARANTEE["salience"], metric=None),
        dataclasses.replace(GUARANTEE["embedding"], metric="NFR-QUAL-99"),
        dataclasses.replace(GUARANTEE["context_assembly"], metric="NFR-QUAL-7"),
        dataclasses.replace(GUARANTEE[UNDELIVERED], delivered_in=None),
        dataclasses.replace(GUARANTEE["verification"], delivered_in="M5"),
    ]
    found = tool.problems(broken, delivered=["generation"])
    assert found == [
        "generation: a delivered model purpose with no model-dependent row",
        "generation: 'nonesuch' is not a model purpose",
        "salience: a model-dependent step needs a metric or 'unmeasured'",
        "embedding: 'NFR-QUAL-99' is not an NFR-QUAL requirement",
        "context_assembly: a deterministic step has no eval metric",
        f"{UNDELIVERED}: needs an undelivered module but names no milestone",
        "verification: is delivered but says M5",
    ]


def _strings(node: Any) -> Iterator[str]:
    """Every ``description`` and ``summary`` in an OpenAPI document."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("description", "summary") and isinstance(value, str):
                yield value
            yield from _strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)


def output_claims(text: str) -> list[str]:
    """Where ``text`` calls something "fully verified", or "verified" outside ``ALLOWED``."""
    lowered = text.lower()
    hits = [match.group(0) for match in re.finditer(r"fully\s+verified", lowered)]
    for phrase in ALLOWED:
        lowered = lowered.replace(phrase, "")
    for match in VERIFIED.finditer(lowered):
        start = max(0, match.start() - 40)
        hits.append(lowered[start : match.end() + 20])
    return hits


def user_facing() -> dict[str, str]:
    """The user-facing documents by name: the README, every document under ``docs/`` but the
    design documents 01 to 05, and the OpenAPI descriptions and summaries, generated now."""
    docs: dict[str, str] = {"README.md": (REPO_ROOT / "README.md").read_text(encoding="utf-8")}
    for path in sorted((REPO_ROOT / "docs").glob("*.md")):
        if not DESIGN_DOCS.match(path.name):
            docs[f"docs/{path.name}"] = path.read_text(encoding="utf-8")
    docs["openapi"] = "\n".join(_strings(openapi.schema()))
    return docs


@pytest.mark.part("M4", "FR-VERIFY-15")
def test_c_14__no_output_called_verified() -> None:
    """No user-facing document calls an output "fully verified", or uses "verified" outside the
    phrases that only name what was checked. The check finds a planted claim (its complement)."""
    docs = user_facing()
    assert {"README.md", "docs/guarantees.md", "docs/capability-map.md", "openapi"} <= set(docs)
    assert {name: output_claims(text) for name, text in docs.items() if output_claims(text)} == {}
    assert output_claims("Every reply is Fully  Verified.") != []
    assert output_claims("The message is verified.") != []
    assert output_claims("The verified claims hold; unverified ones are flagged.") == []
    assert len(output_claims("verified claims, and a verified reply")) == 1
