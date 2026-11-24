"""A ``think`` that copies a ``## Canon`` line records its ``causes`` edge (#994, FR-TURN-8,
FR-DIRECT-7).

The render prompt shows no record ids, so a structured character render cites the text entries
its belief rests on. The commit resolves a cite that copies a line the prompt showed to the
record behind it (``prompt.cite_lines``, ``prompt.resolve_cites``): a ``## Canon`` line to its
fact, which ``edges.fact_edges`` records as the ``think``'s ``causes`` edge.

Bobby's education (``tests/fixtures/bobby_education.py``), played through ``respond`` with the
stub adapter. A first turn (Bobby nods) finds the ``## Canon`` line "OSU located_in Ohio" in
Bobby's prompt; a later turn has Bobby think, citing it. The cite is scripted by a stub parrot
over Bobby's own prompt, so it is the line that render's prompt showed. Complements record no
edge: a step number (``1``), a paraphrase of the line, and the text of a canon fact the prompt
did not show (Bobby's enrollment).
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import effect_support as es
from tests import multi_support as ms
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.fixtures.bobby_education import Education, bobby_education
from tests.respond_support import body, played, respond_client
from tests.structured_turn_support import beat

pytestmark = pytest.mark.integration

THINKS = "Bobby thinks of his old friends from OSU."
DECIDES = "Bobby decides to call his old roommate."
CANARY = "@@CITE@@"
ASK = "Bobby, where did you study? Was it OSU?"
OSU_IN_OHIO = "OSU located_in Ohio"
"""``osu_in_ohio``'s ``## Canon`` line (``retrieval.pipeline.canon_text``)."""
ENROLLED = "Bobby enrolled_at OSU"
"""``enrollment``'s line, were it shown: Bobby's prompt does not show it."""


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _causes(engine: AsyncEngine, story: str, turn: int) -> list[Any]:
    return await rows(
        engine,
        "SELECT target_kind::text AS target_kind, target_id FROM dependency_edges "
        "WHERE story_id = :s AND membership = 'active' AND edge_type = 'causes' "
        "AND dependent_table = 'events' AND narrative_turn = :t ORDER BY id",
        s=story,
        t=turn,
    )


def _canon_lines(prompt: str) -> list[str]:
    """The lines of a prompt's ``## Canon`` section, without their list markers."""
    section = prompt.split("## Canon\n", 1)[1].split("\n\n", 1)[0]
    return [line.removeprefix("- ") for line in section.splitlines() if line.startswith("- ")]


async def _turn(client: Any, stub: Any, w: Education, bobby: str | None = None) -> Any:
    """Cal asks where Bobby studied; Bobby renders ``bobby`` (when given), the narrator a
    crackle."""
    stub.reset()
    if bobby is not None:
        ms.renders_for(stub, "Bobby", bobby)
    es.narrator(stub, es.render(beat("narration", "The fire crackles.")))
    return await played(client, w.story_id, body(w.cal, ASK, render_character_ids=[w.bobby]))


def _cite_render(cite: str | None) -> str:
    """Bobby's render as a parrot template: a ``think`` citing ``cite`` (``None``: the parroted
    line), then a ``do`` (the JSON's own braces doubled for ``str.format``)."""
    answer = ms.render(beat("think", THINKS, cites=[cite or CANARY]), beat("do", DECIDES))
    return answer.replace("{", "{{").replace("}", "}}").replace(CANARY, "{canary}")


async def _shown_line(client: Any, stub: Any, w: Education) -> str:
    await _turn(client, stub, w, ms.render(beat("do", "Bobby nods.")))
    (prompt, *_) = ms.render_prompts(stub, "Bobby")
    lines = [line for line in _canon_lines(prompt) if line == OSU_IN_OHIO]
    assert len(lines) == 1, _canon_lines(prompt)
    return lines[0]


async def _cited(
    client: Any, stub: Any, engine: AsyncEngine, w: Education, *, line: str | None, cite: str
) -> tuple[str, list[Any]]:
    """A turn whose Bobby render thinks citing ``cite``, or, with ``line``, that line as
    Bobby's own prompt shows it (parroted, so only a line the prompt showed is cited); answers
    Bobby's prompt and the turn's beat ``causes`` edges."""
    stub.reset()
    es.narrator(stub, es.render(beat("narration", "The fire crackles.")))
    if line is not None:
        stub.parrot(
            rf"(?<=\n- ){re.escape(line)}(?=\n)", purpose="generation", template=_cite_render(None)
        )
    else:
        stub.parrot(r"## You are Bobby\n", purpose="generation", template=_cite_render(cite))
    turn = await played(client, w.story_id, body(w.cal, ASK, render_character_ids=[w.bobby]))
    (prompt, *_) = ms.render_prompts(stub, "Bobby")
    thought = await rows(
        engine,
        "SELECT payload FROM events WHERE story_id = :s AND narrative_turn = :t "
        "AND type = 'beat_think'",
        s=w.story_id,
        t=turn["turn"],
    )
    assert [r.payload.get("text") for r in thought] == [THINKS]
    return prompt, await _causes(engine, w.story_id, turn["turn"])


async def test_a_think_copying_a_canon_line_records_its_causes_edge(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        w = await bobby_education(client, engine)
        line = await _shown_line(client, stub, w)
        prompt, causes = await _cited(client, stub, engine, w, line=line, cite=line)
        assert f"- {line}\n" in prompt  # the cited line is the one this render's prompt showed
        assert [(e.target_kind, e.target_id) for e in causes] == [("fact", w.osu_in_ohio.id)]


@pytest.mark.parametrize("kind", ["step_number", "paraphrase", "not_shown"])
async def test_a_cite_matching_no_shown_line_records_no_edge(
    kind: str, stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        w = await bobby_education(client, engine)
        line = await _shown_line(client, stub, w)
        cite = {
            "step_number": "1",
            "paraphrase": f"I remember that {line}",
            "not_shown": ENROLLED,
        }[kind]
        prompt, causes = await _cited(client, stub, engine, w, line=None, cite=cite)
        # A canon fact's text the prompt did not show is not citable into an edge.
        assert f"- {ENROLLED}\n" not in prompt
        assert causes == []


def test_cite_render_template_round_trips() -> None:
    """The parrot template keeps the render's JSON once formatted."""
    found = json.loads(_cite_render(None).format(canary="Bobby enrolled_at OSU"))
    assert found["beats"][0]["cites"] == ["Bobby enrolled_at OSU"]
