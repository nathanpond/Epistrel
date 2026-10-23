"""What each indexed record gives the subject-reference index (FR-ADMIN-6; #123).

Each ``*_record`` function turns one row into its ``index.Record``: its id, its scope, its text
fields by dotted path and its declared subjects. The writers call them with the row they wrote
and the rebuild's readers (``read_*``, named by ``index.SUBJECT_WRITERS``) with the rows they
read back, so the two compute the same rows.

**JSON columns** are walked whole (``text_fields``): every string leaf is a text field at its
dotted path (list items as ``[n]``), except the values of id-like keys (``id``, ``*_id``,
``*_ids``), which name records rather than people. Walking the whole document rather than a
declared list means a field added later is indexed without a change here; over-inclusion is the
safe direction for erasure. In a story record, ``character_id`` and the ``*character_ids``
lists declare the characters they name (``character_ids``).

Record ids: a row's own id, or for a composite key its parts joined by ``:`` (a snapshot's
``<story_id>:<character_id>``, a stored result's ``<scope>:<scope_id>:<key>``, a rating's
``<message_id>:<principal>``), and a catalog version's ``<id>@<version>``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.erasure.index import Declared, Record
from epistrel.core.tables import (
    character_catalog,
    character_snapshots,
    character_versions,
    characters,
    config_changes,
    events,
    genesis_deferred,
    idempotency_keys,
    media_assets,
    operations,
    personas,
    policies,
    scenario_catalog,
    scenario_versions,
    stories,
)
from epistrel.core.tables.beliefs import belief_transitions, beliefs
from epistrel.core.tables.commitments import thunks
from epistrel.core.tables.exposures import exposures
from epistrel.core.tables.facts import facts
from epistrel.core.tables.media import SubjectKind, media_upload_sessions
from epistrel.core.tables.memories import memories
from epistrel.core.tables.propositions import (
    closed_sets,
    entities,
    entity_aliases,
    predicate_proposals,
    predicates,
    propositions,
)

STORY_SCOPE = "story"
"""The ``scope`` of a story's configuration change, stored result and policy."""
CHARACTER_SCOPE = "character"
"""A policy's ``scope`` when it applies to one character (``scope_id``: its id)."""
CHARACTER_KEY = "character_id"
CHARACTER_LISTS = ("character_ids", "present_character_ids", "render_character_ids")


def _id_like(key: str) -> bool:
    return key == "id" or key.endswith(("_id", "_ids"))


def _join(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


def text_fields(value: Any, prefix: str = "") -> dict[str, str | None]:
    """Every string leaf of ``value`` by dotted path (see the module docstring)."""
    found: dict[str, str | None] = {}

    def walk(node: Any, at: str) -> None:
        if isinstance(node, str):
            if node.strip():
                found[at] = node
        elif isinstance(node, Mapping):
            for key, child in node.items():
                if isinstance(key, str) and not _id_like(key):
                    walk(child, _join(at, key))
        elif isinstance(node, list | tuple):
            for index, child in enumerate(node):
                walk(child, f"{at}[{index}]")

    walk(value, prefix)
    return found


def character_ids(value: Any, prefix: str = "") -> list[Declared]:
    """The characters a story document names by id (``character_id``, ``*character_ids``)."""
    found: list[Declared] = []

    def walk(node: Any, at: str) -> None:
        if isinstance(node, Mapping):
            for key, child in node.items():
                if not isinstance(key, str):
                    continue
                path = _join(at, key)
                if key == CHARACTER_KEY and isinstance(child, str):
                    found.append((SubjectKind.CHARACTER, child, path))
                elif key in CHARACTER_LISTS and isinstance(child, list):
                    found.extend(
                        (SubjectKind.CHARACTER, item, f"{path}[{index}]")
                        for index, item in enumerate(child)
                        if isinstance(item, str)
                    )
                else:
                    walk(child, path)
        elif isinstance(node, list | tuple):
            for index, child in enumerate(node):
                walk(child, f"{at}[{index}]")

    walk(value, prefix)
    return found


def _str(value: Any) -> str | None:
    return None if value is None else str(value)


# -- story records -----------------------------------------------------------------------------


def event_record(row: Any) -> Record:
    """An event (``events``: id, story_id, character_id, payload)."""
    story_id = _str(row.story_id)
    return Record(
        events.name,
        str(row.id),
        story_id,
        text_fields(row.payload, "payload"),
        tuple(character_ids(row.payload, "payload")) if story_id is not None else (),
        authored_by=_str(row.character_id) if story_id is not None else None,
    )


def character_record(row: Any) -> Record:
    """A story character: its own row (it defines the subject) and its texts."""
    fields: dict[str, str | None] = {
        "name": row.name,
        "definition": row.definition,
        "safe_fallback_text": row.safe_fallback_text,
        "deflection_style": row.deflection_style,
        **text_fields(row.persona_snapshot, "persona_snapshot"),
    }
    return Record(
        characters.name,
        str(row.id),
        str(row.story_id),
        fields,
        defines=((SubjectKind.CHARACTER, str(row.id)),),
    )


def snapshot_record(row: Any) -> Record:
    """A catalog character's snapshot in a story (the version's whole content)."""
    return Record(
        character_snapshots.name,
        f"{row.story_id}:{row.character_id}",
        str(row.story_id),
        text_fields(row.content, "content"),
        ((SubjectKind.CHARACTER, str(row.character_id), "character_id"),),
    )


def story_record(row: Any) -> Record:
    """A story row: its name and the scenario snapshot it was created from."""
    return Record(
        stories.name,
        str(row.id),
        str(row.id),
        {"name": row.name, **text_fields(row.scenario_snapshot, "scenario_snapshot")},
        tuple(character_ids(row.scenario_snapshot, "scenario_snapshot")),
    )


def deferred_record(row: Any) -> Record:
    """A deferred genesis item (#122): the scenario's original JSON and the characters it
    resolved (``payload.characters``: role id to character id, as genesis writes it)."""
    payload = row.payload if isinstance(row.payload, Mapping) else {}
    named = payload.get("characters")
    subjects: list[Declared] = [
        (SubjectKind.CHARACTER, item, f"payload.characters.{role}")
        for role, item in (named.items() if isinstance(named, Mapping) else ())
        if isinstance(item, str)
    ]
    subjects.extend(character_ids(payload.get("value"), "payload.value"))
    return Record(
        genesis_deferred.name,
        str(row.id),
        str(row.story_id),
        text_fields(payload.get("value"), "payload.value"),
        tuple(subjects),
    )


def operation_record(row: Any) -> Record:
    """An operation's stored report (``turn_report``: a turn's, a genesis's creation report)."""
    story_id = _str(row.story_id)
    return Record(
        operations.name,
        str(row.operation_id),
        story_id,
        text_fields(row.turn_report, "turn_report"),
        tuple(character_ids(row.turn_report, "turn_report")) if story_id is not None else (),
    )


def policy_record(row: Any) -> Record:
    """A policy: its name, message and rule (a story's or a character's policy is a story
    record; a character policy declares its character)."""
    story_id = _str(row.story_id)
    subjects: tuple[Declared, ...] = ()
    if story_id is not None and row.scope == CHARACTER_SCOPE and row.scope_id is not None:
        subjects = ((SubjectKind.CHARACTER, str(row.scope_id), "scope_id"),)
    return Record(
        policies.name,
        str(row.id),
        story_id,
        {"name": row.name, "message": row.message, **text_fields(row.rule, "rule")},
        subjects,
    )


def config_change_record(row: Any) -> Record:
    """A configuration change: its old and new values (a story's change is a story record)."""
    story_id = _str(row.scope_id) if row.scope == STORY_SCOPE else None
    return Record(
        config_changes.name,
        str(row.change_id),
        story_id,
        {**text_fields(row.old, "old"), **text_fields(row.new, "new")},
    )


def rating_record_id(message_id: str, principal: str) -> str:
    """A rating's record id (#112): ``<message_id>:<principal>``, the same across re-ratings."""
    return f"{message_id}:{principal}"


def idempotency_id(scope: str, scope_id: str, key: str) -> str:
    return f"{scope}:{scope_id}:{key}"


def idempotency_record(row: Any) -> Record:
    """A stored idempotency result (#61): every text in it (a story key's is a story record)."""
    story_id = str(row.scope_id) if row.scope == STORY_SCOPE else None
    return Record(
        idempotency_keys.name,
        idempotency_id(str(row.scope), str(row.scope_id), str(row.key)),
        story_id,
        text_fields(row.result, "result"),
        tuple(character_ids(row.result, "result")) if story_id is not None else (),
    )


# -- propositions and the predicate registry (#131) --------------------------------------------


def predicate_words(name: str) -> str:
    """A predicate name as the matcher reads it: its ``_`` as spaces, so the words of a name an
    auto predicate took from a render ("visits_marisol") are whole words."""
    return name.replace("_", " ")


def predicate_record(row: Any) -> Record:
    """A story's predicate (an author's or an auto one): its name, as words, and its
    documentation."""
    return Record(
        predicates.name,
        str(row.id),
        _str(row.story_id),
        {"name": predicate_words(str(row.name)), "docs": row.docs},
    )


def proposal_record(row: Any) -> Record:
    """A predicate proposal: every text of what it proposes and of its decision."""
    return Record(
        predicate_proposals.name,
        str(row.id),
        _str(row.story_id),
        {**text_fields(row.proposed, "proposed"), **text_fields(row.decision, "decision")},
    )


def proposition_record(row: Any) -> Record:
    """A proposition: its literal's and its qualifiers' texts (its ids name entities)."""
    return Record(
        propositions.name,
        str(row.id),
        _str(row.story_id),
        {**text_fields(row.literal, "literal"), **text_fields(row.qualifiers, "qualifiers")},
    )


def closed_set_record(row: Any) -> Record:
    """A closed set: the texts of its key (the key qualifiers' values)."""
    return Record(closed_sets.name, str(row.id), _str(row.story_id), text_fields(row.key, "key"))


def entity_record(row: Any) -> Record:
    """An entity: its canonical name and its descriptors (#139), declaring the character it is
    (if any)."""
    subjects: tuple[Declared, ...] = ()
    if row.character_id is not None:
        subjects = ((SubjectKind.CHARACTER, str(row.character_id), "character_id"),)
    return Record(
        entities.name,
        str(row.id),
        _str(row.story_id),
        {
            "canonical_name": row.canonical_name,
            **text_fields(getattr(row, "descriptors", None) or [], "descriptors"),
        },
        subjects,
    )


def fact_record(row: Any) -> Record:
    """A fact version (#133): no text of its own (its proposition is indexed); the characters
    its ``characters`` visibility scope names are declared subjects."""
    visibility = row.visibility if isinstance(row.visibility, Mapping) else {}
    named = visibility.get("characters") or ()
    return Record(
        facts.name,
        str(row.id),
        _str(row.story_id),
        {},
        tuple(
            (SubjectKind.CHARACTER, str(c), "visibility.characters")
            for c in named
            if isinstance(c, str)
        ),
    )


def belief_record(row: Any) -> Record:
    """A belief (#159): no text of its own (its proposition is indexed); its holder and the
    characters its proposition's subject and object are (``subject_character_id``,
    ``object_character_id``, read through ``entities``) are declared subjects."""
    declared: list[Declared] = [
        (SubjectKind.CHARACTER, str(row.holder_character_id), "holder_character_id")
    ]
    for field in ("subject_character_id", "object_character_id"):
        found = getattr(row, field, None)
        if found is not None:
            declared.append((SubjectKind.CHARACTER, str(found), field))
    return Record(beliefs.name, str(row.id), _str(row.story_id), {}, tuple(declared))


def belief_transition_record(row: Any) -> Record:
    """A belief transition (#160): no text of its own; its holder and its source (who
    testified, what was observed of whom) are declared subjects."""
    declared: list[Declared] = [
        (SubjectKind.CHARACTER, str(row.holder_character_id), "holder_character_id")
    ]
    source = getattr(row, "source_character_id", None)
    if source is not None:
        declared.append((SubjectKind.CHARACTER, str(source), "source_character_id"))
    return Record(belief_transitions.name, str(row.id), _str(row.story_id), {}, tuple(declared))


def exposure_record(row: Any) -> Record:
    """An exposure (#164): no text of its own (its proposition is indexed); its witness, its
    source and the characters its proposition's subject and object are
    (``subject_character_id``, ``object_character_id``) are declared subjects."""
    declared: list[Declared] = [
        (SubjectKind.CHARACTER, str(row.witness_character_id), "witness_character_id")
    ]
    for field in ("source_character_id", "subject_character_id", "object_character_id"):
        found = getattr(row, field, None)
        if found is not None:
            declared.append((SubjectKind.CHARACTER, str(found), field))
    return Record(exposures.name, str(row.id), _str(row.story_id), {}, tuple(declared))


def thunk_record(row: Any) -> Record:
    """A thunk (#146): its ``proposition_sketch`` walked whole (the subject, relation and
    object texts and every candidate's text, positionally, ADR-154); its ids name entities and
    the predicate, never a person directly."""
    return Record(
        thunks.name,
        str(row.id),
        _str(row.story_id),
        text_fields(row.proposition_sketch, "proposition_sketch"),
    )


def memory_record(row: Any) -> Record:
    """A memory (#135): its ``text``, indexed positionally (ADR-154), and its
    ``quarantine_reason`` (#145, free text) when it has one; its holder a declared subject; its
    actor (the beat's character) its author."""
    holder = _str(row.holder_character_id)
    fields = {"text": row.text}
    reason = getattr(row, "quarantine_reason", None)
    if reason:
        fields["quarantine_reason"] = reason
    return Record(
        memories.name,
        str(row.id),
        _str(row.story_id),
        fields,
        ((SubjectKind.CHARACTER, holder, "holder_character_id"),) if holder else (),
        authored_by=_str(row.character_id),
    )


def alias_record(row: Any) -> Record:
    """An entity alias: its text."""
    return Record(entity_aliases.name, str(row.id), _str(row.story_id), {"alias": row.alias})


# -- catalog records ---------------------------------------------------------------------------


def character_draft_record(entry_id: str, document: Any) -> Record:
    """A catalog character's draft: it defines the subject; its whole document is text."""
    return Record(
        character_catalog.name,
        entry_id,
        None,
        text_fields(document),
        defines=((SubjectKind.CATALOG_CHARACTER, entry_id),),
    )


def character_version_record(entry_id: str, version: int, document: Any) -> Record:
    """A published catalog character version (``<id>@<version>``)."""
    return Record(
        character_versions.name,
        f"{entry_id}@{version}",
        None,
        text_fields(document),
        defines=((SubjectKind.CATALOG_CHARACTER, entry_id),),
    )


def persona_record(row: Any) -> Record:
    """A persona: it defines the subject; its name, gender and description are text."""
    return Record(
        personas.name,
        str(row.id),
        None,
        {"name": row.name, "gender": row.gender, "description": row.description},
        defines=((SubjectKind.PERSONA, str(row.id)),),
    )


def media_record(row: Any) -> Record:
    """A media asset (#105): its captions and prompts, and its declared subjects."""
    from epistrel.core.media import assets

    generation = row.generation_metadata if isinstance(row.generation_metadata, Mapping) else {}
    found = {
        "caption": row.caption,
        "alt_text": row.alt_text,
        "generation_metadata.prompt": _str(generation.get("prompt")),
        "generation_metadata.negative_prompt": _str(generation.get("negative_prompt")),
    }
    subjects: list[Declared] = []
    for item in row.depicted_subjects if isinstance(row.depicted_subjects, list) else []:
        if isinstance(item, Mapping) and isinstance(item.get("id"), str):
            subjects.append((SubjectKind(item["kind"]), item["id"], assets.DECLARED_FIELD))
    return Record(
        assets.RECORD_TABLE,
        str(row.id),
        None,
        {path: found[path] for path in assets.INDEXED_FIELDS},
        tuple(subjects),
    )


def upload_session_record(row: Any) -> Record:
    """A resumable upload session (#107, #334): the asset metadata it holds until the sweep
    deletes the closed row (captions, prompts and declared subjects, as ``media_record``)."""
    from epistrel.core.media import assets

    metadata = row.metadata if isinstance(row.metadata, Mapping) else {}
    generation = metadata.get("generation_metadata")
    generation = generation if isinstance(generation, Mapping) else {}
    found = {
        "caption": _str(metadata.get("caption")),
        "alt_text": _str(metadata.get("alt_text")),
        "generation_metadata.prompt": _str(generation.get("prompt")),
        "generation_metadata.negative_prompt": _str(generation.get("negative_prompt")),
    }
    subjects: list[Declared] = []
    depicted = metadata.get(assets.DECLARED_FIELD)
    for item in depicted if isinstance(depicted, list) else []:
        if isinstance(item, Mapping) and isinstance(item.get("id"), str):
            subjects.append(
                (SubjectKind(item["kind"]), item["id"], f"metadata.{assets.DECLARED_FIELD}")
            )
    return Record(
        media_upload_sessions.name,
        str(row.id),
        None,
        {f"metadata.{path}": found[path] for path in assets.INDEXED_FIELDS},
        tuple(subjects),
    )


# -- the readers -------------------------------------------------------------------------------


def _scoped(column: Any, story_id: str | None) -> Any:
    return column.is_(None) if story_id is None else column == story_id


async def read_events(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    found = await conn.execute(
        select(events.c.id, events.c.story_id, events.c.character_id, events.c.payload).where(
            _scoped(events.c.story_id, story_id)
        )
    )
    return [event_record(r) for r in found]


async def read_characters(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    if story_id is None:
        return []
    found = await conn.execute(select(characters).where(characters.c.story_id == story_id))
    return [character_record(r) for r in found]


async def read_snapshots(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    if story_id is None:
        return []
    found = await conn.execute(
        select(character_snapshots).where(character_snapshots.c.story_id == story_id)
    )
    return [snapshot_record(r) for r in found]


async def read_stories(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    if story_id is None:
        return []
    found = await conn.execute(
        select(stories.c.id, stories.c.name, stories.c.scenario_snapshot).where(
            stories.c.id == story_id
        )
    )
    return [story_record(r) for r in found]


async def read_deferred(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    if story_id is None:
        return []
    found = await conn.execute(
        select(genesis_deferred).where(genesis_deferred.c.story_id == story_id)
    )
    return [deferred_record(r) for r in found]


async def read_operations(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    found = await conn.execute(
        select(operations.c.operation_id, operations.c.story_id, operations.c.turn_report).where(
            _scoped(operations.c.story_id, story_id), operations.c.turn_report.is_not(None)
        )
    )
    return [operation_record(r) for r in found]


async def read_policies(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    found = await conn.execute(select(policies).where(_scoped(policies.c.story_id, story_id)))
    return [policy_record(r) for r in found]


async def read_ratings(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    from epistrel.core import feedback

    return await feedback.index_records_of(conn, story_id, history=False)


async def read_rating_history(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    from epistrel.core import feedback

    return await feedback.index_records_of(conn, story_id, history=True)


async def read_config_changes(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    where = (
        config_changes.c.scope != STORY_SCOPE
        if story_id is None
        else (config_changes.c.scope == STORY_SCOPE) & (config_changes.c.scope_id == story_id)
    )
    found = await conn.execute(select(config_changes).where(where))
    return [config_change_record(r) for r in found]


async def read_idempotency(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    k = idempotency_keys.c
    where = (
        k.scope != STORY_SCOPE
        if story_id is None
        else (k.scope == STORY_SCOPE) & (k.scope_id == story_id)
    )
    found = await conn.execute(
        select(k.scope, k.scope_id, k.key, k.result).where(where, k.result.is_not(None))
    )
    return [idempotency_record(r) for r in found]


async def read_character_drafts(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    if story_id is not None:
        return []
    found = await conn.execute(select(character_catalog.c.id, character_catalog.c.draft))
    return [character_draft_record(str(r.id), r.draft) for r in found]


async def read_character_versions(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    if story_id is not None:
        return []
    v = character_versions.c
    found = await conn.execute(select(v.catalog_id, v.version, v.content))
    return [character_version_record(str(r.catalog_id), int(r.version), r.content) for r in found]


async def read_scenario_drafts(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    if story_id is not None:
        return []
    from epistrel.core.catalog import scenarios

    found = await conn.execute(select(scenario_catalog.c.id, scenario_catalog.c.draft))
    return [scenarios.draft_record(str(r.id), r.draft) for r in found]


async def read_scenario_versions(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    if story_id is not None:
        return []
    from epistrel.core.catalog import scenarios

    v = scenario_versions.c
    found = await conn.execute(select(v.catalog_id, v.version, v.content, v.report))
    return [
        scenarios.version_record(str(r.catalog_id), int(r.version), r.content, r.report)
        for r in found
    ]


async def read_personas(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    if story_id is not None:
        return []
    found = await conn.execute(select(personas))
    return [persona_record(r) for r in found]


async def read_media(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    if story_id is not None:
        return []
    found = await conn.execute(select(media_assets))
    return [media_record(r) for r in found]


async def read_upload_sessions(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    if story_id is not None:
        return []
    found = await conn.execute(select(media_upload_sessions))
    return [upload_session_record(r) for r in found]


async def _story_rows(
    conn: AsyncConnection, table: Any, story_id: str | None, build: Any
) -> list[Record]:
    """Every row of a story-only table in the scope (the catalog scope has none)."""
    if story_id is None:
        return []
    found = await conn.execute(select(table).where(table.c.story_id == story_id))
    return [build(r) for r in found]


async def read_predicates(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    """A story's own predicates (the core vocabulary is the engine's text, never indexed)."""
    return await _story_rows(conn, predicates, story_id, predicate_record)


async def read_proposals(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    return await _story_rows(conn, predicate_proposals, story_id, proposal_record)


async def read_propositions(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    return await _story_rows(conn, propositions, story_id, proposition_record)


async def read_closed_sets(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    return await _story_rows(conn, closed_sets, story_id, closed_set_record)


async def read_entities(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    return await _story_rows(conn, entities, story_id, entity_record)


async def read_facts(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    return await _story_rows(conn, facts, story_id, fact_record)


async def read_beliefs(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    """A story's beliefs, as ``belief_record`` indexes them (each with the characters its
    proposition's subject and object are)."""
    if story_id is None:
        return []
    subject = entities.alias("subject_entity")
    obj = entities.alias("object_entity")
    b = beliefs.c
    found = await conn.execute(
        select(
            b.id,
            b.story_id,
            b.holder_character_id,
            subject.c.character_id.label("subject_character_id"),
            obj.c.character_id.label("object_character_id"),
        )
        .select_from(
            beliefs.join(propositions, propositions.c.id == b.proposition_id)
            .outerjoin(subject, subject.c.id == propositions.c.subject_id)
            .outerjoin(obj, obj.c.id == propositions.c.object_id)
        )
        .where(b.story_id == story_id)
    )
    return [belief_record(r) for r in found]


async def read_exposures(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    """A story's exposures, as ``exposure_record`` indexes them (each with the characters its
    proposition's subject and object are)."""
    if story_id is None:
        return []
    subject = entities.alias("subject_entity")
    obj = entities.alias("object_entity")
    x = exposures.c
    found = await conn.execute(
        select(
            x.id,
            x.story_id,
            x.witness_character_id,
            x.source_character_id,
            subject.c.character_id.label("subject_character_id"),
            obj.c.character_id.label("object_character_id"),
        )
        .select_from(
            exposures.join(propositions, propositions.c.id == x.proposition_id)
            .outerjoin(subject, subject.c.id == propositions.c.subject_id)
            .outerjoin(obj, obj.c.id == propositions.c.object_id)
        )
        .where(x.story_id == story_id)
    )
    return [exposure_record(r) for r in found]


async def read_belief_transitions(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    """A story's belief transitions, as ``belief_transition_record`` indexes them."""
    return await _story_rows(conn, belief_transitions, story_id, belief_transition_record)


async def read_thunks(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    """A story's thunks, as ``thunk_record`` indexes them."""
    return await _story_rows(conn, thunks, story_id, thunk_record)


async def read_memories(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    """A story's memories, as ``memory_record`` indexes them (the vector is not read)."""
    if story_id is None:
        return []
    m = memories.c
    found = await conn.execute(
        select(
            m.id, m.story_id, m.text, m.holder_character_id, m.character_id, m.quarantine_reason
        ).where(m.story_id == story_id)
    )
    return [memory_record(r) for r in found]


async def read_aliases(conn: AsyncConnection, story_id: str | None) -> list[Record]:
    return await _story_rows(conn, entity_aliases, story_id, alias_record)


async def index_rows(conn: AsyncConnection, table: Any, where: Any, build: Any) -> int:
    """Index the rows of ``table`` matching ``where`` with ``build`` (a ``*_record`` function):
    for a writer that changed rows in place by a statement."""
    from epistrel.core.erasure.index import index_records

    found = await conn.execute(select(table).where(where))
    return await index_records(conn, [build(r) for r in found])


__all__ = [
    "alias_record",
    "belief_record",
    "belief_transition_record",
    "character_draft_record",
    "character_ids",
    "character_record",
    "character_version_record",
    "closed_set_record",
    "config_change_record",
    "deferred_record",
    "entity_record",
    "event_record",
    "exposure_record",
    "fact_record",
    "idempotency_id",
    "idempotency_record",
    "index_rows",
    "media_record",
    "memory_record",
    "operation_record",
    "persona_record",
    "policy_record",
    "predicate_record",
    "predicate_words",
    "proposal_record",
    "proposition_record",
    "rating_record_id",
    "read_belief_transitions",
    "read_beliefs",
    "read_exposures",
    "read_thunks",
    "snapshot_record",
    "story_record",
    "text_fields",
    "thunk_record",
    "upload_session_record",
]
