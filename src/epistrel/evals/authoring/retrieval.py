"""The retrieval sets (``evals/sets/retrieval/``; #157): Q-01's recall queries over the
S-THOSEGUYS-style fixture, Q-02's resolution cases and Q-03's abstract queries.

**The fixture** (``FIXTURE_VERSION``; built by ``suites.q01_recall.build_fixture``): a
5,000-turn story (Cal the player, Bobby, Ann; Dan, Tom, Maren, Raj, Jenni and Owen are people of
the story), with 5,000 filler episodes spread over the turns and 100 **occasions** planted among
them: each an event (a game, a party, a wedding…) at one turn with two or three participants,
told in two or three beats that all name the occasion's topic ("the runaway goose"). Every
planted beat has a stable builder key, ``occasion:<k>:<beat>``. ``OCCASIONS`` and
``FILLER_LINES`` are the fixture's content; the labels below are emitted from the same data, so
they are pinned to the fixture version.

- ``recall.jsonl`` (Q-01): per occasion, one S-THOSEGUYS-style query (its event, the day as
  "N days ago", one or two of its participants, sometimes its topic); relevant: the
  occasion's beats.
- ``resolution.jsonl`` (Q-02): #139's generator (``evals.resolution.generate``, seeds 0 and 1,
  60 queries each: S-THOSEGUYS generalised over six shapes), one query per case with its cast.
- ``abstract.jsonl`` (Q-03): per consolidation theme, four windows (the deeds of one character
  and a distractor) and the theme's abstract question, which only the reflection answers.
"""

from __future__ import annotations

from typing import Any

from epistrel.evals import resolution
from epistrel.evals.authoring import consolidation
from epistrel.evals.authoring.common import Chooser, case

FIXTURE_VERSION = "thoseguys-v1"
TURNS = 5_000
FILLERS = 5_000
CAST = ("Cal", "Bobby", "Ann")
PEOPLE = ("Dan", "Tom", "Maren", "Raj", "Jenni", "Owen")
EVERYONE = CAST + PEOPLE
ADJECTIVES = (
    "runaway", "broken", "cracked", "painted", "stolen", "flooded", "burnt", "golden", "muddy",
    "crooked",
)  # fmt: skip
NOUNS = (
    "goose", "fiddle", "bell", "cart", "barrel", "kite", "wagon", "scarecrow", "banner", "chimney",
)  # fmt: skip
EVENTS = ("game", "party", "wedding", "funeral", "birthday", "concert", "meeting", "fight")
"""Event types of the engine's vocabulary (``core/resolution/events.json``), so a query's event
word resolves to the occasion's type."""
BEAT_LINES = (
    "{p} says: I still can't believe {topic} at the {event}.",
    "{p} does: laughs about {topic} with {q}.",
    "{p} says: Nobody at the {event} will forget {topic}.",
)
QUERIES = (
    "Those guys at the {event} {ago}, with {p}: what about {topic}?",
    "What happened at the {event} {ago} with {p} and {q}?",
    "{ago_cap} at the {event}, {p} was there. Remember {topic}?",
    "Remember the {event} {ago}? {p} and {q} were both there.",
)
MINUTES_PER_TURN = 10
DAY_MINUTES = 1_440
FILLER_LINES = (
    "{a} says: The weather turned again.",
    "{a} does: stacks the plates by the sink.",
    "{a} says: We should fix the fence before winter.",
    "The wind rattles the shutters.",
    "{a} does: hums an old song.",
    "{a} says: Pass the salt, would you?",
    "{a} does: sweeps the yard.",
    "{a} says: The road was quiet today.",
)
FILLER_TYPES = ("walk", "work", None)
"""No filler shares an occasion's event type: a query names exactly one occasion."""
OCCASION_COUNT = 100


def occasions() -> list[dict[str, Any]]:
    """The planted occasions: key, topic, event, turn, participants and beats (text, actor)."""
    c = Chooser(f"retrieval:{FIXTURE_VERSION}:occasions")
    found = []
    taken: set[tuple[str, int]] = set()
    for k in range(OCCASION_COUNT):
        topic = f"the {ADJECTIVES[k // 10]} {NOUNS[k % 10]}"
        people = c.picks(EVERYONE, c.between(2, 3))
        turn = 20 + (k * (TURNS - 300)) // OCCASION_COUNT + c.between(0, 9)
        day = turn * MINUTES_PER_TURN // DAY_MINUTES
        # One occasion of a type a day: "the game 9 days ago" names exactly one.
        event = c.pick([e for e in EVENTS if (e, day) not in taken])
        taken.add((event, day))
        beats = []
        for b, p in enumerate(people):
            q = people[(b + 1) % len(people)]
            line = BEAT_LINES[b % len(BEAT_LINES)].format(p=p, q=q, topic=topic, event=event)
            beats.append({"key": f"occasion:{k}:{b + 1}", "text": line, "actor": p})
        found.append(
            {
                "key": f"occasion:{k}",
                "topic": topic,
                "event": event,
                "turn": turn,
                "participants": people,
                "beats": beats,
            }
        )
    return found


def days_ago(turn: int) -> str:
    """How a speaker at the head's next turn names the day of ``turn`` (``N days ago``; a turn
    is ten story-minutes, #131's M4 rule)."""
    now = (TURNS + 1) * MINUTES_PER_TURN // DAY_MINUTES
    days = now - (turn * MINUTES_PER_TURN) // DAY_MINUTES
    return "yesterday" if days == 1 else f"{days} days ago"


def recall_cases() -> list[dict[str, Any]]:
    """Q-01: one S-THOSEGUYS-style query per occasion (its event, its day, one or two of its
    participants, sometimes its topic); relevant = its beats."""
    c = Chooser(f"retrieval:{FIXTURE_VERSION}:queries")
    found = []
    for n, occasion in enumerate(occasions(), start=1):
        template = QUERIES[(n - 1) % len(QUERIES)]
        p, q = c.picks(occasion["participants"], 2)
        ago = days_ago(occasion["turn"])
        query = template.format(
            topic=occasion["topic"],
            event=occasion["event"],
            p=p,
            q=q,
            ago=ago,
            ago_cap=ago[0].upper() + ago[1:],
        )
        found.append(
            case(
                "recall",
                "occasion",
                n,
                origin="authored" if n <= len(QUERIES) else "paraphrase",
                inputs={"fixture": FIXTURE_VERSION, "query": query, "asker": "Cal"},
                expected={"relevant": [b["key"] for b in occasion["beats"]]},
            )
        )
    return found


def resolution_cases() -> list[dict[str, Any]]:
    """Q-02: #139's generated queries (seeds 0 and 1, 60 each), each with its cast."""
    found = []
    for seed in (0, 1):
        fixture = resolution.generate(seed, 60)
        entries = [
            {"id": e.id, "kind": e.kind, "name": e.canonical_name, "gender": e.gender}
            for e in fixture.entries
        ]
        relations = [[r.subject, r.predicate, r.object] for r in fixture.relations]
        for n, q in enumerate(fixture.queries, start=1):
            shape = ("those-guys", "pronoun", "ambiguous", "relational", "unknown-name",
                     "no-calendar")[(n - 1) % 6]  # fmt: skip
            found.append(
                case(
                    "resolution",
                    shape,
                    seed * 100 + n,
                    origin="whitepaper" if shape == "those-guys" and n == 1 else "paraphrase",
                    inputs={
                        "text": q.text,
                        "speaker": q.speaker,
                        "beats": [[b.text, b.actor] for b in q.beats],
                        "entries": entries,
                        "relations": relations,
                    },
                    expected={
                        "mentions": [[m.surface, m.entity_id] for m in q.mentions],
                        "time_rule": q.time_rule,
                        "event_type": q.event_type,
                    },
                )
            )
    return found


ABSTRACT_PER_THEME = 4


def abstract_cases() -> list[dict[str, Any]]:
    """Q-03: four windows per consolidation theme, with the theme's abstract question."""
    found = []
    for theme in consolidation.THEMES:
        c = Chooser(f"retrieval:abstract:{theme.family}")
        for n in range(1, ABSTRACT_PER_THEME + 1):
            built = consolidation.window(c, theme)
            found.append(
                case(
                    "abstract",
                    theme.family,
                    n,
                    origin="authored" if n == 1 else "paraphrase",
                    inputs={**built["inputs"], "question": theme.question},
                    expected={
                        "reflection": built["expected"]["entailed"],
                        "supports": built["expected"]["supports"],
                    },
                )
            )
    return found


__all__ = [
    "FILLERS",
    "FILLER_LINES",
    "FIXTURE_VERSION",
    "TURNS",
    "abstract_cases",
    "occasions",
    "recall_cases",
    "resolution_cases",
]
