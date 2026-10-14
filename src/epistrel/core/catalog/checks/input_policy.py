"""The input-policy check (FR-GUARD-10, FR-SCN-5; #111): catalog content that fails an input policy
warns at publish, since it enters no story until genesis, where it is gated again (#119).

The texts checked are the scenario's private prose a story created from it can receive: the
world (its description, every location's and entity's name, description and aliases), each
role's setup description, relationship names and name overrides, every seed's text, the opening
narration, the director's material (plot hooks, objectives, guidance, reveal conditions,
milestones, endings), and every string inside the objects stored as given (starting values,
scheduled items, the rules, a role's starting values). The public card (never seeded) and the
visual style (image prompts) are not.

The policies are the live global policies that apply to ``text_input`` plus the scenario
rating's closure policies set to apply to input too (#56's ``creation_context``), evaluated
through the ``PolicyEvaluator`` port (#103), which the Engine wires to the Orchestrator. That
evaluation writes no audit row and raises nothing. Deterministic policies always run (no model
call). Model-assisted policies run within the deployment's ``validation_model_calls`` budget
(default 0), one call per policy and text; those the budget does not reach, or that meet an
unavailable model, are listed ``input_policy_model_assisted``. Age-conditioned policies need a
cast and a scenario has none, so they are listed ``age_conditioned_policies``. A port that
cannot evaluate policies at all lists ``input_policy``.

The evaluation is the check's prepare phase: it may call a model, so it runs before the
publish's transaction. Each hit is one ``input_policy`` warning (outcome ``kept``) per text and
policy, at the text's field, quoting the matched span (or, when no span can be located, the
start of the text; at most 200 characters) with the policy's id as its ``rule``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from epistrel.core.catalog.checks.base import (
    Check,
    Findings,
    Preparation,
    Subject,
    ValidationContext,
)
from epistrel.core.catalog.checks.scenario import seeds_at
from epistrel.core.catalog.report import SPAN_MAX, Outcome
from epistrel.core.catalog.scenario_models import ScenarioDraft
from epistrel.core.ports import PolicyEvaluation, PortText


@dataclass(frozen=True, slots=True)
class TextAt:
    """One text the check sends to the policies: its field (the port's locator), the role it
    belongs to, the seed it is the text of, and the text."""

    field: str
    text: str
    role: str | None = None
    seed_id: str | None = None


def strings_in(value: Any, path: str) -> Iterator[tuple[str, str]]:
    """``(field, string)`` for every string inside ``value`` (an object stored as given)."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from strings_in(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from strings_in(item, f"{path}[{index}]")


def policy_texts(draft: ScenarioDraft) -> list[TextAt]:
    """Every text the check sends to the policies (see the module docstring), in document
    order; blank ones are left out."""
    found: list[TextAt] = []

    def add(field: str, text: str | None, role: str | None = None, seed: str | None = None) -> None:
        if text is not None and text.strip():
            found.append(TextAt(field, text, role, seed))

    world = draft.world
    add("world.description", world.description)
    for index, location in enumerate(world.locations):
        add(f"world.locations[{index}].name", location.name)
        add(f"world.locations[{index}].description", location.description)
    for index, entity in enumerate(world.entities):
        add(f"world.entities[{index}].name", entity.name)
        for alias_index, alias in enumerate(entity.aliases):
            add(f"world.entities[{index}].aliases[{alias_index}]", alias)
        add(f"world.entities[{index}].description", entity.description)
    for index, role in enumerate(draft.cast.roles):
        at = f"cast.roles[{index}]"
        add(f"{at}.setup_description", role.setup_description, role.id)
        for rel_index, relationship in enumerate(role.relationships):
            add(f"{at}.relationships[{rel_index}].name", relationship.name, role.id)
        if role.name_overrides is not None:
            add(f"{at}.name_overrides.canonical", role.name_overrides.canonical, role.id)
            for alias_index, alias in enumerate(role.name_overrides.aliases):
                add(f"{at}.name_overrides.aliases[{alias_index}]", alias, role.id)
        for field, text in strings_in(role.state_initial_values, f"{at}.state_initial_values"):
            add(field, text, role.id)
    for seed_at in seeds_at(draft):
        add(f"{seed_at.field}.text", seed_at.seed.text, seed_at.role, seed_at.seed.id)
    start = draft.starting_state
    add("starting_state.opening.narration", start.opening.narration)
    for name in ("states", "clock", "scheduled_events", "predicates", "closed_sets", "rules"):
        for field, text in strings_in(getattr(start, name), f"starting_state.{name}"):
            add(field, text)
    drivers = draft.drivers
    for name in ("plot_hooks", "objectives", "milestones", "endings"):
        for index, item in enumerate(getattr(drivers, name)):
            add(f"drivers.{name}[{index}]", item)
    add("drivers.director_guidance", drivers.director_guidance)
    for index, reveal in enumerate(drivers.planned_reveals):
        add(f"drivers.planned_reveals[{index}].conditions", reveal.conditions)
    return found


@dataclass(frozen=True, slots=True)
class Prepared:
    """What the prepare phase found: the texts sent, by field, and the port's evaluation."""

    texts: dict[str, TextAt]
    evaluation: PolicyEvaluation


class InputPolicyCheck(Check):
    """See the module docstring."""

    name = "input_policy"

    async def prepare(self, subject: Subject, env: Preparation) -> Prepared | None:
        try:
            draft = ScenarioDraft.model_validate(subject.document)
        except ValidationError:
            return None  # the document check reports it, and gates this check
        texts = policy_texts(draft)
        evaluation = await env.ports.policy_evaluator.evaluate(
            env.db,
            [PortText(locator=t.field, text=t.text) for t in texts],
            rating_id=draft.metadata.content_rating_id,
            max_model_calls=env.settings.validation_model_calls,
        )
        return Prepared(texts={t.field: t for t in texts}, evaluation=evaluation)

    async def run(self, subject: Subject, ctx: ValidationContext) -> Findings:
        prepared = ctx.prepared.get(self.name)
        if not isinstance(prepared, Prepared):
            return Findings(unavailable=[self.name])  # not prepared: never passed silently
        found = Findings(unavailable=list(prepared.evaluation.unavailable))
        for hit in prepared.evaluation.hits:
            at = prepared.texts.get(hit.locator)
            if at is None:
                continue
            found.entries.append(
                subject.entry(
                    "input_policy",
                    Outcome.kept,
                    field=at.field,
                    role=at.role,
                    seed_id=at.seed_id,
                    text=(hit.span or at.text)[:SPAN_MAX],
                    rule=hit.policy_id,
                )
            )
        return found


CHECK = InputPolicyCheck()

__all__ = ["CHECK", "InputPolicyCheck", "Prepared", "TextAt", "policy_texts", "strings_in"]
