"""Lazy character modeling's pure half (#163; ``core.epistemic.modeling``): the interaction
detector, the candidates a turn's beats yield, the weight rules and the sparsity test.

- interactions: a ``do`` with an interaction verb (directly, or through one of its
  prepositions) toward a description or a named entity; a narration whose subject acknowledges a
  cast member ("the neighbor waves at Jenni"); a ``say``'s vocative ("Thanks, driver."); never a
  verb inside the speech, a ``think``, or a mere mention;
- candidates: an interaction between a modeled character and an unmodeled person models the
  person (a description is minted under its phrase, an existing person entity is adopted), the
  salience step's ``characters`` model what a beat names, appearance and non-persons never do,
  and each entity is a candidate once;
- the minor window (``max(1, w // 5)``), the minor score factor in the ranking, the minor hot
  budget, the default render character skipping minors, and ``holds_memory``.
"""

from __future__ import annotations

from epistrel.core.epistemic import modeling
from epistrel.core.epistemic.modeling import BeatView, EntityInfo
from epistrel.core.memory.retirement import Budgets
from epistrel.core.resolution.types import Mention, MentionKind, Reason, ReasonCode
from epistrel.core.retrieval import rank
from epistrel.core.turns import CastEntry, StoryCast

JENNI = "01JCHAR000000000000000JENN"
WREN = "01JCHAR000000000000000WREN"
PATEL = "01JENTI000000000000000PATL"
LAMP = "01JENTI000000000000000LAMP"

ENTITIES = {
    JENNI: EntityInfo(JENNI, "person", "Jenni", character_id=JENNI),
    WREN: EntityInfo(WREN, "person", "Wren", character_id=WREN),
    PATEL: EntityInfo(PATEL, "person", "Mrs. Patel", names=("Patel",)),
    LAMP: EntityInfo(LAMP, "item", "the lamp"),
}


def _objects(kind: str, text: str, actor: str | None = JENNI) -> list[tuple[str | None, str]]:
    return [
        (hit.object.entity_id, hit.object.phrase)
        for hit in modeling.interactions(kind, text, actor=actor, entities=ENTITIES)
    ]


def test_interaction_verbs_find_the_other_party() -> None:
    assert _objects("do", "Jenni waves to the neighbor across the street.") == [
        (None, "the neighbor")
    ]
    assert _objects("do", "waves back at the jogger.") == [(None, "the jogger")]
    assert _objects("do", "She hands the delivery driver a tip.") == [(None, "the delivery driver")]
    assert _objects("do", "Jenni greets Mrs. Patel warmly.") == [(PATEL, "Mrs. Patel")]
    # A verb with no party after it, a non-person noun, a mere mention: no interaction.
    assert _objects("do", "Jenni waves.") == []
    assert _objects("do", "Jenni waves to the bus.") == []
    assert _objects("do", "Jenni watches a jogger run past.") == []
    assert _objects("think", "I should wave to the neighbor.") == []


def test_a_narration_names_its_subject() -> None:
    hits = modeling.interactions(
        "narration", "The neighbor waves at Jenni.", actor=None, entities=ENTITIES
    )
    assert [(h.subject and h.subject.phrase, h.object.entity_id) for h in hits] == [
        ("The neighbor", JENNI)
    ]


def test_a_say_interacts_by_vocative_only() -> None:
    assert _objects("say", "Thanks, driver.") == [(None, "driver")]
    assert _objects("say", "Hello, Mrs. Patel!") == [(PATEL, "Mrs. Patel")]
    # Words inside the speech are speech: "wave to the neighbor" is about the neighbor.
    assert _objects("say", "I will wave to the neighbor later.") == []
    assert modeling.is_interaction("say", "Thanks, driver.")
    assert not modeling.is_interaction("say", "The driver was late.")
    assert modeling.is_interaction("do", "She hands him the parcel.")


def test_candidates_model_the_unmodeled_side_of_an_interaction() -> None:
    beats = [
        BeatView("do", 2, "Jenni waves to the neighbor across the street.", JENNI),
        BeatView("do", 3, "Jenni watches a jogger run past.", JENNI),
        BeatView("narration", 4, "Mrs. Patel nods at Jenni."),
        BeatView("do", 5, "Jenni waves to the neighbor again.", JENNI),
        BeatView("do", 6, "Jenni hands the lamp to Wren.", JENNI),
    ]
    found = modeling.candidates(beats, ENTITIES)
    assert [(c.trigger, c.via, c.beat, c.name, c.entity_id) for c in found] == [
        ("interaction", "verb", 2, "the neighbor", None),
        ("interaction", "verb", 4, "Mrs. Patel", PATEL),
    ]
    assert found[0].definition == beats[0].text
    # A resolved mention counts as the party it names.
    mention = Mention((14, 24), MentionKind.NAME, PATEL, confidence=1.0)
    hit = modeling.candidates([BeatView("do", 2, "Jenni greets Mrs. Patel.", JENNI)], ENTITIES)
    assert [c.entity_id for c in hit] == [PATEL]
    assert mention.resolved


def test_an_uncertain_recurring_match_names_its_candidates() -> None:
    """#171 (carried from #163): "the driver" that the resolver found ambiguous between two
    modeled drivers is minted as a new character, its candidate naming both (an identity
    proposal each once it is modeled); an unmodeled candidate is not named."""
    first, second, other = (
        "01JCHAR00000000000000DRIV1",
        "01JCHAR00000000000000DRIV2",
        "01JENTI00000000000000DRIV3",
    )
    known = {
        **ENTITIES,
        first: EntityInfo(first, "person", "the delivery driver", character_id=first),
        second: EntityInfo(second, "person", "the bus driver", character_id=second),
        other: EntityInfo(other, "person", "the cab driver"),
    }
    text = "Jenni waves to the driver."
    start = text.index("the driver")
    doubt = Mention(
        (start, start + len("the driver")),
        MentionKind.DESCRIPTION,
        None,
        (first, second, other),
        0.0,
        Reason(ReasonCode.AMBIGUOUS, "descriptor"),
        "descriptor",
    )
    (found,) = modeling.candidates([BeatView("do", 2, text, JENNI, (doubt,))], known)
    assert (found.entity_id, found.name, found.uncertain_of) == (
        None,
        "the driver",
        (first, second),
    )
    # A resolved mention is that entity: nothing uncertain.
    sure = Mention(doubt.span, MentionKind.DESCRIPTION, other, (other,), 0.6, None, "descriptor")
    (found,) = modeling.candidates([BeatView("do", 2, text, JENNI, (sure,))], known)
    assert (found.entity_id, found.uncertain_of) == (other, ())


def test_salience_models_what_a_beat_names_and_nothing_else() -> None:
    beats = [BeatView("do", 2, "Jenni notices the old man on the bench.", JENNI)]
    found = modeling.candidates(
        beats, ENTITIES, salient=["the old man on the bench", "the mayor", "Jenni"]
    )
    assert [(c.trigger, c.name, c.beat) for c in found] == [
        ("salience", "the old man on the bench", 2)
    ]


def test_the_weight_rules() -> None:
    assert modeling.minor_window(50) == 10
    assert modeling.minor_window(4) == 1
    budgets = Budgets(2_000, 50_000, 0.2, 50, False, False, frozenset({PATEL}), 0.2)
    assert budgets.character_budget(PATEL) == 400
    assert budgets.character_budget(JENNI) == 2_000
    assert budgets.budget("character", PATEL) == 400
    ranked = rank.rank(
        [
            rank.Candidate("major", 1, 0.5, 1, 1, "a"),
            rank.Candidate("minor", 1, 0.5, 1, 1, "a", factor=modeling.MINOR_SCORE_FACTOR),
        ],
        {"major": 0.5, "minor": 0.5},
        now=2,
        weights=rank.Weights(),
        recency_half_life=100,
        importance_half_life=100,
    )
    assert [r.memory_id for r in ranked] == ["major", "minor"]
    assert ranked[1].score == ranked[0].score * modeling.MINOR_SCORE_FACTOR


def test_a_minor_is_never_the_default_render_character() -> None:
    from epistrel.core.settings_schema import StorySettings
    from epistrel.orchestrator.lite.schemas import RespondRequest
    from epistrel.orchestrator.structured.pipeline import resolve_structured_cast

    story = StoryCast(
        status="active",
        settings=StorySettings(),
        cast=(
            CastEntry(WREN, "Wren", "", True),
            CastEntry(PATEL, "the neighbor", "", False, minor=True),
            CastEntry(JENNI, "Jenni", "", False),
        ),
    )
    request = RespondRequest.model_validate(
        {"actor_character_id": WREN, "player_input": {"say": "Hi."}}
    )
    assert [c.id for c in resolve_structured_cast(story, request).render] == [JENNI]


def test_a_minor_holds_only_what_it_took_part_in() -> None:
    names = {JENNI: "Jenni", PATEL: "the neighbor"}

    def holds(text: str, *, kind: str = "do", content: bool = True, **kw: object) -> bool:
        return modeling.holds_memory(
            PATEL,
            actor=kw.get("actor", JENNI),  # type: ignore[arg-type]
            addressed=kw.get("addressed", ()),  # type: ignore[arg-type]
            participants=kw.get("participants", ()),  # type: ignore[arg-type]
            kind=kind,
            text=text,
            content=content,
            names=names,
        )

    assert holds("Jenni waves to the neighbor.")
    assert not holds("Jenni waves to the neighbor.", content=False)
    assert not holds("Jenni sips her coffee.")
    assert holds("I bring the parcel.", actor=PATEL)
    assert holds("Here you are.", kind="say", addressed=(PATEL,))
    assert holds("Jenni thanks the driver.", participants=(PATEL,))
    assert not holds("The neighbor was late again.", kind="say", participants=(PATEL,))
