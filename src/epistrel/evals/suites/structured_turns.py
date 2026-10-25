"""The structured turn E-06 and E-07 play (#157): a scratch story of Wren (the player) and Maren
(the innkeeper) on ``Structured``, one ``Engine.respond`` with the player's line, read at
the omniscient viewpoint so every channel (say, think, do, latent) is visible.

``structure_problems(result)`` is FR-TURN-1's check on what the turn released: at least one
rendered beat, every beat on a known channel with text, at least one ``say`` or ``do``, a
non-empty message, and no safe fallback (a render the output gate replaced is not the
model's structure).

``render_output(ctx, result)`` is what the turn's render call answered (#464): each rendered
beat's text by beat, a beat #151's ladder repaired by its original text (its
``render_rejected`` event, attempt 0), never the composed message.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from epistrel.core.stories import CharacterSpec, StorySpec
from epistrel.core.tables import events
from epistrel.evals import scenes
from epistrel.orchestrator.lite.schemas import SAFE_FALLBACK, RespondRequest, RespondResult

if TYPE_CHECKING:
    from epistrel.evals.runner import CaseContext

PLAYER = "Wren"
INNKEEPER = "Maren"
CAST = (
    CharacterSpec(name=PLAYER, definition="A traveller, curious.", true_age=29, is_player=True),
    CharacterSpec(
        name=INNKEEPER,
        definition="The innkeeper: warm, plain-spoken, glad to help a guest.",
        true_age=46,
    ),
)
CHANNELS = frozenset({"say", "think", "do", "latent"})
RENDER_KINDS = frozenset({"beat_say", "beat_think", "beat_do", "beat_latent"})


async def play(
    ctx: CaseContext, say: str, prompt_params: dict[str, Any] | None = None
) -> RespondResult:
    """One structured turn of a new scratch story (see the module docstring)."""
    created = await ctx.story(StorySpec(characters=list(CAST)))
    await scenes.structured(ctx.engine, ctx.principal, created.story_id)
    actor = next(c.id for c in created.characters if c.name == PLAYER)
    request = RespondRequest.model_validate(
        {
            "actor_character_id": actor,
            "player_input": {"say": say},
            "output_viewpoint": "omniscient",
            **({"prompt_params": prompt_params} if prompt_params else {}),
        }
    )
    return await ctx.engine.respond(ctx.principal, created.story_id, request)


def rendered(result: RespondResult) -> list[Any]:
    """The turn's rendered beats (not the player's input, not the composed message)."""
    return [b for b in result.beats if b.kind in RENDER_KINDS]


def structure_problems(result: RespondResult) -> list[str]:
    """Why the released turn is not a valid say/think/do/latent structure (empty: it is)."""
    found: list[str] = []
    beats = rendered(result)
    if not beats:
        found.append("no rendered beat")
    if any(b.channel not in CHANNELS for b in beats):
        found.append("a beat on an unknown channel")
    if any(not (b.text or "").strip() for b in beats):
        found.append("a beat without text")
    if not any(b.channel in {"say", "do"} for b in beats):
        found.append("no say or do beat")
    if not result.message.text.strip():
        found.append("an empty message")
    if SAFE_FALLBACK in result.degradation:
        found.append("the safe fallback replaced the render")
    return found


RenderKey = tuple[tuple[int, str, str], ...]


def render_key(beats: Sequence[Any], originals: Mapping[int, str]) -> RenderKey:
    """The render's output from the released rendered ``beats`` and the repaired beats'
    ``originals`` (by beat): ``(beat, channel, text)`` in beat order; a beat the ladder
    dropped keeps its original text (its channel unknown, ``""``)."""
    found = {int(b.beat): (str(b.channel), str(b.text)) for b in beats if b.beat is not None}
    for beat, text in originals.items():
        channel = found.get(beat, ("", ""))[0]
        found[beat] = (channel, text)
    return tuple((beat, *found[beat]) for beat in sorted(found))


async def render_output(ctx: CaseContext, result: RespondResult) -> RenderKey:
    """What the turn's render call answered (see the module docstring)."""
    beats = rendered(result)
    operation = next((b.operation_id for b in result.beats), None)
    originals: dict[int, str] = {}
    if operation is not None:
        async with ctx.engine.db.connect() as conn:
            rows = await conn.execute(
                select(events.c.payload).where(
                    events.c.operation_id == operation, events.c.type == "render_rejected"
                )
            )
            for (payload,) in rows:
                if isinstance(payload, dict) and payload.get("attempt") == 0:
                    originals[int(payload["beat"])] = str(payload.get("text") or "")
    return render_key(beats, originals)


__all__ = [
    "CAST",
    "INNKEEPER",
    "PLAYER",
    "RenderKey",
    "play",
    "render_key",
    "render_output",
    "rendered",
    "structure_problems",
]
