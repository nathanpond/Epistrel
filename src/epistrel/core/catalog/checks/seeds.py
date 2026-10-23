"""The seed check (FR-SCN-5,8; #111, #173): every knowledge seed declares its holder, its
audience and its disclosure, and names only what the scenario has.

**Required fields.** One error per missing field, its code naming the field
(``seed_missing_holder``, ``seed_missing_audience``, ``seed_missing_disclosure``), at that field
(``cast.roles[0].seeded_memories[1].audience``), with the seed's server-assigned id and, for a
role's seed, the role. Only seed items need the three: the world's seeded memories, each role's,
and each planned reveal's truth (``ScenarioDraft.seeds``). A draft may leave them out (#108); a
published seed may not; a seed's holder is checked against the roles when the draft is saved.

**Audiences and references (#173).** The roles are always known here, so an audience is checked
against them: a token that is no level (``self``, ``acquaintances``, ``perceivable``,
``public``), no role id and no named group of ``cast.groups`` is an error
(``seed_audience_unknown``), as is a ``belief`` or ``nested`` seed on the ``world`` holder (a
belief is someone's: ``seed_holder_required``) and a ``nested`` seed without its ``nested``
content (``seed_nested_missing``). ``acquaintances`` on a role no relationship links to anyone
changes nothing and is a warning (``seed_audience_ignored``, outcome ``ignored``); ``self`` on a
world seed, which the catalog's own seeds use, is ignored silently. A group member, a
relationship's audience, a channel participant or a nested seed's attributed holder that is no
role, and a location's ``located_in`` or ``adjacent_to`` that is no location, are errors
(``seed_reference_unknown``).
"""

from __future__ import annotations

from collections.abc import Iterator

from epistrel.core.catalog.checks.base import Check, Findings, Subject, ValidationContext
from epistrel.core.catalog.checks.scenario import draft_of, seeds_at
from epistrel.core.catalog.report import Entry, Outcome
from epistrel.core.catalog.scenario_models import AUDIENCE_LEVELS, WORLD_HOLDER, ScenarioDraft

SEED_FIELDS = ("holder", "audience", "disclosure")
RULE = "a published seed declares its holder, audience and disclosure (FR-SCN-8)"
AUDIENCE_RULE = "a seed's audience names levels, roles or the scenario's named groups (FR-SCN-8)"
REFERENCE_RULE = "a knowledge seed names the scenario's own roles and locations (FR-SCN-8)"
HOLDER_RULE = "a belief is someone's: a belief or nested seed has a role as its holder (FR-SCN-8)"
AUDIENCE_UNKNOWN = "seed_audience_unknown"
AUDIENCE_IGNORED = "seed_audience_ignored"
HOLDER_REQUIRED = "seed_holder_required"
NESTED_MISSING = "seed_nested_missing"
REFERENCE_UNKNOWN = "seed_reference_unknown"
BELIEF_KINDS = frozenset({"belief", "nested"})


def _linked(draft: ScenarioDraft) -> set[str]:
    """The roles a relationship links to another role, either end."""
    found: set[str] = set()
    for role in draft.cast.roles:
        for link in role.relationships:
            found.update((role.id, link.to_role))
    return found


def _references(subject: Subject, draft: ScenarioDraft) -> Iterator[Entry]:
    roles = {role.id for role in draft.cast.roles}
    places = {location.id for location in draft.world.locations}

    def unknown(field: str, value: str, role: str | None = None) -> Entry:
        return subject.entry(
            REFERENCE_UNKNOWN,
            Outcome.rejected,
            field=field,
            role=role,
            text=value,
            rule=REFERENCE_RULE,
        )

    for name, members in sorted((draft.cast.groups or {}).items()):
        for index, member in enumerate(members):
            if member not in roles:
                yield unknown(f"cast.groups.{name}[{index}]", member)
    for r, role in enumerate(draft.cast.roles):
        for i, link in enumerate(role.relationships):
            for a, member in enumerate(link.audience or ()):
                if member not in roles:
                    yield unknown(
                        f"cast.roles[{r}].relationships[{i}].audience[{a}]", member, role.id
                    )
    for c, channel in enumerate(draft.starting_state.channels):
        for p, member in enumerate(channel.participants):
            if member not in roles:
                yield unknown(f"starting_state.channels[{c}].participants[{p}]", member)
    for n, location in enumerate(draft.world.locations):
        if location.located_in is not None and location.located_in not in places:
            yield unknown(f"world.locations[{n}].located_in", location.located_in)
        for a, adjacent in enumerate(location.adjacent_to):
            if adjacent.location not in places:
                yield unknown(f"world.locations[{n}].adjacent_to[{a}].location", adjacent.location)


class SeedsCheck(Check):
    """See the module docstring."""

    name = "seeds"

    async def run(self, subject: Subject, ctx: ValidationContext) -> Findings:
        found = Findings()
        draft = draft_of(subject)
        roles = {role.id for role in draft.cast.roles}
        groups = set(draft.cast.groups or {})
        linked = _linked(draft)
        for at in seeds_at(draft):
            seed = at.seed
            for name in SEED_FIELDS:
                if getattr(seed, name) is None:
                    found.entries.append(
                        subject.entry(
                            f"seed_missing_{name}",
                            Outcome.rejected,
                            field=f"{at.field}.{name}",
                            role=at.role,
                            seed_id=seed.id,
                            rule=RULE,
                        )
                    )

            def entry(code: str, outcome: Outcome, field: str, text: str | None, rule: str) -> None:
                found.entries.append(
                    subject.entry(
                        code,
                        outcome,
                        field=field,
                        role=at.role,  # noqa: B023 - used within the iteration
                        seed_id=seed.id,  # noqa: B023
                        text=text,
                        rule=rule,
                    )
                )

            world = seed.holder in (None, WORLD_HOLDER)
            for index, name in enumerate(seed.audience or ()):
                where = f"{at.field}.audience[{index}]"
                ignored = name == "acquaintances" and not world and seed.holder not in linked
                if name not in AUDIENCE_LEVELS and name not in roles and name not in groups:
                    entry(AUDIENCE_UNKNOWN, Outcome.rejected, where, name, AUDIENCE_RULE)
                elif ignored:
                    entry(AUDIENCE_IGNORED, Outcome.ignored, where, name, AUDIENCE_RULE)
            kind = seed.kind or "canon"
            if kind in BELIEF_KINDS and seed.holder == WORLD_HOLDER:
                entry(HOLDER_REQUIRED, Outcome.rejected, f"{at.field}.holder", None, HOLDER_RULE)
            if kind == "nested" and seed.nested is None:
                entry(NESTED_MISSING, Outcome.rejected, f"{at.field}.nested", None, HOLDER_RULE)
            if seed.nested is not None and seed.nested.attributed_holder not in roles:
                entry(
                    REFERENCE_UNKNOWN,
                    Outcome.rejected,
                    f"{at.field}.nested.attributed_holder",
                    seed.nested.attributed_holder,
                    REFERENCE_RULE,
                )
        found.entries.extend(_references(subject, draft))
        return found


CHECK = SeedsCheck()

__all__ = [
    "AUDIENCE_IGNORED",
    "AUDIENCE_UNKNOWN",
    "CHECK",
    "HOLDER_REQUIRED",
    "NESTED_MISSING",
    "REFERENCE_UNKNOWN",
    "SEED_FIELDS",
    "SeedsCheck",
]
