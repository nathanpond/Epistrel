"""Provider content is untrusted data, never instructions (FR-EXT-5, FR-API-5, NFR-SEC-2; #208).

Everything an adapter answers is content from outside the story: an injection surface. Three
rules keep it data.

**Typed before it is pinned** (``sanitize``, from ``PinnedReads.get``, #205). A value is checked
against the manifest's declared ``type`` and the State that owns it:

- ``number``: a finite number (never a boolean) of at most ``DIGITS_MAX`` significant digits;
- ``boolean``: a boolean;
- ``enum``: one of the State's ``allowed_values``, exactly;
- ``text``: a string. It is stripped of control and format characters other than newline and
  tab (``unicodedata`` categories ``Cc``, ``Cf``, ``Zl``, ``Zp``: a carriage return, a bidi
  override, a zero-width joiner), its fence delimiters are neutralised (``<`` → ``\u2039``,
  ``>`` → ``\u203a``: prompt templates render with ``autoescape=False``, so this makes
  ``</external>`` or a forged ``<external …>`` ordinary punctuation), and it is capped at
  ``TEXT_MAX`` characters (``truncated`` is then set; the ``external_read`` event records the
  flag, never the raw body).

Number, boolean and enum values are validated, never rewritten. Anything else (an object or an
array at ``value_path``, a value of another type, a manifest type that no longer matches the
State's) raises ``ProviderMalformed`` (``provider_malformed``): nothing of the answer is used
(no partial parse) and the provider's unavailability policy applies (#206).

**Fenced in prompts** (``render_block``). A value reaches a prompt only as one labelled data
block, ``<external name=… tier=…>value</external>`` (``holder=…`` for a per-character value),
whose attributes come from the registration (the value's name, lower-cased, and the provider's
trust tier) and must match ``[a-z0-9_]+`` (a holder is a character id). The payload's JSON keys
are never rendered: only the typed value at ``value_path``. The block is the narrator's alone
(``orchestrator.structured.prompt.narrator_inputs``, ``ContentCategory.EXTERNAL_DATA``): a
character learns a value only as a belief through ``build_context`` (#170), and
``check-character-context`` keeps ``external_data`` out of ``character_inputs``.

**Within its domain** (``in_domain``). A provider's value answers only a ``state_value`` claim on
the State it owns: a balance provider never supports a location claim, and a claim outside its
value name never reads it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from epistrel.core.enums import StateType
from epistrel.core.propositions.vocabulary import STATE_VALUE
from epistrel.core.states.model import DIGITS_MAX, number
from epistrel.core.states.verify import state_of
from epistrel.orchestrator.providers.port import ProviderMalformed

if TYPE_CHECKING:
    from collections.abc import Sequence

    from epistrel.core.propositions.model import Proposition
    from epistrel.core.states.model import StateDefinition, Value

TEXT_MAX = 500
"""The most characters of a ``text`` value a prompt sees; longer is truncated (``truncated``)."""

ATTRIBUTE = re.compile(r"[a-z0-9_]+")
"""A block's ``name`` and ``tier`` attributes."""
HOLDER = re.compile(r"[A-Za-z0-9_]+")
"""A block's ``holder`` attribute (a character id)."""

FENCE = "external"
"""The data block's tag."""
NEUTRALISED = str.maketrans({"<": "\u2039", ">": "\u203a"})
"""``<`` → ``\u2039`` and ``>`` → ``\u203a``: no tag-like sequence survives in a text value."""
KEPT = frozenset("\n\t")
STRIPPED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
"""Control, format (bidi overrides, zero-width), line- and paragraph-separator characters."""

STATE_TYPES: dict[str, frozenset[StateType]] = {
    "number": frozenset({StateType.NUMBER}),
    "boolean": frozenset({StateType.BOOLEAN}),
    "enum": frozenset({StateType.ENUM}),
    "text": frozenset({StateType.TEXT}),
}
"""The State types a manifest type may own (``core.providers.check_external_value``)."""


@dataclass(frozen=True, slots=True)
class Sanitized:
    """A value fit to pin: typed, and for text cleaned (``truncated``: it was capped)."""

    value: Decimal | bool | str
    truncated: bool = False


def neutralise(text: str) -> str:
    """``text`` with no fence delimiter: ``<``/``>`` as ``\u2039``/``\u203a``."""
    return text.translate(NEUTRALISED)


def clean_text(text: str) -> tuple[str, bool]:
    """A ``text`` value stripped of control and format characters (newline and tab kept),
    neutralised, and capped at ``TEXT_MAX`` characters; and whether it was capped."""
    kept = "".join(
        c for c in text if c in KEPT or unicodedata.category(c) not in STRIPPED_CATEGORIES
    )
    kept = neutralise(kept)
    if len(kept) > TEXT_MAX:
        return kept[:TEXT_MAX], True
    return kept, False


def sanitize(
    value: Any,
    declared: str,
    *,
    provider: str,
    name: str,
    state_type: StateType | None = None,
    allowed_values: Sequence[str] | None = None,
) -> Sanitized:
    """``value`` (an adapter's answer for ``provider``'s value ``name``) checked against the
    manifest's ``declared`` type and the owning State's type and allowed values, a text value
    cleaned (see the module docstring); else ``ProviderMalformed``."""
    if state_type is not None and state_type not in STATE_TYPES.get(declared, frozenset()):
        raise ProviderMalformed(provider, name, "type")
    if declared == "number":
        found = number(value)
        if found is None:
            raise ProviderMalformed(provider, name, "type")
        if len(found.normalize().as_tuple().digits) > DIGITS_MAX:
            raise ProviderMalformed(provider, name, "precision")
        return Sanitized(found)
    if declared == "boolean":
        if not isinstance(value, bool):
            raise ProviderMalformed(provider, name, "type")
        return Sanitized(value)
    if declared == "enum":
        if not isinstance(value, str):
            raise ProviderMalformed(provider, name, "type")
        if allowed_values is not None and value not in allowed_values:
            raise ProviderMalformed(provider, name, "allowed_values")
        return Sanitized(value)
    if declared == "text":
        if not isinstance(value, str):
            raise ProviderMalformed(provider, name, "type")
        cleaned, truncated = clean_text(value)
        return Sanitized(cleaned, truncated)
    raise ProviderMalformed(provider, name, "type")


def _shown(value: Value | None) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")
    return neutralise(str(value))


def render_block(name: str, tier: str, value: Value | None, *, holder: str | None = None) -> str:
    """One value as the prompt's fenced data block (see the module docstring). ``name`` is
    lower-cased; an attribute outside its pattern is a ``ValueError`` (never rendered)."""
    label = name.lower()
    for attribute in (label, tier):
        if not ATTRIBUTE.fullmatch(attribute):
            raise ValueError(f"{attribute!r} is not a data block attribute")
    held = ""
    if holder is not None:
        if not HOLDER.fullmatch(holder):
            raise ValueError(f"{holder!r} is not a data block holder")
        held = f" holder={holder}"
    return f"<{FENCE} name={label} tier={tier}{held}>{_shown(value)}</{FENCE}>"


def in_domain(definition: StateDefinition, proposition: Proposition) -> bool:
    """Whether a provider's value of ``definition`` may answer ``proposition``: a
    ``state_value`` claim on that State, and nothing else (domain gating)."""
    return proposition.predicate_id == STATE_VALUE and state_of(proposition) == str(definition.id)


__all__ = [
    "ATTRIBUTE",
    "FENCE",
    "HOLDER",
    "TEXT_MAX",
    "Sanitized",
    "clean_text",
    "in_domain",
    "neutralise",
    "render_block",
    "sanitize",
]
