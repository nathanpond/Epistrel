"""S-ENTITY-IDENTITY: mentions, aliases, and identity as a fact (docs/04 §7), its M4 part.

#139's slice (FR-PROP-6): every character of a story created from a scenario is an entity with
the character's id, its name and its registered aliases (the role's name overrides, the persona's
name), a gender and descriptors; mentions resolve through names and aliases; an ambiguous
mention ("he said it", with two candidate referents) is ``uncertain`` with both candidates and
never a guess. #140 asserts the verdict on top (no contradiction arises from it); M5's part is
``same_as`` as a held fact, M7's the history side-channel.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.resolution.context import load_context, load_index
from epistrel.core.resolution.entities import resolve_mentions
from epistrel.core.resolution.types import MentionKind, ReasonCode
from tests import genesis_support as g
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import respond_client
from tests.story_support import WRITE


@pytest.fixture
async def client(pg_url: str, engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    async with respond_client(auth_settings(pg_url, model_adapter="stub"), engine) as (found, _):
        yield found


@pytest.mark.integration
@pytest.mark.part("M4", "FR-PROP-6")
async def test_s_entity_identity__m4(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    """A scenario's cast becomes entities at genesis (Lite included): the catalog character
    filling ``brother`` under the role's name overrides is "Tom Harrow" with the alias "Tommy",
    masculine and "tall" from its appearance; the player persona Sam is feminine; the ad-hoc
    mother Ada has no gender. "Tommy" and "the tall man" resolve to Tom. "Then Tom told Ada he
    said it" has two candidate referents for "he": ``uncertain(ambiguous)`` listing both, no
    entity. Complement: with one compatible antecedent the same pronoun resolves, and a
    character added later is an entity too."""
    tom_catalog = await g.catalog_character_id(
        client, "Thomas Hale", definition={"appearance": "A tall man with a grey beard."}
    )
    scenario_id, _ = await g.published_scenario(
        client,
        g.scenario_body(
            g.role("mother"),
            g.role("brother", name_overrides={"canonical": "Tom Harrow", "aliases": ["Tommy"]}),
            g.role("player", kind="player"),
        ),
    )
    made = await g.create_from(
        client,
        g.from_scenario(
            scenario_id,
            slots={"mother": g.ad_hoc_slot("Ada", 61), "brother": g.catalog_slot(tom_catalog)},
            players={"player": g.player(name="Sam", gender="woman")},
        ),
    )
    story = made["story_id"]
    cast = {c["name"]: c["id"] for c in made["characters"]}
    assert set(cast) == {"Ada", "Tom Harrow", "Sam"}
    entities = {
        r.id: r for r in await rows(engine, "SELECT * FROM entities WHERE story_id = :s", s=story)
    }
    # One entity per character, with the character's id, Lite included.
    assert {str(i) for i in entities} == set(cast.values())
    assert all(str(e.character_id) == str(e.id) for e in entities.values())
    tom, ada, sam = cast["Tom Harrow"], cast["Ada"], cast["Sam"]
    assert (entities[tom].gender, entities[sam].gender, entities[ada].gender) == (
        "masculine",
        "feminine",
        None,
    )
    assert {"tall", "grey", "beard"} <= set(entities[tom].descriptors)
    aliases = await rows(
        engine,
        "SELECT alias, source FROM entity_aliases WHERE entity_id = :e ORDER BY alias",
        e=tom,
    )
    assert [(a.alias, a.source) for a in aliases] == [
        ("Tom Harrow", "canonical"),
        ("Tommy", "name_override"),
    ]
    async with engine.connect() as conn:
        index = await load_index(conn, story)
        context = await load_context(conn, story, position=(1, 0), speaker=sam, index=index)
    # Names and registered aliases resolve; so does a description.
    text = "Then Tommy waved, and the tall man sat."
    found = resolve_mentions(text, sam, context)
    assert [(text[m.span[0] : m.span[1]], m.entity_id) for m in found] == [
        ("Tommy", tom),
        ("the tall man", tom),
    ]
    # "he said it" with two candidate referents: uncertain, never a guess.
    found = resolve_mentions("Then Tom told Ada he said it.", sam, context)
    he = next(m for m in found if m.kind is MentionKind.PRONOUN and m.span[1] - m.span[0] == 2)
    assert he.entity_id is None
    assert he.candidates == tuple(sorted((tom, ada)))
    uncertain = he.uncertain()
    assert uncertain is not None
    assert uncertain.reason.code is ReasonCode.AMBIGUOUS
    assert uncertain.reason.gate == "unresolved_entity"
    # Complement: one compatible antecedent resolves the same pronoun.
    found = resolve_mentions("Then Tom came in. He said it.", sam, context)
    assert next(m.entity_id for m in found if m.kind is MentionKind.PRONOUN) == tom
    # A character added to the running story is an entity at once.
    added = await client.post(
        f"/stories/{story}/characters",
        json={"name": "Wren Pike", "definition": "A neighbour.", "true_age": 30},
        headers=WRITE,
    )
    assert added.status_code == 201, added.text
    wren = added.json()["id"]
    async with engine.connect() as conn:
        index = await load_index(conn, story)
    entry = index.get(wren)
    assert entry is not None
    assert (entry.kind, entry.canonical_name, entry.character_id) == ("person", "Wren Pike", wren)
    (mention,) = resolve_mentions("Then Pike left.", sam, type(context)(index))
    assert mention.entity_id == wren
