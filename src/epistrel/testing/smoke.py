"""C-17's scenario smoke (FR-PROF-2,4; #243): one engine profile played end to end in process.

``run_scenario_smoke(engine, profile_id, creation)`` runs, on the Engine's own (stub) adapters:

1. a story created from a scenario (``creation``, a ``StoryFromScenario`` body; C-17 passes the
   Sunday Dinner fixture's) under ``profile_id``;
2. four turns by the player, the third naming two characters to render (a multi-character
   request);
3. a regeneration of the last turn (before the ingest: an ingested narration is a turn with no
   reply to regenerate);
4. one ingested narration;
5. a truncation to turn 2;
6. a fork at turn 2;
7. the story's effective configuration and guarantee table;
8. both stories deleted.

Every step must succeed. A refusal is allowed only where the configuration makes the step
inapplicable, with its documented code (``INAPPLICABLE``): a multi-character request to a
structured profile whose renders are ``combined`` is 422 ``validation_error`` with reason
``one_render_character``. Anything else is the configuration's failure: the step and the error.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from epistrel.core.context import RequestContext
from epistrel.core.errors import EpistrelError
from epistrel.core.fork import ForkRequest
from epistrel.core.genesis.models import StoryFromScenario
from epistrel.core.ingest import IngestRequest
from epistrel.core.stories import CreatedStory
from epistrel.core.timeline import TruncateRequest
from epistrel.orchestrator.lite.schemas import RespondRequest

PRINCIPAL = "p-c17-smoke"
TURNS = 4
MULTI_TURN = 3
LINES = (
    "Good evening, everyone.",
    "What is for dinner?",
    "Will you both pass the bread?",
    "I think I'll take a walk.",
)


@dataclass(frozen=True, slots=True)
class Inapplicable:
    """A refusal the configuration explains: the step, the error code and, for a validation
    error, its ``reason``."""

    step: str
    code: str
    reason: str | None = None


INAPPLICABLE = (Inapplicable("turn 3", "validation_error", "one_render_character"),)


@dataclass
class SmokeResult:
    """What one configuration's smoke did: each step's outcome (``ok``, or the allowed refusal's
    code), and the first failing step with its error, if any."""

    steps: dict[str, str] = field(default_factory=dict)
    failed_step: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.failed_step is None


def _allowed(step: str, exc: EpistrelError) -> bool:
    reason = (exc.details or {}).get("reason")
    return any(
        a.step == step and a.code == exc.code and (a.reason is None or a.reason == reason)
        for a in INAPPLICABLE
    )


async def run_scenario_smoke(
    engine: Any, profile_id: str, creation: Mapping[str, Any]
) -> SmokeResult:
    """Play ``profile_id`` end to end (see the module docstring)."""
    ctx = RequestContext(principal=PRINCIPAL)
    result = SmokeResult()
    stories: list[str] = []

    async def step(name: str, call: Callable[[], Awaitable[Any]]) -> Any:
        try:
            found = await call()
        except EpistrelError as exc:
            if _allowed(name, exc):
                result.steps[name] = exc.code
                return None
            raise _Failed(name, f"{exc.code}: {exc} {exc.details}") from None
        except Exception as exc:
            raise _Failed(name, f"{type(exc).__name__}: {exc}") from None
        result.steps[name] = "ok"
        return found

    try:
        spec = StoryFromScenario.model_validate(
            {**creation, "engine_profile_id": profile_id, "ignore_warnings": True}
        )
        created = await step("create", lambda: engine.create_story(ctx, spec))
        if not isinstance(created, CreatedStory):
            raise _Failed("create", f"not created: {type(created).__name__}")
        story = created.story_id
        stories.append(story)
        player = next(c.id for c in created.characters if c.role_id == "player")
        others = [c.id for c in created.characters if c.id != player]
        for turn in range(1, TURNS + 1):
            body: dict[str, Any] = {
                "actor_character_id": player,
                "player_input": {"say": LINES[turn - 1]},
            }
            if turn == MULTI_TURN and len(others) >= 2:
                body["render_character_ids"] = others[:2]
            request = RespondRequest.model_validate(body)

            async def play(request: RespondRequest = request) -> Any:
                return await engine.respond(ctx, story, request)

            await step(f"turn {turn}", play)
        await step("regenerate", lambda: engine.regenerate(ctx, story))
        narration = {"client_event_id": "c17-narration", "kind": "narration", "text": "Dusk."}
        await step(
            "ingest",
            lambda: engine.ingest(
                ctx, story, IngestRequest.model_validate({"events": [narration]})
            ),
        )
        await step(
            "truncate",
            lambda: engine.truncate(ctx, story, TruncateRequest.model_validate({"to_turn": 2})),
        )
        forked = await step(
            "fork", lambda: engine.fork(ctx, story, ForkRequest.model_validate({"at_turn": 2}))
        )
        if forked is not None and hasattr(forked, "story_id"):
            stories.append(forked.story_id)
        await step("effective config", lambda: engine.get_effective_config(ctx, story))
        await step("guarantees", lambda: engine.get_story_guarantees(ctx, story))
    except _Failed as failed:
        result.failed_step, result.error = failed.step, failed.error
    finally:
        for found in reversed(stories):
            try:
                await engine.delete_story(ctx, found)
            except Exception as exc:
                if result.ok:
                    result.failed_step, result.error = "delete", f"{type(exc).__name__}: {exc}"
        if result.ok:
            result.steps["delete"] = "ok"
    return result


class _Failed(Exception):
    def __init__(self, step: str, error: str) -> None:
        super().__init__(step)
        self.step = step
        self.error = error


__all__ = ["INAPPLICABLE", "Inapplicable", "SmokeResult", "run_scenario_smoke"]
