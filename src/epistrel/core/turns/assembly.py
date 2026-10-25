"""The deterministic assembly of a structured turn's verified beats (FR-RESP-8, FR-RESP-12;
#152).

The safe path of composition. It adds no proposition, keeps every beat's words and speaker, and
makes no model call. ``orchestrator.compose`` releases it whenever a polished composition is
skipped or fails its check, and the history read (``core.history``) recomposes it for a
viewpoint other than the one a turn's stored message was composed for. Each beat appears once,
in turn order, as its own paragraph (separated by a blank line):

- ``say``: ``Name: "text"`` (``"text"`` when the speaker has no name);
- ``do``: ``*text*``;
- ``narration``: the text as it is;
- ``think``, ``omniscient`` only: ``[Name thinks] text``;
- ``latent``, ``omniscient`` only: ``[latent] text``;
- a beat the viewpoint perceived only the envelope of (#177, ``fidelity: envelope``): its
  envelope as an action, ``*Jenni whispers something to Marcus*``.

The ``say``/``do``/narration shape is #150's M4 placeholder, already stored on M4 turns, so a
recomposed message of an older turn reads like the one stored. For a character viewpoint the
input is the beats less ``think`` and ``latent`` (``viewpoint_beats``; perception ``none``,
FR-RESP-12), and where perception is on what the audience filter returns
(``core.epistemic.audience``, #177). The player's input is not part of the message.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from epistrel.core.viewpoint import PRIVATE_CHANNELS, is_omniscient

SEPARATOR = "\n\n"
THINK_LABEL = "[{name} thinks] "
LATENT_LABEL = "[latent] "
PRIVATE_KINDS = frozenset(PRIVATE_CHANNELS)
"""``think`` and ``latent``: never in a character viewpoint's message."""


class Beat(Protocol):
    """What the assembly reads of a beat (a ``core.turns.beats.TurnBeat``, an
    ``AssemblyBeat``)."""

    @property
    def kind(self) -> str: ...
    @property
    def text(self) -> str: ...
    @property
    def character_id(self) -> str | None: ...
    @property
    def beat(self) -> int: ...


@dataclass(frozen=True, slots=True)
class AssemblyBeat:
    """One beat as the assembly reads it: its kind (``say``/``do``/``think``/``latent``/
    ``narration``), text, actor (``None`` for narration) and position."""

    kind: str
    text: str
    character_id: str | None
    beat: int
    fidelity: str = "content"
    """``envelope`` (#177): ``text`` is the beat's envelope, assembled as an action."""


ENVELOPE = "envelope"


def viewpoint_beats[B: Beat](beats: Sequence[B], viewpoint: str) -> list[B]:
    """``beats`` as ``viewpoint`` receives them: every beat for ``omniscient``, every beat less
    ``think`` and ``latent`` for a character (perception ``none``)."""
    if is_omniscient(viewpoint):
        return list(beats)
    return [b for b in beats if b.kind not in PRIVATE_KINDS]


def think_label(name: str) -> str:
    """The label an omniscient message gives a thought: ``[Name thinks] ``."""
    return THINK_LABEL.format(name=name or "someone")


def piece(beat: Beat, names: Mapping[str, str]) -> str:
    """One beat's paragraph of the assembly (see the module docstring)."""
    name = names.get(beat.character_id or "", "")
    text = beat.text.strip()
    if getattr(beat, "fidelity", None) == ENVELOPE:
        return f"*{text}*"
    if beat.kind == "say":
        return f'{name}: "{text}"' if name else f'"{text}"'
    if beat.kind == "do":
        return f"*{text}*"
    if beat.kind == "think":
        return think_label(name) + text
    if beat.kind == "latent":
        return LATENT_LABEL + text
    return text


def assembly_pieces[B: Beat](
    beats: Sequence[B], names: Mapping[str, str], viewpoint: str
) -> list[tuple[B, str]]:
    """Each beat ``viewpoint`` receives and its paragraph, in turn order (empty texts left
    out)."""
    return [(b, piece(b, names)) for b in viewpoint_beats(beats, viewpoint) if b.text.strip()]


def deterministic_assembly(beats: Sequence[Beat], names: Mapping[str, str], viewpoint: str) -> str:
    """The message ``viewpoint`` receives for ``beats``: their paragraphs joined (see the
    module docstring). Pure."""
    return SEPARATOR.join(text for _, text in assembly_pieces(beats, names, viewpoint))


__all__ = [
    "ENVELOPE",
    "LATENT_LABEL",
    "PRIVATE_KINDS",
    "SEPARATOR",
    "AssemblyBeat",
    "Beat",
    "assembly_pieces",
    "deterministic_assembly",
    "piece",
    "think_label",
    "viewpoint_beats",
]
