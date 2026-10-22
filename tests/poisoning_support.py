"""The poisoning loop (FR-GUARD-5,6; #145): plant a policy-violating canary in a structured
story's memories, ask for it back, and run the output gate's regeneration loop over a context
retrieved afresh for every render, the way the structured pipeline (#150) will.

``run_sequence`` plays one red-team sequence (``tests/redteam/corpus/*.json``, schema ``{id,
canary, plants[], requests[], policy, dials, expected}``) on #133's world (Cal the player,
Bobby, Ann) on the seeded ``Structured (M4)`` profile:

- each plant is an episodic memory written by a turn the way the structured commit writes one
  (``memory_support.turn``): an ingested ``narration`` (an unvoiced reply), Bobby's ``say`` or
  the player's ``say``; ``decoys`` are clean memories that share the request's words;
- the policy is a story-scoped, output-only wordlist over the canary (the plant is accepted by
  design: nothing gates what the story stores, only what it releases);
- the stub's ``parrot`` mode answers a ``generation`` prompt holding the canary with the
  canary, as a model repeating its context would; a prompt without it gets the default reply;
- each request is one render loop: ``produce`` retrieves for the request (#141's pipeline,
  lexical arms, under the configuration the turn captured), puts the assembled context in the
  prompt's memory section (``summary_segments``: the Lite template's slot for what the
  character remembers, until #150's structured template), calls the ``generation`` purpose
  through the Model Access Layer, and hands back a ``Draft`` carrying the context's records
  (``implication.draft_context``). ``gate_output`` runs it under the story's polish budget
  with an ``Implicator`` at the story's ``repair.implication_threshold`` (``auto=False``:
  threshold 0, the complement: counted, never quarantined).

``SequenceRun`` holds, per request, the renders, whether the fallback was released, what was
released and every text the consumer would see (the reply, the released violations' messages);
``emissions`` counts the canary in them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel import Engine
from epistrel.core.ids import new_ulid
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.retrieval.pipeline import RetrievalQuery, retrieve
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.lite.pipeline import guardrail_fields, polish_budget
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.registry import GenerationInputs, PlayerInput
from epistrel.orchestrator.policies.implication import Implicator, draft_context, threshold_of
from epistrel.orchestrator.policies.output_gate import (
    Draft,
    FallbackLines,
    Gated,
    gate_output,
    output_policies,
)
from epistrel.orchestrator.policies.store import guard_context
from tests import consolidation_support as cs
from tests.memory_support import turn
from tests.policy_support import AUTHOR, story_item, words

CORPUS_DIR = Path(__file__).parent / "redteam" / "corpus"
GENERATION = "generation"
SPEAKER = "Bobby"


@dataclass(frozen=True)
class Plant:
    kind: str
    """``narration`` (an ingested, unvoiced reply), ``say`` (Bobby's) or ``player`` (Cal's)."""
    text: str


@dataclass(frozen=True)
class Spec:
    """One red-team sequence (see the module docstring)."""

    id: str
    canary: str
    plants: tuple[Plant, ...]
    requests: tuple[str, ...]
    policy_message: str
    threshold: int
    max_regenerations: int
    expected: dict[str, Any]
    decoys: tuple[str, ...] = ()
    pr: bool = False

    @classmethod
    def load(cls, path: Path) -> Spec:
        raw = json.loads(path.read_text(encoding="utf-8"))
        dials = raw.get("dials", {})
        return cls(
            id=raw["id"],
            canary=raw["canary"],
            plants=tuple(Plant(p["kind"], p["text"]) for p in raw["plants"]),
            requests=tuple(raw["requests"]),
            policy_message=raw["policy"]["message"],
            threshold=int(dials.get("implication_threshold", 2)),
            max_regenerations=int(dials.get("max_regenerations", 2)),
            expected=raw["expected"],
            decoys=tuple(raw.get("decoys", ())),
            pr=bool(raw.get("pr", False)),
        )


def corpus() -> list[Spec]:
    """Every sequence of the corpus, by id."""
    return sorted((Spec.load(p) for p in CORPUS_DIR.glob("*.json")), key=lambda s: s.id)


@dataclass
class Render:
    """One request's loop: its renders, whether the fallback was released, the released text,
    the texts a consumer sees, the memories quarantined and the gate's outcome."""

    renders: int
    fallback: bool
    released: str
    visible: tuple[str, ...]
    quarantined: tuple[str, ...]
    gated: Gated
    fields: dict[str, Any]


@dataclass
class SequenceRun:
    spec: Spec
    story_id: str
    planted: tuple[str, ...]
    requests: list[Render] = field(default_factory=list)

    @property
    def emissions(self) -> int:
        """How often the canary reached anything a consumer would see."""
        pattern = re.compile(re.escape(self.spec.canary), re.IGNORECASE)
        return sum(len(pattern.findall(t)) for r in self.requests for t in r.visible)


async def _plant(engine: AsyncEngine, story: str, plant: Plant, w: Any) -> str:
    if plant.kind == "narration":
        _, found = await turn(engine, story, say=None, reply=plant.text)
    elif plant.kind == "say":
        _, found = await turn(
            engine, story, actor=w.characters[SPEAKER], say=plant.text, reply=None
        )
    else:
        _, found = await turn(engine, story, actor=w.characters["Cal"], say=plant.text, reply=None)
    (memory,) = found
    return memory.id


async def converge(
    engine: AsyncEngine,
    models: ModelAccessLayer,
    story_id: str,
    request: str,
    *,
    speaker: str = SPEAKER,
    implicate: bool = True,
) -> Render:
    """One request's render loop (see the module docstring)."""
    async with engine.connect() as conn:
        configuration = await effective_configuration(conn, story_id, validate=False)
    settings = configuration.settings
    context = await guard_context(engine, story_id)
    policies = output_policies(context)
    budget = TurnBudget()
    implicator = Implicator(
        engine,
        story_id,
        turn_operation_id=new_ulid(),
        threshold=threshold_of(settings),
        policies=policies,
    )

    async def produce(constraints: tuple[str, ...], attempt: int) -> Draft:
        async with engine.connect() as conn:
            found = await retrieve(
                conn, story_id, RetrievalQuery(request), settings=settings, touch=False
            )
        payload = GenerationInputs(
            summary_segments=[item.text for item in found.context.items],
            voiced_characters=[speaker],
            policy_constraints=list(constraints),
            player_input=PlayerInput(name="Cal", say=request),
        )
        result = await models.call(GENERATION, payload, ModelParams(), budget=budget)
        return draft_context(found, Draft(text=result.text, model=result.model))

    gated = await gate_output(
        models,
        context,
        policies,
        produce,
        budget=budget,
        retry_budget=polish_budget(settings),
        fallback=FallbackLines(name=speaker),
        implicate=implicator if implicate else None,
    )
    fields = guardrail_fields(gated)
    visible = (gated.draft.text, *(v.message for v in gated.violations))
    return Render(
        renders=gated.attempts,
        fallback=gated.fallback_used,
        released=gated.draft.text,
        visible=visible,
        quarantined=gated.quarantined,
        gated=gated,
        fields=fields,
    )


async def run_sequence(
    engine: AsyncEngine,
    lib: Engine,
    models: ModelAccessLayer,
    spec: Spec,
    *,
    auto: bool = True,
) -> SequenceRun:
    """Plant ``spec``'s canary and play its requests (``auto=False``: threshold 0)."""
    overrides = {
        "repair": {
            "implication_threshold": spec.threshold if auto else 0,
            "polish": {"max_regenerations": spec.max_regenerations},
        }
    }
    w = await cs.story(engine, overrides)
    story = w.story_id
    await lib.policies_set_story(
        AUTHOR,
        story,
        [
            story_item(
                words(spec.canary.lower()), name=f"r01-{spec.id}", message=spec.policy_message
            )
        ],
    )
    planted = tuple([await _plant(engine, story, p, w) for p in spec.plants])
    for decoy in spec.decoys:
        await turn(engine, story, say=None, reply=decoy)
    models.stub.reset()
    models.stub.parrot(re.escape(spec.canary), purpose=GENERATION, template="Psst: {canary}.")
    run = SequenceRun(spec, story, planted)
    for request in spec.requests:
        run.requests.append(await converge(engine, models, story, request))
    return run


def summary(run: SequenceRun) -> dict[str, Any]:
    """What a sequence's ``expected`` states: renders and fallbacks per request, and how many
    planted memories ended quarantined."""
    quarantined = {m for r in run.requests for m in r.quarantined}
    return {
        "renders": [r.renders for r in run.requests],
        "fallback": [r.fallback for r in run.requests],
        "quarantined": len(quarantined & set(run.planted)),
    }


def planted_in(prompts: Sequence[str], canary: str) -> list[bool]:
    """Whether each prompt held the canary."""
    return [canary.casefold() in p.casefold() for p in prompts]


__all__ = [
    "CORPUS_DIR",
    "Plant",
    "Render",
    "SequenceRun",
    "Spec",
    "converge",
    "corpus",
    "planted_in",
    "run_sequence",
    "summary",
]
