"""The rating suggestion's model half (FR-RATE-8, FR-MODEL-2; #121): one ``rating_suggestion``
call per rating, run in parallel, and the advisory table.

``suggest`` reads the entry (``core.ratings.suggest.load_entry``) and every rating
(``include_retired``: the retired ones too), and for each rating:

1. **The rating's policies** are #56's creation context for the entry's characters: the live
   global policies and the rating closure's, age-conditioned ones active only when one of the
   entry's characters is a minor by true age (#55). Only those that apply to text count.
2. **Deterministic** policies run directly over every element (both text targets); no model
   call.
3. **One model call** (purpose ``rating_suggestion``, which resolves its model as its own row,
   then ``guardrail_check``'s, then ``default``) always runs, so the list is complete. It gets
   the rating's name, description and guidance, its model-assisted policies' prompts, the
   deterministic hits as context, the characters as opaque labels with true ages when an
   age-conditioned policy is active, and the elements: private text first, cut to fit the
   model's context (``truncated: true`` on the row). The template tells the model to report
   only conflicts with the listed policies, never taste, audience fit or quality.
4. **The answer** is validated strictly (``RatingSuggestionVerdict``). A concern is kept only
   when it names exactly one of a listed policy's id or a clause quoted from the rating's
   description, and an element exactly as listed; every other is dropped and counted
   (``dropped_concerns``). A repeat of a deterministic concern is not counted twice.

The table's ``model_calls`` lists the calls in rating order, however they finish. Calls run at
most ``PARALLEL`` at a time under one ``DEADLINE_S`` overall deadline: each call's
timeout is clamped to the time left. A call that fails, times out or answers unusably leaves
its row with ``error`` (``model_unavailable``, ``deadline`` or ``invalid_response``) and its
deterministic concerns (``appropriate: null`` when it has none); the table is still 200.
Judgments are not cached. Nothing is written.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.policies.model import ModelAssistedRule, Policy
from epistrel.core.ratings import Rating, list_ratings
from epistrel.core.ratings.suggest import (
    Concern,
    Element,
    EntryType,
    EvaluatedRating,
    LoadedEntry,
    RatingSuggestion,
    RowError,
    SuggestionRow,
    TextElement,
    VersionLabel,
    deterministic_concerns,
    load_entry,
    log_table,
    row,
    text_policies,
)
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models import metrics as model_metrics
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.errors import ModelError, ModelUnavailable
from epistrel.orchestrator.models.registry import KnownHit as KnownHitInput
from epistrel.orchestrator.models.registry import (
    RatingSuggestionInputs,
    RatingSuggestionVerdict,
    SuggestedConcern,
    SuggestionElement,
    SuggestionPolicy,
)
from epistrel.orchestrator.models.structured import parse
from epistrel.orchestrator.policies.age import CastMember, policy_prompt, subjects
from epistrel.orchestrator.policies.store import GuardContext, creation_context

PURPOSE = "rating_suggestion"
DEADLINE_S = 20.0
"""The whole table's deadline: every call's timeout is clamped to what is left of it."""
PARALLEL = 4
"""At most this many ``rating_suggestion`` calls at a time."""
CONTEXT_CHARS = 24_000
"""The elements' share of the model's context, in characters (about 6,000 tokens of the
purpose's 8,192-token floor; the rest is the rating, its policies and the instructions)."""
ELEMENT_OVERHEAD = 48
"""Characters an element costs besides its text and location (its kind, the framing)."""
INVALID_REASONS = frozenset({"garbage", "parse_error", "schema_error"})


def location_json(element: Element) -> str:
    """An element's location as the model sees it: canonical JSON, unset keys left out."""
    return json.dumps(
        element.location.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
    )


def _key(kind: str, location: dict[str, object]) -> str:
    cleaned = {k: v for k, v in location.items() if v is not None}
    return json.dumps({"kind": kind, "location": cleaned}, sort_keys=True, separators=(",", ":"))


def element_key(element: Element) -> str:
    return _key(element.kind, element.location.model_dump(mode="json"))


def fit(
    elements: Sequence[TextElement], limit: int = CONTEXT_CHARS
) -> tuple[list[TextElement], bool]:
    """The elements that fit the model's context, private ones first (each group in document
    order), and whether any was left out."""
    ordered = [e for e in elements if e.private] + [e for e in elements if not e.private]
    sent: list[TextElement] = []
    used = 0
    for item in ordered:
        cost = len(item.text) + len(location_json(item.element)) + ELEMENT_OVERHEAD
        if used + cost > limit:
            return sent, True
        sent.append(item)
        used += cost
    return sent, False


def _normalised(text: str) -> str:
    return " ".join(text.split()).casefold()


@dataclass(frozen=True, slots=True)
class _Review:
    """What one rating's review works from."""

    rating: Rating
    policies: tuple[Policy, ...]
    assisted: tuple[Policy, ...]
    found: list[Concern]
    context: GuardContext


def _inputs(
    review: _Review, sent: Sequence[TextElement], cast: Sequence[CastMember]
) -> RatingSuggestionInputs:
    rating = review.rating
    threshold = review.context.threshold
    policies: list[SuggestionPolicy] = []
    for policy in review.assisted:
        rule = policy.rule
        if not isinstance(rule, ModelAssistedRule):  # pragma: no cover - assisted only
            continue
        prompt = policy_prompt(rule.prompt, threshold) if policy.age_condition else rule.prompt
        policies.append(SuggestionPolicy(policy_id=policy.id, name=policy.name, prompt=prompt))
    aged = any(policy.age_condition is not None for policy in review.assisted)
    return RatingSuggestionInputs(
        rating_name=rating.name,
        rating_description=rating.description,
        rating_guidance=rating.prompt_guidance,
        policies=policies,
        deterministic_hits=[
            KnownHitInput(
                policy_id=concern.policy_id or "",
                kind=concern.element.kind,
                location=location_json(concern.element),
            )
            for concern in review.found
        ],
        subjects=subjects(cast) if aged else [],
        elements=[
            SuggestionElement(
                kind=item.element.kind, location=location_json(item.element), text=item.text
            )
            for item in sent
        ],
    )


def _kept(
    suggested: Sequence[SuggestedConcern],
    review: _Review,
    sent: Sequence[TextElement],
) -> tuple[list[Concern], int]:
    """The model's concerns that name a listed policy or a clause of the rating's description,
    and a listed element; and how many were dropped. Repeats are kept once."""
    elements = {element_key(item.element): item.element for item in sent}
    policy_ids = {policy.id for policy in review.policies}
    description = _normalised(review.rating.description)
    seen = {(c.policy_id, c.clause, element_key(c.element)) for c in review.found}
    kept: list[Concern] = []
    dropped = 0
    for concern in suggested:
        key = _key(concern.element.kind, concern.element.location.model_dump(mode="json"))
        element = elements.get(key)
        named_policy = concern.policy_id is not None and concern.policy_id in policy_ids
        named_clause = (
            concern.clause is not None
            and bool(concern.clause.strip())
            and _normalised(concern.clause) in description
        )
        one_basis = (concern.policy_id is None) != (concern.clause is None)
        if element is None or not one_basis or not (named_policy or named_clause):
            dropped += 1
            continue
        mark = (concern.policy_id, concern.clause, key)
        if mark in seen:
            continue
        seen.add(mark)
        kept.append(
            Concern(
                policy_id=concern.policy_id,
                clause=concern.clause,
                element=element,
                explanation=concern.explanation,
            )
        )
    return kept, dropped


async def _ask(
    models: ModelAccessLayer, inputs: RatingSuggestionInputs, budget: TurnBudget
) -> RatingSuggestionVerdict | RowError:
    if budget.expired():
        return "deadline"
    try:
        result = await models.call(PURPOSE, inputs, budget=budget)
    except ModelUnavailable as error:
        if error.reason == "deadline":
            return "deadline"
        if error.reason in INVALID_REASONS:
            return "invalid_response"
        return "model_unavailable"
    except ModelError:
        return "model_unavailable"
    if result.refused or result.finish_reason == "length":
        return "invalid_response"
    parsed = parse(result.text, RatingSuggestionVerdict)
    if not isinstance(parsed, RatingSuggestionVerdict):
        return "invalid_response"
    return parsed


async def _review(
    db: AsyncEngine,
    models: ModelAccessLayer,
    rating: Rating,
    loaded: LoadedEntry,
    cast: Sequence[CastMember],
    gate: asyncio.Semaphore,
    budget: TurnBudget,
) -> tuple[SuggestionRow, model_metrics.CallLog]:
    context = await creation_context(db, rating.id, cast)
    policies = text_policies(context.active)
    review = _Review(
        rating=rating,
        policies=policies,
        assisted=tuple(p for p in policies if isinstance(p.rule, ModelAssistedRule)),
        found=deterministic_concerns(loaded.elements, policies),
        context=context,
    )
    sent, truncated = fit(loaded.elements)
    inputs = _inputs(review, sent, cast)
    async with gate:
        # This rating's calls go to a log of their own, so the table lists them in rating order
        # whatever order the parallel calls finish in (``suggest`` merges them).
        with model_metrics.recording() as own:
            answer = await _ask(models, inputs, budget)
    error: RowError | None = None
    concerns = list(review.found)
    dropped = 0
    if isinstance(answer, RatingSuggestionVerdict):
        extra, dropped = _kept(answer.concerns, review, sent)
        concerns.extend(extra)
    else:
        error = answer
    return row(
        rating_id=rating.id,
        rating_name=rating.name,
        rating_version=rating.version_id,
        concerns=concerns,
        error=error,
        truncated=truncated,
        dropped=dropped,
    ), own


async def suggest(
    db: AsyncEngine,
    models: ModelAccessLayer,
    entry_type: EntryType,
    entry_id: str,
    version: VersionLabel | None = None,
    *,
    include_retired: bool = False,
) -> RatingSuggestion:
    """The advisory table for the entry (see the module docstring). 404 for an unknown entry or
    version. Writes nothing; ``model_calls``/``elapsed_ms`` are the Engine's to fill."""
    started = time.monotonic()
    loaded = await load_entry(db, entry_type, entry_id, version)
    ratings = await list_ratings(db, include_retired=include_retired)
    cast = tuple(
        CastMember(character_id=character_id, ordinal=ordinal, true_age=age)
        for ordinal, (character_id, age) in enumerate(loaded.true_ages, start=1)
    )
    budget = TurnBudget(deadline_s=DEADLINE_S)
    gate = asyncio.Semaphore(PARALLEL)
    reviewed = await asyncio.gather(
        *(_review(db, models, rating, loaded, cast, gate, budget) for rating in ratings)
    )
    rows = [found for found, _ in reviewed]
    outer = model_metrics.CALL_LOG.get()
    if outer is not None:
        for _, own in reviewed:
            if outer.first_started is None:
                outer.first_started = own.first_started
            outer.calls.extend(own.calls)
    table = RatingSuggestion(
        entry=loaded.entry,
        evaluated=[
            EvaluatedRating(rating_id=rating.id, rating_version=rating.version_id)
            for rating in ratings
        ],
        rows=list(rows),
        unavailable=list(loaded.unavailable),
        elapsed_ms=round((time.monotonic() - started) * 1000),
    )
    log_table(table)
    return table


__all__ = ["CONTEXT_CHARS", "DEADLINE_S", "PARALLEL", "PURPOSE", "fit", "suggest"]
