"""The audience filter: what a viewpoint's output may hold of a turn's beats (FR-RESP-12,13,
FR-MULTI-4, FR-AUTH-2, FR-EPI-12; docs/02 §4.4; #177).

``filter_for_viewpoint(beats, viewpoint, …)`` decides, beat by beat, what a response, a history
read and a returned beat list for ``viewpoint`` carry. It routes the beats through the same
perception decision memory writes and working memory use (``perception.route_turn``, #161), so
output and memory never disagree about what a character saw. It reads authoritative positions
only (the ``SceneState`` the caller loads and each beat's committed moves), so the player
character is filtered like anyone and needs no partition (FR-EPI-12). Pure: no I/O and no log.

For each beat the filter answers a ``Seen``: the beat, the **fidelity** the viewing character
perceived it at (``content``, ``envelope`` or ``none``) and the text the output carries:

- ``omniscient`` (only ever when a request names it): every beat at full content, ``think`` and
  ``latent`` included (labelled by the assembly); ``audience_filter: "none"``.
- A character viewpoint never receives a ``think`` or a ``latent``, its own included, under
  every profile and every ``player_view`` (FR-RESP-12).
- Perception ``none`` (every Lite story, and structured stories with shared character
  knowledge): every other beat at full content; ``audience_filter: "none"``.
- ``co_location`` or ``full`` under ``player_view = character`` (the default): ``content`` → the
  beat in full (an eavesdropper limited to part of the words gets that part); ``envelope`` (only
  at ``full``) → the envelope text (``perception.envelope_text``, "Jenni whispers something to
  Marcus") in place of the content; ``none`` → omitted. ``audience_filter`` is the level, or
  ``nothing_perceived`` when the viewpoint perceived none of the beats.
- ``player_view = story`` (the owner's round-two setting): every non-private beat at full
  content whatever the character perceived, each still carrying the fidelity the character
  **would** have perceived it at (so a game can style it); ``audience_filter:
  "bypassed_by_player_view"``. What the player reads is not what the character knows (FR-EPI-12):
  memory and partitions are routed at the commit and never read this.

A ``simultaneous`` turn (``mode``, #175) routes every beat against the starting placements, as
its memories were.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from epistrel.core.epistemic import perception
from epistrel.core.settings_schema import CHARACTER_VIEWPOINT_PREFIX, PlayerView
from epistrel.core.viewpoint import PRIVATE_CHANNELS, AudienceFilter, is_omniscient

CHARACTER_VIEW: PlayerView = "character"
STORY_VIEW: PlayerView = "story"
PLAYER_VIEWS: tuple[PlayerView, ...] = (CHARACTER_VIEW, STORY_VIEW)

OutputFidelity = Literal["content", "envelope", "none"]
CONTENT: OutputFidelity = "content"
ENVELOPE: OutputFidelity = "envelope"
UNPERCEIVED: OutputFidelity = "none"

NOTHING_PERCEIVED: AudienceFilter = "nothing_perceived"
BYPASSED: AudienceFilter = "bypassed_by_player_view"


@dataclass(frozen=True, slots=True)
class AudienceBeat:
    """One beat as the filter reads it: its kind (``say``/``do``/``think``/``latent``/
    ``narration``), number, actor (``None`` for a narration and the narrator's ``latent``), text,
    stored perception qualifiers and the moves its beat number committed. ``source`` is the
    caller's own beat, passed through untouched."""

    kind: str
    beat: int
    character_id: str | None
    text: str
    qualifiers: perception.Qualifiers = field(default_factory=perception.Qualifiers)
    moves: tuple[tuple[str, str | None], ...] = ()
    source: object | None = None


@dataclass(frozen=True, slots=True)
class Seen:
    """A beat the output carries: the beat, the fidelity the viewing character perceived it at
    (under ``player_view = story``: would have), the text the output carries in its place, and
    whether that text is only the part an eavesdropper caught."""

    beat: AudienceBeat
    fidelity: OutputFidelity
    text: str
    partial: bool = False

    @property
    def envelope(self) -> bool:
        """Whether the output carries the beat's envelope in place of its content."""
        return self.text != self.beat.text and self.fidelity == ENVELOPE


@dataclass(frozen=True, slots=True)
class Filtered:
    """What ``viewpoint`` receives of a turn: the beats it carries, in order, every beat it
    withholds (an envelope's content and every unperceived beat: what a composition must not
    disclose), and the ``audience_filter`` the output states."""

    seen: tuple[Seen, ...]
    withheld: tuple[AudienceBeat, ...]
    audience_filter: AudienceFilter


def character_of(viewpoint: str) -> str | None:
    """The character a ``character:<id>`` viewpoint names (``None`` for ``omniscient``)."""
    if viewpoint.startswith(CHARACTER_VIEWPOINT_PREFIX):
        return viewpoint.removeprefix(CHARACTER_VIEWPOINT_PREFIX)
    return None


def audience_filter_of(
    viewpoint: str, level: perception.Level, player_view: PlayerView
) -> AudienceFilter:
    """What a read for ``viewpoint`` states about its filtering before any beat is seen (see the
    module docstring; ``nothing_perceived`` is the filter's own answer)."""
    if is_omniscient(viewpoint) or level == perception.NONE:
        return "none"
    if player_view == STORY_VIEW:
        return BYPASSED
    return level


def _route_kind(kind: str) -> perception.RouteKind:
    if kind in ("say", "do", "think", "latent", "narration"):
        return kind  # type: ignore[return-value]
    return "narration"


def filter_for_viewpoint(
    beats: Sequence[AudienceBeat],
    viewpoint: str,
    *,
    scene: perception.SceneState | None,
    level: perception.Level,
    names: Mapping[str, str],
    places: Mapping[str, str] | None = None,
    player_view: PlayerView = CHARACTER_VIEW,
    mode: str = perception.SEQUENTIAL,
) -> Filtered:
    """What ``viewpoint`` receives of one turn's ``beats`` (see the module docstring). ``scene``
    is the turn's scene at its start (``None``: nothing is routed). Pure."""
    ordered = sorted(beats, key=lambda b: b.beat)
    if is_omniscient(viewpoint):
        return Filtered(tuple(Seen(b, CONTENT, b.text) for b in ordered), (), "none")
    shown = [b for b in ordered if b.kind not in PRIVATE_CHANNELS]
    stated = audience_filter_of(viewpoint, level, player_view)
    viewer = character_of(viewpoint)
    if stated == "none" or scene is None or viewer is None:
        return Filtered(tuple(Seen(b, CONTENT, b.text) for b in shown), (), "none")
    routed = perception.route_turn(
        [
            perception.RouteBeat(
                kind=_route_kind(b.kind),
                beat=b.beat,
                actor=None if b.kind == "narration" else b.character_id,
                qualifiers=b.qualifiers,
                sound=perception.sound_of(b.text) if b.kind == "do" else None,
                moves=b.moves,
                source=b,
            )
            for b in ordered
        ],
        scene,
        level,
        mode=mode,
    )
    seen: list[Seen] = []
    withheld: list[AudienceBeat] = []
    for result in routed:
        beat = result.beat.source
        assert isinstance(beat, AudienceBeat)  # noqa: S101 - built above
        if beat.kind in PRIVATE_CHANNELS:
            continue
        found = result.of(viewer)
        if found is None:
            fidelity, text, partial = UNPERCEIVED, beat.text, False
        elif found.fidelity == perception.ENVELOPE and found.envelope is not None:
            fidelity = ENVELOPE
            text = perception.envelope_text(found.envelope, names, places)
            partial = False
        elif found.partial_text:
            fidelity, text, partial = CONTENT, found.partial_text.strip(), True
        else:
            fidelity, text, partial = CONTENT, beat.text, False
        if player_view == STORY_VIEW:
            seen.append(Seen(beat, fidelity, beat.text))
            continue
        if fidelity == UNPERCEIVED:
            withheld.append(beat)
            continue
        if fidelity == ENVELOPE or partial:
            withheld.append(beat)
        seen.append(Seen(beat, fidelity, text, partial))
    if player_view != STORY_VIEW and not seen:
        stated = NOTHING_PERCEIVED
    return Filtered(tuple(seen), tuple(withheld), stated)


__all__ = [
    "BYPASSED",
    "CHARACTER_VIEW",
    "CONTENT",
    "ENVELOPE",
    "NOTHING_PERCEIVED",
    "PLAYER_VIEWS",
    "STORY_VIEW",
    "UNPERCEIVED",
    "AudienceBeat",
    "AudienceFilter",
    "Filtered",
    "OutputFidelity",
    "PlayerView",
    "Seen",
    "audience_filter_of",
    "character_of",
    "filter_for_viewpoint",
]
