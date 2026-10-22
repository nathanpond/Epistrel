"""Q-02: referential and temporal resolution (docs/04 §9; FR-RET-2,3, NFR-QUAL-6; #157).

Each case is one query of #139's generated fixture (``retrieval/resolution.jsonl``: S-THOSEGUYS
generalised over six shapes, with its cast and relations), resolved through the retrieval
pipeline's resolve stage (``core.retrieval.pipeline.resolve_query``, the function ``retrieve``
runs first; ``evals.resolution.run``). No model is called; docs/04 sets no floor, so both
metrics are report-only.

- ``q02.mention_accuracy``: over every expected mention, the share resolved to the expected
  entity (or left unresolved where ``uncertain`` is right), micro-averaged per seed.
- ``q02.query_all_correct``: every mention, the time rule (or its reason) and the event type of
  the query right.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from epistrel.core.resolution.entities import ContextBeat, EntityEntry, Relation
from epistrel.evals import resolution
from epistrel.evals.gate import Metric, Rule
from epistrel.evals.suites import Aggregate, CaseModel, Counts, Outcome, Suite, register

if TYPE_CHECKING:
    from epistrel.evals.runner import CaseContext

MENTIONS = "q02.mention_accuracy"
QUERIES = "q02.query_all_correct"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Entry(_Model):
    id: str
    kind: str
    name: str
    gender: str | None = None


class ResolutionInputs(_Model):
    text: str = Field(min_length=1, max_length=500)
    speaker: str
    beats: list[tuple[str, str | None]] = Field(default_factory=list)
    entries: list[Entry]
    relations: list[tuple[str, str, str]] = Field(default_factory=list)


class ResolutionLabels(_Model):
    mentions: list[tuple[str, str | None]]
    time_rule: str | None
    event_type: str | None


class ResolutionCase(CaseModel):
    family: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    origin: Literal["whitepaper", "authored", "paraphrase"]
    inputs: ResolutionInputs
    expected: ResolutionLabels


def fixture_of(case: ResolutionCase) -> resolution.Fixture:
    query = resolution.Query(
        case.id,
        case.inputs.text,
        case.inputs.speaker,
        tuple(ContextBeat(text, actor) for text, actor in case.inputs.beats),
        tuple(resolution.Expected(s, e) for s, e in case.expected.mentions),
        case.expected.time_rule,
        case.expected.event_type,
    )
    entries = tuple(EntityEntry(e.id, e.kind, e.name, e.gender) for e in case.inputs.entries)
    relations = tuple(Relation(s, p, o) for s, p, o in case.inputs.relations)
    return resolution.Fixture(entries, relations, (query,))


@register
class Q02Suite(Suite):
    name = "q02"
    title = "Q-02: referential and temporal resolution (S-THOSEGUYS generalised)"
    set_path = "retrieval/resolution.jsonl"
    min_cases = 120
    ci_gate = False
    case_model = ResolutionCase
    metrics = (
        Metric(MENTIONS, Rule.informational, description="a mention resolved as labeled"),
        Metric(QUERIES, Rule.informational, description="a whole query resolved as labeled"),
    )
    reading_notes = (
        "Q-02 resolves each query through the retrieval pipeline's resolve stage, with no "
        "model; docs/04 sets no floor, so its rows are report-only.",
    )

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        assert isinstance(case, ResolutionCase)  # noqa: S101 - load() validated it
        (result,) = resolution.run(fixture_of(case))
        right = sum(result.mentions_right)
        return Outcome(
            {QUERIES: result.all_correct},
            output=json.dumps(
                {
                    "mentions": list(result.mentions_right),
                    "time": result.time_right,
                    "event": result.event_right,
                }
            ),
            detail={"right": right, "wrong": len(result.mentions_right) - right},
        )

    def aggregate(self, outcomes: Sequence[Outcome]) -> Mapping[str, Aggregate]:
        return {
            MENTIONS: Counts(
                sum(int(o.detail["right"]) for o in outcomes),
                sum(int(o.detail["wrong"]) for o in outcomes),
            )
        }


__all__ = ["MENTIONS", "QUERIES", "Q02Suite", "ResolutionCase", "fixture_of"]
