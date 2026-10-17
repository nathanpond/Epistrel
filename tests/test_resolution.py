"""Unit tests of the resolution front end (#139; FR-PROP-6,9, FR-RET-2): the mention resolver,
the time grammar, the event-type vocabulary, the entity specs written at creation and Q-02's
metric. The milestone parts are ``tests/scenario/test_s_entity_identity.py`` (FR-PROP-6),
``test_u_15__resolution_time`` in ``tests/test_u_15.py`` (FR-PROP-9) and
``test_i_05__resolution`` in ``tests/integration/test_retrieval.py`` (FR-RET-2).

Model-call complement: every test here runs with the stub adapter instrumented to fail on any
call (``_no_model``); resolution never reaches it (and the ``resolution-calls-no-model`` import
contract keeps it so)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from epistrel.core.propositions.model import (
    Granularity,
    Instant,
    Interval,
    covers,
    story_minute,
)
from epistrel.core.resolution import events, time
from epistrel.core.resolution.entities import (
    NARRATOR,
    PLAYER,
    ContextBeat,
    EntityEntry,
    EntityIndex,
    Relation,
    ResolutionContext,
    character_spec,
    descriptors_of,
    normalize,
    resolve_mentions,
    resolve_reference,
    scenario_entity_specs,
)
from epistrel.core.resolution.time import (
    SubjectTimeline,
    TimeBasis,
    find_time_expressions,
    resolve_time,
    story_time_at,
)
from epistrel.core.resolution.types import (
    APOSTROPHE,
    MentionKind,
    Reason,
    ReasonCode,
    ResolvedTime,
    Uncertain,
)
from epistrel.evals import resolution as q02
from epistrel.orchestrator.models.stub import StubAdapter

BOBBY = "01JR0000000000000000000001"
TOM = "01JR0000000000000000000002"
JENNI = "01JR0000000000000000000003"
CAL = "01JR0000000000000000000004"
KITCHEN = "01JR0000000000000000000005"
LAMP = "01JR0000000000000000000006"
SMITH_SR = "01JR0000000000000000000007"

DAY = 1440


@pytest.fixture(autouse=True)
def _no_model(monkeypatch: pytest.MonkeyPatch) -> Iterator[StubAdapter]:
    """The stub adapter fails on any call while a test runs; none is ever made."""
    stub = StubAdapter()

    async def refuse(*args: Any, **kwargs: Any) -> Any:
        stub.calls.append(args[1] if len(args) > 1 else kwargs)  # type: ignore[arg-type]
        raise AssertionError("resolution called a model")

    monkeypatch.setattr(StubAdapter, "complete", refuse)
    monkeypatch.setattr(StubAdapter, "probe", refuse)
    yield stub
    assert stub.calls == []


def _index() -> EntityIndex:
    return EntityIndex(
        [
            EntityEntry(
                BOBBY,
                "person",
                "Bobby Smith",
                "masculine",
                ("tall", "beard"),
                BOBBY,
                (("Bob", "catalog_alias"), ("the stranger", "name_override")),
            ),
            EntityEntry(TOM, "person", "Tom", "masculine", character_id=TOM),
            EntityEntry(JENNI, "person", "Jenni", "feminine", ("red", "hair"), JENNI),
            EntityEntry(CAL, "person", "Cal", None, character_id=CAL),
            EntityEntry(KITCHEN, "place", "the kitchen", None, ("warm",)),
            EntityEntry(LAMP, "item", "the brass lamp", None, ("brass",)),
        ]
    )


def _context(**fields: Any) -> ResolutionContext:
    return ResolutionContext(_index(), **fields)


def _surfaces(text: str, speaker: str, context: ResolutionContext) -> list[tuple[str, Any]]:
    return [
        (text[m.span[0] : m.span[1]], m.entity_id) for m in resolve_mentions(text, speaker, context)
    ]


# -- names and aliases -----------------------------------------------------------------------


def test_names_resolve_exactly_case_folded_and_longest_first() -> None:
    context = _context()
    text = "BOBBY SMITH met bob and the stranger in The Kitchen; Bobby's lamp was the brass lamp."
    found = resolve_mentions(text, NARRATOR, context)
    surfaces = [(text[m.span[0] : m.span[1]], m.entity_id, m.kind, m.confidence) for m in found]
    assert surfaces == [
        ("BOBBY SMITH", BOBBY, MentionKind.NAME, 1.0),
        ("bob", BOBBY, MentionKind.ALIAS, 1.0),
        ("the stranger", BOBBY, MentionKind.ALIAS, 1.0),
        ("The Kitchen", KITCHEN, MentionKind.NAME, 1.0),
        ("Bobby", BOBBY, MentionKind.NAME, 1.0),
        ("lamp", LAMP, MentionKind.NAME, 1.0),
        ("the brass lamp", LAMP, MentionKind.NAME, 1.0),
    ]
    # A single word of a multi-word name is an implicit alias; whole words only.
    assert _surfaces("Then Smith left; Bobbyx stayed.", NARRATOR, context)[0] == ("Smith", BOBBY)
    assert normalize(f"Jenni{APOSTROPHE}s") == normalize("JENNI's") == "jenni"


def test_a_shared_name_is_ambiguous_never_guessed() -> None:
    index = EntityIndex(
        [
            EntityEntry(BOBBY, "person", "Bobby Smith", "masculine"),
            EntityEntry(SMITH_SR, "person", "Old Smith", "masculine"),
        ]
    )
    (mention,) = resolve_mentions("Then Smith left.", NARRATOR, ResolutionContext(index))
    assert mention.entity_id is None
    assert mention.candidates == tuple(sorted((BOBBY, SMITH_SR)))
    uncertain = mention.uncertain()
    assert uncertain is not None
    assert uncertain.reason.code is ReasonCode.AMBIGUOUS
    assert uncertain.reason.gate == "unresolved_entity"
    # Complement: an explicit alias outranks another entity's implicit one-word token.
    index = EntityIndex(
        [
            EntityEntry(BOBBY, "person", "Bobby Smith", "masculine"),
            EntityEntry(SMITH_SR, "person", "Smith", "masculine"),
        ]
    )
    (mention,) = resolve_mentions("Then Smith left.", NARRATOR, ResolutionContext(index))
    assert mention.entity_id == SMITH_SR


def test_an_unknown_name_is_uncertain() -> None:
    found = resolve_mentions("Then Zed Quill waved at Tom.", NARRATOR, _context())
    assert [(m.entity_id, m.reason and m.reason.code) for m in found] == [
        (None, ReasonCode.UNKNOWN_NAME),
        (TOM, None),
    ]
    # Sentence-initial, calendar and stop words are never unknown names.
    text = "Yesterday it rained. Then in May it snowed, and on Monday The Kitchen flooded."
    found = resolve_mentions(text, NARRATOR, _context())
    assert all(m.reason is None or m.reason.code is not ReasonCode.UNKNOWN_NAME for m in found)


# -- persons and pronouns --------------------------------------------------------------------


def test_first_and_second_person() -> None:
    context = _context(present=frozenset({JENNI, BOBBY}), player_id=CAL)
    assert _surfaces("I told you.", JENNI, context) == [("I", JENNI), ("you", BOBBY)]
    # The narrator has no first person; its "you" is the player character.
    found = resolve_mentions("I see you.", NARRATOR, context)
    assert [(m.entity_id, m.reason and m.reason.code) for m in found] == [
        (None, ReasonCode.NO_ANTECEDENT),
        (CAL, None),
    ]
    assert _surfaces("me", PLAYER, context) == [("me", CAL)]
    # Complement: with two others present, "you" is ambiguous.
    crowded = _context(present=frozenset({JENNI, BOBBY, TOM}))
    (you,) = resolve_mentions("you", JENNI, crowded)
    assert you.entity_id is None
    assert you.candidates == tuple(sorted((BOBBY, TOM)))
    # With no scene, the context window's other actor is the addressee.
    beats = [ContextBeat("Hi.", TOM), ContextBeat("Hello.", JENNI)]
    assert _surfaces("Are you well?", JENNI, _context(beats=beats)) == [("you", TOM)]


def test_pronouns_bind_to_the_nearest_compatible_entity() -> None:
    context = _context()
    # Same sentence, gender-compatible: one candidate.
    assert _surfaces("Then Jenni met Tom and he smiled.", NARRATOR, context)[-1] == ("he", TOM)
    # An earlier sentence; a pronoun's entity is a later pronoun's antecedent.
    text = "Then Jenni came in. She saw the lamp. It was warm. Her hair was red."
    assert [e for _, e in _surfaces(text, NARRATOR, context)] == [
        JENNI,
        JENNI,
        LAMP,
        LAMP,
        JENNI,
    ]
    # The context window's beats, most recent first: a beat's actor and the names it uses.
    beats = [ContextBeat("Tom waved.", None), ContextBeat("Jenni laughed.", JENNI)]
    assert _surfaces("He left.", NARRATOR, _context(beats=beats)) == [("He", TOM)]
    # An entity with no gender is compatible with either; "they" with anything.
    assert _surfaces("Then Cal came in. She sat.", NARRATOR, context)[-1] == ("She", CAL)
    assert _surfaces("Then Tom came in. They sat.", NARRATOR, context)[-1] == ("They", TOM)


def test_two_compatible_antecedents_are_uncertain() -> None:
    """S-ENTITY-IDENTITY's shape: "he said it" with two candidate referents."""
    found = resolve_mentions("Then Bobby told Tom he said it.", JENNI, _context())
    he = found[2]
    assert (he.kind, he.entity_id) == (MentionKind.PRONOUN, None)
    assert he.candidates == tuple(sorted((BOBBY, TOM)))
    assert he.reason == Reason(ReasonCode.AMBIGUOUS, "pronoun")
    # Two compatible in one beat is ambiguous too.
    beats = [ContextBeat("Bobby argued with Tom.", None)]
    (he,) = resolve_mentions("He left.", NARRATOR, _context(beats=beats))
    assert he.entity_id is None
    assert he.reason is not None
    # Complement: the speaker is never a third person's antecedent, so one candidate is left.
    (he,) = resolve_mentions("He left.", TOM, _context(beats=beats))
    assert he.entity_id == BOBBY


def test_no_antecedent_and_gender_mismatch() -> None:
    (she,) = resolve_mentions("She left.", NARRATOR, _context())
    assert she.reason == Reason(ReasonCode.NO_ANTECEDENT, "pronoun")
    found = resolve_mentions("Then Tom came in. She left.", NARRATOR, _context())
    assert found[1].reason == Reason(ReasonCode.GENDER_MISMATCH, "pronoun")
    assert found[1].uncertain() == Uncertain(Reason(ReasonCode.GENDER_MISMATCH, "pronoun"))


# -- descriptions, relations, groups ---------------------------------------------------------


def test_descriptions_match_descriptors_and_ties_are_ambiguous() -> None:
    context = _context()
    found = resolve_mentions("Then the tall man nodded.", NARRATOR, context)
    assert [(m.entity_id, m.kind, m.confidence) for m in found] == [
        (BOBBY, MentionKind.DESCRIPTION, 0.6)
    ]
    assert _surfaces("Then the woman smiled.", NARRATOR, context) == [("the woman", JENNI)]
    # Two men match "the man" equally: ambiguous.
    (man,) = resolve_mentions("Then the man left.", NARRATOR, context)
    assert man.entity_id is None
    assert man.candidates == tuple(sorted((BOBBY, TOM)))
    # A definite noun phrase that matches no descriptor is no mention.
    assert resolve_mentions("Then the door opened.", NARRATOR, context) == []


def test_relational_descriptions_follow_one_relationship() -> None:
    relations = [
        Relation(JENNI, "sibling_of", BOBBY),
        Relation(TOM, "parent_of", BOBBY),
    ]
    context = _context(relations=relations)
    assert _surfaces("Then Bobby's sister came.", NARRATOR, context) == [
        ("Bobby", BOBBY),
        ("sister", JENNI),
    ]
    text = "Then Bobby came with his father."
    assert _surfaces(text, NARRATOR, context)[-1] == ("father", TOM)
    assert _surfaces("My dad is late.", BOBBY, context) == [("My", BOBBY), ("dad", TOM)]
    # Complement: no such relationship, or the wrong gender, is uncertain.
    found = resolve_mentions("Then Bobby's brother came.", NARRATOR, context)
    assert found[-1].entity_id is None
    assert found[-1].reason == Reason(ReasonCode.UNRESOLVED_ENTITY, "relation")


def test_group_mentions_name_no_entity() -> None:
    found = resolve_mentions("We saw those guys at the game.", JENNI, _context())
    assert [(m.kind, m.entity_id, m.reason, m.uncertain()) for m in found] == [
        (MentionKind.GROUP, None, None, None),
        (MentionKind.GROUP, None, None, None),
    ]


def test_resolution_is_deterministic_and_serializable() -> None:
    text = "Then Bobby told Tom he said it. I saw you."
    context = _context(present=frozenset({BOBBY, JENNI}))
    first = resolve_mentions(text, JENNI, context)
    assert first == resolve_mentions(text, JENNI, _context(present=frozenset({BOBBY, JENNI})))
    payload = [m.as_payload() for m in first]
    assert {"span", "kind", "entity_id", "candidates", "confidence", "reason", "rule"} == set(
        payload[0]
    )
    assert all("Bobby" not in str(p) for p in payload)  # spans and ids, never story text
    reference = resolve_reference("the tall man", NARRATOR, context)
    assert reference.entity_id == BOBBY
    nothing = resolve_reference("someone", NARRATOR, context)
    assert nothing.reason == Reason(ReasonCode.UNRESOLVED_ENTITY, "no_mention")


# -- the entity specs ------------------------------------------------------------------------


def test_character_specs_carry_aliases_gender_and_descriptors() -> None:
    content = {
        "definition": {"appearance": "A tall woman with copper hair and a scar."},
        "metadata": {"aliases": ["Lin", "the Captain"]},
    }
    spec = character_spec(
        JENNI,
        "Linda",
        catalog_content=content,
        name_overrides={"canonical": "Captain Linda", "aliases": ["Cap"]},
    )
    assert spec.entity_id == JENNI
    assert spec.kind == "person"
    assert spec.aliases == (
        ("Lin", "catalog_alias"),
        ("the Captain", "catalog_alias"),
        ("Captain Linda", "name_override"),
        ("Cap", "name_override"),
    )
    assert spec.gender == "feminine"
    assert spec.descriptors == ("tall", "woman", "copper", "hair", "scar")
    persona = character_spec(CAL, "Cal", persona={"name": "Calvin", "gender": "Man"})
    assert (persona.aliases, persona.gender) == ((("Calvin", "persona"),), "masculine")
    # Complement: no persona gender and an appearance naming both, or none, is no gender.
    assert character_spec(TOM, "Tom").gender is None
    both = {"definition": {"appearance": "A man and a woman in one coat."}}
    assert character_spec(TOM, "Tom", catalog_content=both).gender is None
    assert character_spec(TOM, "Tom", persona={"name": "T", "gender": "nonbinary"}).gender is None


def test_scenario_entities_keep_their_local_ids_as_aliases() -> None:
    world = {
        "locations": [{"id": "old-mill", "name": "The Old Mill", "description": "A ruined mill."}],
        "entities": [
            {
                "id": "guild",
                "name": "The Guild",
                "aliases": ["the Brothers"],
                "kind": "organization",
            },
            {"id": "relic", "name": "The Relic"},
        ],
    }
    specs = scenario_entity_specs(world)
    assert [(s.kind, s.canonical_name, s.aliases) for s in specs] == [
        ("place", "The Old Mill", (("old mill", "scenario_entity"),)),
        (
            "organization",
            "The Guild",
            (("guild", "scenario_entity"), ("the Brothers", "scenario_entity")),
        ),
        ("other", "The Relic", (("relic", "scenario_entity"),)),
    ]
    assert specs[0].descriptors == ("ruined", "mill")
    assert descriptors_of(None) == ()


# -- story-time and the time grammar ---------------------------------------------------------


EPOCH = datetime(2020, 6, 15, 12, 0, tzinfo=UTC)
BASIS = TimeBasis(minute_zero=0, epoch=EPOCH)
UNANCHORED = TimeBasis(minute_zero=1_000)


def test_story_time_at_a_position_is_the_m4_rule() -> None:
    assert story_time_at(0, UNANCHORED) == 1_000
    assert story_time_at((3, 2), UNANCHORED) == 1_030
    assert story_time_at(3, TimeBasis(minute_zero=5, minutes_per_turn=60)) == 185
    now = resolve_time("now", (3, 1), basis=UNANCHORED)
    assert isinstance(now, ResolvedTime)
    assert now.valid_time == Instant.of(1_030, Granularity.MINUTE)


def _resolved(expression: str, position: int = 0, **kw: Any) -> ResolvedTime:
    found = resolve_time(expression, position, basis=kw.pop("basis", BASIS), **kw)
    assert isinstance(found, ResolvedTime), (expression, found)
    return found


def test_relative_expressions() -> None:
    now = story_time_at(0, BASIS)
    yesterday = _resolved("yesterday")
    assert yesterday.valid_time == Instant.of(now - DAY, Granularity.DAY, EPOCH)
    assert (yesterday.direction, yesterday.approximate) == ("past", False)
    last_week = _resolved("we met last week")
    assert last_week.valid_time.possible() == (now - 14 * DAY, now - 7 * DAY)
    assert last_week.approximate
    assert last_week.direction == "past"
    month = _resolved("a month ago")
    assert month.valid_time.possible() == (now - 37 * DAY, now - 23 * DAY)
    three = _resolved("three hours ago")
    assert three.valid_time == Instant.of(now - 180, Granularity.HOUR, EPOCH)
    later = _resolved("in two days")
    assert later.direction == "future"
    assert later.valid_time == Instant.of(now + 2 * DAY, Granularity.DAY, EPOCH)
    morning = _resolved("this morning")
    start = story_minute(datetime(2020, 6, 15, tzinfo=UTC), EPOCH)
    assert morning.valid_time == Interval.of(start + 6 * 60, start + 12 * 60)
    other = _resolved("the other day")
    assert other.valid_time.possible() == (now - 4 * DAY, now - DAY)
    assert _resolved("recently").valid_time.possible() == (now - 7 * DAY, now)
    # Offsets resolve without a calendar anchor too.
    assert isinstance(resolve_time("last week", 0, basis=UNANCHORED), ResolvedTime)


def test_calendar_expressions_need_the_anchor() -> None:
    year = _resolved("back in 2012")
    assert year.valid_time == Instant.of(
        story_minute(datetime(2012, 1, 1, tzinfo=UTC), EPOCH), Granularity.YEAR, EPOCH
    )
    may = _resolved("on May 10")
    assert may.valid_time == Instant.of(
        story_minute(datetime(2020, 5, 10, tzinfo=UTC), EPOCH), Granularity.DAY, EPOCH
    )
    summer = _resolved("last summer")
    assert summer.valid_time == Interval.of(
        story_minute(datetime(2019, 6, 1, tzinfo=UTC), EPOCH),
        story_minute(datetime(2019, 9, 1, tzinfo=UTC), EPOCH),
    )
    monday = _resolved("last Monday")
    assert monday.valid_time == Instant.of(
        story_minute(datetime(2020, 6, 8, tzinfo=UTC), EPOCH), Granularity.DAY, EPOCH
    )
    for expression in ("in 2012", "on May 10", "last summer", "last Monday"):
        found = resolve_time(expression, 0, basis=UNANCHORED)
        assert isinstance(found, Uncertain)
        assert found.reason.code is ReasonCode.NO_CALENDAR
        assert found.reason.gate == "unresolved_time"


def test_ages_anchor_on_the_subjects_timeline() -> None:
    year = time.YEAR
    born = Instant.of(0, Granularity.DAY)
    seven = _resolved("when he was seven", subject=SubjectTimeline(born=born))
    assert seven.valid_time == Interval.of(7 * year, 8 * year)
    # A coarse birth (a year) or an age fact is approximate: +/- 1 year.
    coarse = _resolved(
        "at the age of 7", subject=SubjectTimeline(born=Instant.of(0, Granularity.YEAR))
    )
    assert coarse.valid_time.possible() == (6 * year, 9 * year)
    aged = SubjectTimeline(ages=((40, Interval.of(40 * year, None)),))
    assert _resolved("when she was 7", subject=aged).valid_time.possible() == (6 * year, 9 * year)
    # Complement: no anchor is uncertain, never a default date.
    for subject in (None, SubjectTimeline()):
        found = resolve_time("when he was seven", 0, subject, basis=BASIS)
        assert found == Uncertain(Reason(ReasonCode.NO_ANCHOR, "age_when"))


def test_unresolvable_expressions_are_uncertain() -> None:
    for text in ("once upon a time", "", "on the blue moon"):
        assert resolve_time(text, 0, basis=BASIS) == Uncertain(
            Reason(ReasonCode.UNRESOLVED_TIME, "no_rule")
        )


def test_time_expressions_are_found_longest_first() -> None:
    text = "Those guys a month ago at the game, yesterday morning and 3 days from now."
    assert [(e.rule, e.text) for e in find_time_expressions(text)] == [
        ("ago", "a month ago"),
        ("time_of_day", "yesterday morning"),
        ("from_now", "3 days from now"),
    ]
    grammar = time.grammar()
    assert grammar.raw["language"] == "en"
    assert "coverage" in grammar.raw
    assert time.calendar_words() >= {"may", "monday", "summer"}


def test_a_coarse_claim_covers_nothing_finer() -> None:
    """An approximate "last week" covers a day inside its inner bounds; complement: not a day
    only its outer bounds reach (the expression is coarse, never stretched into a fact)."""
    now = story_time_at(0, BASIS)
    week = _resolved("last week").valid_time
    assert covers(week, Instant.of(now - 10 * DAY, Granularity.DAY, EPOCH)) is True
    assert covers(week, Instant.of(now - 14 * DAY, Granularity.DAY, EPOCH)) is not True


# -- event types -----------------------------------------------------------------------------


def test_event_types() -> None:
    assert events.resolve_event_type("Those guys at the game last week") == "game"
    assert events.resolve_event_type("we had DINNER after the fight") == "dinner"
    assert events.resolve_event_type("a road trip and a party") == "trip"
    assert events.resolve_event_type("the gamekeeper slept") is None
    assert events.resolve_event_type("the joust", extra=["joust"]) == "joust"
    assert 35 <= len(events.event_types()) <= 50
    missing = events.event_type_or_uncertain("nothing happened")
    assert isinstance(missing, Uncertain)
    assert missing.reason.code is ReasonCode.UNRESOLVED_EVENT_TYPE
    assert missing.reason.gate == "unresolved_event_type"


# -- Q-02 ------------------------------------------------------------------------------------


def test_q_02_metric_and_fixture() -> None:
    """Q-02 (report-only): the generated S-THOSEGUYS fixture is deterministic, mixes resolvable
    and uncertain mentions, and scores per mention and per query."""
    fixture = q02.generate(seed=7, n=30)
    assert fixture == q02.generate(seed=7, n=30)
    expected = [m.entity_id for q in fixture.queries for m in q.mentions]
    assert None in expected
    assert any(expected)
    accuracy = q02.accuracy(q02.run(fixture))
    assert (accuracy.queries, accuracy.mentions) == (30, len(expected))
    assert accuracy.per_mention == 1.0
    assert accuracy.per_query == 1.0
    # The metric scores a miss: one wrong expectation is one wrong mention, one wrong query.
    first = fixture.queries[1]
    wrong = q02.Query(
        first.id,
        first.text,
        first.speaker,
        first.beats,
        (q02.Expected(first.mentions[0].surface, TOM), *first.mentions[1:]),
        first.time_rule,
        first.event_type,
    )
    broken = q02.Fixture(fixture.entries, fixture.relations, (wrong,))
    scored = q02.accuracy(q02.run(broken))
    assert (scored.mentions_right, scored.mentions, scored.queries_all_correct) == (
        len(wrong.mentions) - 1,
        len(wrong.mentions),
        0,
    )


def test_q_02_runs_through_the_retrieval_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """#141: Q-02 resolves every query through the retrieval pipeline's resolve stage
    (``core.retrieval.pipeline.resolve_query``, the function ``retrieve`` runs first), not
    through the resolvers on their own: one call per query, with the scores unchanged."""
    from epistrel.core.retrieval import pipeline

    seen: list[str] = []
    original = pipeline.resolve_query

    def spy(text: str, **kw: Any) -> pipeline.QueryResolution:
        seen.append(text)
        return original(text, **kw)

    monkeypatch.setattr(pipeline, "resolve_query", spy)
    fixture = q02.generate(seed=3, n=12)
    accuracy = q02.accuracy(q02.run(fixture))
    assert seen == [q.text for q in fixture.queries]
    assert (accuracy.per_mention, accuracy.per_query) == (1.0, 1.0)
