"""S-CLOCK-BELIEF: characters know only the time they can know (docs/04 §7; FR-CLOCK-10,11,15,
FR-TURN-6), its M6 parts (#200).

Every S-CLOCK-BELIEF (M6) function lives in this file (traceability counts a test ID per
milestone in one file):

- ``test_s_clock_belief`` (Postgres, the structured pipeline with the stub; the clock in
  ``manual`` mode, ``time_awareness = exact``, per-character partitions, testimony at
  ``defaulted``, ``clock.time_claims`` on). Bobby's watch was seen working at 14:30 and stopped
  at 15:00, unnoticed: at 17:30 he reads three off it. He sincerely tells the player "it's only
  three, we've got time" → accepted as a sincere error (objectively false, belief-consistent),
  with no regeneration; his generation context showed 15:00 and never the objective 17:30
  (complement: the narrator's context names 17:30). Jenni deliberately tells Bobby "it's not
  even four" (barely ten past three) to keep him there → a lie (her own time belief contradicts
  it), its divergence recorded, and Bobby accepts it as testimony: his time belief is revised
  (basis ``testimony``). When he later checks the wall clock ("half past five", a ``do``: what
  is seen, objectively true), the observation revises his belief again, and his next context
  shows 17:30. The same scene under ``approximate`` (Jenni's context names the hour, never the
  minute; her lie is still a lie) and under ``none`` (she has no sense of the time, so her
  "about 3:10" is a guess she cannot know is false: a sincere error, not a lie; her context
  says she has no clear sense of the time; the guess writes no belief for her, and Bobby takes
  it as testimony). Complement: with the profile's ``clock.time_claims`` off, a character's
  time claim is checked as narration's is (against the clock, no belief dimension): Bobby's
  "it's only three" is contradicted and repaired despite his watch.
- ``test_s_clock_belief__no_leak`` (Hypothesis, pure): over random clocks, awareness levels,
  timepiece readings, unconscious spans and held schedules, the character prompt built from the
  time belief (``time_belief.derive`` → ``CharacterContext.time_belief`` →
  ``prompt.character_inputs`` → ``structured_generation.j2``) never contains the objective time
  where the belief differs from it, and an ``approximate`` belief never names a minute.
"""

from __future__ import annotations

import os
import re

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.epistemic.context import compose
from epistrel.core.propositions.vocabulary import TIME_IS
from epistrel.core.retrieval.filtered import _seal
from epistrel.core.settings_schema import CHARACTER_VIEWPOINT_PREFIX
from epistrel.core.turns import CastEntry
from epistrel.core.world_clock import time_belief
from epistrel.core.world_clock.anchor import Anchor, Statement
from epistrel.orchestrator.models import prompts
from epistrel.orchestrator.models.registry import GENERATION
from epistrel.orchestrator.structured.prompt import RenderSections, character_inputs
from epistrel.orchestrator.structured.render import STRUCTURED
from tests import effect_support as es
from tests import time_belief_support as tb
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import respond_client
from tests.structured_turn_support import beat, claim, turn_report

NIGHTLY = os.environ.get("EPISTREL_HYPOTHESIS_PROFILE") == "nightly"
EXAMPLES = 2_000 if NIGHTLY else 200
SENSE = "## Your sense of time"
BOBBY = "## You are Bobby\n"
JENNI = "## You are Jenni\n"


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _section(prompt: str) -> str:
    """The prompt's ``## Your sense of time`` section."""
    assert SENSE in prompt, prompt
    return prompt.split(SENSE, 1)[1].split("\n## ", 1)[0]


def _verdict(report: dict[str, object]) -> dict[str, object]:
    """The turn's one verdict on a character's own time claim (its belief dimension decided by
    its time belief: ``owner_ref`` ``time_belief:…``)."""
    verify = report["verify"]
    assert isinstance(verify, dict)
    found = [
        v
        for v in verify["verdicts"]
        if any(
            str(e.get("owner_ref", "")).startswith(time_belief.REF_PREFIX)
            for e in (v.get("speech") or {}).get("belief_evidence", ())
        )
    ]
    assert len(found) == 1, verify["verdicts"]
    return dict(found[0])


async def _scene(
    client: object, engine: AsyncEngine, awareness: str
) -> tuple[tb.Scene, dict[str, str]]:
    """The diner at 14:30: Bobby sees his watch working; at 17:30 he reads three off it (it
    stopped at 15:00, unnoticed)."""
    scene = await tb.Scene.open(
        client,  # type: ignore[arg-type]
        engine,
        "14:30",
        "Bobby",
        "Jenni",
        awareness=awareness,
        testimony="defaulted",
    )
    await scene.observe("Bobby", "14:30")
    await scene.at("17:30")
    await scene.observe("Bobby", "15:00")
    return scene, scene.ids


async def _divergences(engine: AsyncEngine, story: str, speaker: str) -> int:
    (row,) = await rows(
        engine,
        "SELECT count(*) AS n FROM divergences WHERE story_id = :s AND speaker_character_id = :c",
        s=story,
        c=speaker,
    )
    return int(row.n)


@pytest.mark.integration
@pytest.mark.part("M6", "FR-CLOCK-10,11,15; FR-TURN-6")
async def test_s_clock_belief(
    stub_settings: Settings,
    engine: AsyncEngine,
    time_claims_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        scene, ids = await _scene(client, engine, "exact")

        # "It's only three, we've got time": a sincere error, never repaired.
        found = await tb.play(
            scene,
            stub,
            "Rain streaks the diner window.",
            "Bobby",
            beat(
                "say",
                "It's only three, we've got time.",
                propositions=[claim("the diner", "time_is", "it's only three")],
            ),
            do="glances at the door",
        )
        verdict = _verdict(await turn_report(engine, scene.story))
        assert verdict["kind"] == "contradicted"  # objectively false: the clock says 17:30
        speech = verdict["speech"]
        assert isinstance(speech, dict)
        assert (speech["classification"], speech["belief_relation"]) == (
            "sincere_error",
            "consistent",
        )
        assert es.regenerations(stub) == []
        assert "It's only three, we've got time." in [
            b["text"] for b in found["beats"] if b["kind"] == "beat_say"
        ]
        # His context: his own sense of time, never the clock's 17:30.
        bobby = tb.prompt_of(stub, BOBBY)
        assert "15:00" in _section(bobby)
        assert "17:30" not in bobby
        # The director's context has the objective clock.
        assert "17:30" in tb.prompt_of(stub, es.NARRATOR_MARKER)

        # Jenni: "it's not even four" to keep him there: a lie; Bobby takes her word for it.
        await tb.play(
            scene,
            stub,
            None,
            "Jenni",
            beat(
                "say",
                "It's not even four, barely ten past three. Stay a while.",
                intent="deceptive",
                propositions=[claim("the diner", "time_is", "about 3:10")],
            ),
            say="Jenni, what time is it?",
            do=None,
        )
        verdict = _verdict(await turn_report(engine, scene.story))
        speech = verdict["speech"]
        assert isinstance(speech, dict)
        assert (speech["classification"], speech["belief_relation"]) == ("lie", "inconsistent")
        assert es.regenerations(stub) == []
        assert await _divergences(engine, scene.story, ids["Jenni"]) == 1
        held = await scene.belief("Bobby")
        assert held.basis == "testimony"
        assert held.estimate() == 15 * 60 + 10

        # The wall clock: what Bobby sees revises him (an observation), and his context follows.
        await tb.play(
            scene,
            stub,
            None,
            "Bobby",
            beat(
                "do",
                "Bobby glances at the wall clock: half past five.",
                propositions=[claim("the diner", "time_is", "half past five")],
            ),
            do="waits",
        )
        # What is seen is checked against the clock (true), not against his belief: no repair.
        verify = (await turn_report(engine, scene.story))["verify"]
        assert "contradicted" not in [v["kind"] for v in verify["verdicts"]]
        assert not [v for v in verify["verdicts"] if v.get("speech")]
        assert es.regenerations(stub) == []
        held = await scene.belief("Bobby")
        assert (held.basis, held.times) == ("observation", ((17 * 60 + 30, 17 * 60 + 31),))
        await tb.play(scene, stub, None, "Bobby", beat("say", "We should go."), do="stands")
        assert "17:30" in _section(tb.prompt_of(stub, BOBBY))

        # approximate: Jenni knows the hour, never the minute; her lie is still a lie.
        scene, ids = await _scene(client, engine, "approximate")
        await tb.play(
            scene,
            stub,
            None,
            "Jenni",
            beat(
                "say",
                "It's not even four.",
                intent="deceptive",
                propositions=[claim("the diner", "time_is", "about 3:10")],
            ),
            say="Jenni, what time is it?",
            do=None,
        )
        jenni = tb.prompt_of(stub, JENNI)
        assert "about 18:00" in _section(jenni)
        assert "17:30" not in jenni
        speech = _verdict(await turn_report(engine, scene.story))["speech"]
        assert isinstance(speech, dict)
        assert speech["classification"] == "lie"
        # Bobby's watch still reads three for him: the same sincere error.
        await tb.play(
            scene,
            stub,
            None,
            "Bobby",
            beat(
                "say",
                "It's only three.",
                propositions=[claim("the diner", "time_is", "it's only three")],
            ),
            say="Shouldn't we get going?",
            do=None,
        )
        speech = _verdict(await turn_report(engine, scene.story))["speech"]
        assert isinstance(speech, dict)
        assert speech["classification"] == "sincere_error"

        # none: Jenni has no sense of the time, so "about 3:10" is a guess she cannot know is
        # false: a sincere error, not a lie.
        scene, ids = await _scene(client, engine, "none")
        await tb.play(
            scene,
            stub,
            None,
            "Jenni",
            beat(
                "say",
                "It's not even four.",
                intent="deceptive",
                propositions=[claim("the diner", "time_is", "about 3:10")],
            ),
            say="Jenni, what time is it?",
            do=None,
        )
        jenni = tb.prompt_of(stub, JENNI)
        assert "no clear sense" in _section(jenni)
        assert "17:30" not in jenni
        speech = _verdict(await turn_report(engine, scene.story))["speech"]
        assert isinstance(speech, dict)
        assert (speech["classification"], speech["belief_relation"]) == (
            "sincere_error",
            "consistent",
        )
        assert await _divergences(engine, scene.story, ids["Jenni"]) == 0
        # A guess writes no belief for its speaker (FR-EPI-14); its hearer takes it as testimony.
        assert await _time_beliefs(engine, scene.story, ids["Jenni"]) == []
        assert await _time_beliefs(engine, scene.story, ids["Bobby"]) == ["testimony"]

        # Complement: with the profile's ``clock.time_claims`` off, a character's time claim is
        # checked as narration's is: against the clock alone, with no belief dimension, so
        # Bobby's stopped watch does not save his "it's only three": contradicted, repaired.
        scene = await tb.Scene.open(client, engine, "17:30", "Bobby", time_claims=False)
        await scene.observe("Bobby", "15:00")
        await tb.play(
            scene,
            stub,
            None,
            "Bobby",
            beat(
                "say",
                "It's only three.",
                propositions=[claim("the diner", "time_is", "it's only three")],
            ),
            say="Shouldn't we get going?",
            do=None,
            regenerated="We should think about going.",
        )
        verify = (await turn_report(engine, scene.story))["verify"]
        assert "contradicted" in [v["kind"] for v in verify["verdicts"]]
        assert not [v for v in verify["verdicts"] if v.get("speech")]
        assert len(es.regenerations(stub)) == 1


async def _time_beliefs(engine: AsyncEngine, story: str, holder: str) -> list[str]:
    """How ``holder`` came by each first-order ``time_is`` belief it has (transition kinds)."""
    found = await rows(
        engine,
        "SELECT t.kind::text AS kind FROM belief_transitions t JOIN beliefs b "
        "ON b.id = t.belief_id JOIN propositions p ON p.id = b.proposition_id "
        "WHERE t.story_id = :s AND t.holder_character_id = :h AND p.predicate_id = :p "
        "AND b.kind = 'first_order' ORDER BY t.id",
        s=story,
        h=holder,
        p=TIME_IS,
    )
    return [r.kind for r in found]


# -- the no-leak property ------------------------------------------------------------------------

AWARENESS = ("exact", "approximate", "none")
ME = CastEntry(
    id="01JC0000000000000000000001", name="Bobby", definition="A regular.", is_player=False
)
YOU = CastEntry(id="01JC0000000000000000000002", name="Cal", definition="", is_player=True)


@st.composite
def beliefs(draw: st.DrawFn) -> tuple[time_belief.TimeBelief, str]:
    """A random character's time belief and the objective time of day it is: an anchored
    clock, an awareness, maybe a timepiece read (or a time it was told) some minutes ago, maybe
    an unconscious span, and held schedules."""
    start = draw(st.integers(0, 1439))
    anchor = Anchor.unanchored().refine(Statement(time=((start, start + 1),)), 0)
    assert isinstance(anchor, Anchor)
    now = draw(st.integers(0, 2_000))
    awareness = draw(st.sampled_from(AWARENESS))
    reading = None
    if draw(st.booleans()):
        seen = draw(st.integers(0, now))
        minute = draw(st.integers(0, 1439))
        said = f"{minute // 60:02d}:{minute % 60:02d}"
        reading = time_belief.Reading(said, seen, told=draw(st.booleans()))
    asleep = None
    gaps: list[tuple[int, int]] = []
    if draw(st.booleans()) and now > 0:
        lo = draw(st.integers(0, now))
        hi = draw(st.integers(lo, now))
        gaps.append((lo, hi))
        asleep = lo
    schedules = tuple(
        time_belief.HeldSchedule(f"e{n}", f"x{n}", f"plan {n}", draw(st.integers(0, 3_000)))
        for n in range(draw(st.integers(0, 2)))
    )
    found = time_belief.derive(
        ME.id,
        awareness=awareness,
        anchor=anchor,
        now=now,
        gaps=gaps,
        asleep_from=asleep,
        reading=reading,
        schedules=schedules,
    )
    return found, anchor.clockwork.clock((start + now) % anchor.clockwork.day_minutes)


def _prompt(belief: time_belief.TimeBelief) -> str:
    viewpoint = f"{CHARACTER_VIEWPOINT_PREFIX}{ME.id}"
    context = compose(
        ME.id,
        working=_seal([], viewpoint),
        names={ME.id: ME.name},
        partitioned=True,
        time_belief=belief.text(),
    )
    inputs = character_inputs(context, RenderSections(clock_contract=True), ME, YOU, budget=8_000)
    return "\n".join(m.content for m in prompts.render(GENERATION, inputs, STRUCTURED))


def _derived(awareness: str, start: int, now: int) -> time_belief.TimeBelief:
    """The belief ``beliefs`` draws with no reading, gap or schedule: the clock anchored at
    ``start`` and read ``now`` minutes later."""
    anchor = Anchor.unanchored().refine(Statement(time=((start, start + 1),)), 0)
    assert isinstance(anchor, Anchor)
    return time_belief.derive(ME.id, awareness=awareness, anchor=anchor, now=now)


# #692 (and its duplicates #693, #701): ``approximate`` at 23:00 widens to a range wrapping
# midnight, ``((0, 1), (1320, 1440))``. Its one-minute ``(0, 1)`` piece once made the oracle
# read the belief as exactly 00:00, while the prompt (rightly) says "about 23:00".
WRAPPED = _derived("approximate", 23 * 60, 0)
WRAPPED_HERE = _derived("approximate", 0, 23 * 60)  # #693/#701's shrunk case: here ~23 hours


def _assert_no_leak(belief: time_belief.TimeBelief, objective: str, prompt: str) -> None:
    """The oracle: ``objective`` (``HH:MM``) is in ``prompt`` only where the belief holds that
    very minute as its context tells it (``TimeBelief.told``), or a schedule it holds falls
    on it as it believes it."""
    cw = belief.clockwork
    section = _section(prompt)
    _, minutes = belief.told()
    believed = {cw.clock(m) for m in minutes}
    if belief.basis in ("awareness", "gap") and not belief.told()[0]:
        assert all(m % 60 == 0 for m in minutes)
    planned = {s.split(": ", 1)[1].split(",", 1)[0] for s in belief.lines()[1:] if ": " in s}
    if objective not in believed | planned:
        assert objective not in prompt, (objective, section)
    # Only the time-belief section carries a time of day.
    assert prompt.count(SENSE) == 1
    assert set(re.findall(r"\b\d{2}:\d{2}\b", prompt)) == set(
        re.findall(r"\b\d{2}:\d{2}\b", section)
    )


@pytest.mark.part("M6", "FR-CLOCK-11")
@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(case=beliefs())
@example(case=(WRAPPED, "23:00"))
@example(case=(WRAPPED_HERE, "23:00"))
@example(case=(WRAPPED, "21:37"))  # complement: an objective minute it does not hold
def test_s_clock_belief__no_leak(case: tuple[time_belief.TimeBelief, str]) -> None:
    """See the module docstring: the objective time of day (``HH:MM``) is in the character's
    prompt only where its belief holds that very minute (or a schedule it holds falls on it, as
    it believes it)."""
    belief, objective = case
    _assert_no_leak(belief, objective, _prompt(belief))


def test_no_leak_oracle__wrapped_midnight_case() -> None:
    """#692's pinned case, read directly: the wrapped range is not exact, the prompt says
    "about 23:00", and the oracle counts 23:00 as believed (but never 21:37 or 00:00)."""
    assert WRAPPED.times == ((0, 1), (1320, 1440))
    assert WRAPPED.told() == (False, (23 * 60,))
    section = _section(_prompt(WRAPPED))
    assert "about 23:00 (night)" in section
    assert "00:00" not in section
    assert "21:37" not in section


def test_no_leak_oracle__catches_an_injected_objective() -> None:
    """Mutation: the objective time injected into the time-belief section fails the oracle."""
    prompt = _prompt(WRAPPED)
    mutated = prompt.replace(SENSE + "\n", f"{SENSE}\nThe clock says 21:37.\n", 1)
    assert mutated != prompt
    _assert_no_leak(WRAPPED, "21:37", prompt)
    with pytest.raises(AssertionError):
        _assert_no_leak(WRAPPED, "21:37", mutated)
