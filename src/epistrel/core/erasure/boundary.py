"""The erasure boundary, stated once (FR-ADMIN-8; #125): what an erasure reaches and what it
cannot.

``ERASURE_BOUNDARY`` is the single source. ``GET /admin/erasure/boundary`` returns it as data
(``Engine.erasure_boundary``); ``render_boundary()`` is the same text as Markdown, which is the
route's OpenAPI description, the end of the API's ``info.description`` (after the trust boundary
statement) and the marked block of ``docs/OPERATIONS.md`` (``<!-- erasure-boundary:start -->``
to ``<!-- erasure-boundary:end -->``). A test holds the three equal, whitespace aside.

The ``erasure_shared_media`` sentence (#124) states both of its values, so the documents stay
static whatever a deployment has set.
"""

from __future__ import annotations

from dataclasses import dataclass

DELIVERED_EXPORTS = "exports already delivered"
PROVIDER_RETENTION = (
    "data retained by external model/image/data providers "
    "(including hosted guardrail or image check providers)"
)
VISUAL_ONLY = "subjects appearing in images only visually with no declared or named reference"
OLD_BACKUPS = "backups taken before the erasure, until restored and reconciled"
LOGS_ITEM = "logs, which never carry story content"
AMBIGUOUS_MENTIONS = (
    "mentions the matcher classes as ambiguous (a name word that is also a common word, "
    "capitalised at the start of a sentence) unless the operator confirms them, and nicknames "
    "never declared as aliases; an erasure reports how many candidates remain "
    "(`candidates_remaining`). A word of the subject's name on its own is not matched when "
    "another character in the same scope also uses it (a shared surname) or when it is a "
    "title or particle (`Dr`, `de`); a common word (`will`, `rose`) is matched only "
    "capitalised. Names match in any case, fully case-folded (`Strauß` is `STRAUSS`, `Yılmaz` "
    "is `YILMAZ`), a hyphenated name by its halves at any dash, and a soft hyphen inside a "
    "word is ignored (ADR-154, ADR-155, ADR-156)"
)
KEPT_CARD = (
    "a cast catalog character's public card in the creation report of a story she was erased "
    "from at story scope: its labels and public description stay, with its structural fields "
    "(its kind, ids, source, version and status values), while its name is redacted and its "
    "images and content rating are emptied, because the card is public catalog data, until a "
    "catalog-scope erasure of that catalog character empties it too (docs/04 C-15)"
)
LOGS = "operational logs contain no story content by default (NFR-SEC-4)"

SHARED_MEDIA = (
    "media assets that depict or name only the subject: under `erasure_shared_media = purge` "
    "(the default) the asset and its bytes are deleted; under `retire`, an asset another "
    "record still uses stays on disk, retired, with the subject removed from its metadata"
)

IN_SCOPE: tuple[str, ...] = (
    "events and beats (redacted in place; each deleted record leaves an erasure marker)",
    "projections: transcript, story texts and heads, rebuilt from the redacted events",
    "rolling-summary segments (summarised again from the redacted window, or left redacted)",
    "pinned memories and custom instructions",
    "stored idempotency results",
    "catalog drafts and published versions, and scenario reports",
    "personas",
    "media metadata and bytes (declared subjects, captions, alt text and prompts)",
    SHARED_MEDIA,
    "message feedback and its history",
    "feedback exports not yet downloaded (the subject is redacted in their cases)",
    "story snapshots (scenario, characters, persona) and deferred genesis items",
    "the story's creation report: the subject's deferred genesis items, and her cast card "
    "(see below for what a story-scope erasure keeps of a catalog character's card)",
    "the subject-reference index",
    "a backup restored after the erasure: its stories are not served until the erasure "
    "ledger, kept outside the database and copied into every backup, has been reapplied",
)
OUT_OF_SCOPE: tuple[str, ...] = (
    DELIVERED_EXPORTS,
    PROVIDER_RETENTION,
    VISUAL_ONLY,
    OLD_BACKUPS,
    LOGS_ITEM,
    AMBIGUOUS_MENTIONS,
    KEPT_CARD,
)


@dataclass(frozen=True, slots=True)
class ErasureBoundary:
    """What erasure covers (``in_scope``), what it cannot reach (``out_of_scope``), and what
    the logs hold (``logs``)."""

    in_scope: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    logs: str


ERASURE_BOUNDARY = ErasureBoundary(in_scope=IN_SCOPE, out_of_scope=OUT_OF_SCOPE, logs=LOGS)

HEADING = "## Erasure boundary"


def render_boundary(boundary: ErasureBoundary = ERASURE_BOUNDARY) -> str:
    """The boundary as Markdown: a heading, the two lists and the logs line."""
    lines = [
        HEADING,
        "",
        "**In scope.** An erasure removes or redacts the subject in:",
        "",
        *(f"- {item}" for item in boundary.in_scope),
        "",
        "**Out of scope.** An erasure cannot reach:",
        "",
        *(f"- {item}" for item in boundary.out_of_scope),
        "",
        f"**Logs.** {boundary.logs[0].upper()}{boundary.logs[1:]}.",
    ]
    return "\n".join(lines)


def normalised(text: str) -> str:
    """``text`` with every run of whitespace made one space (how the copies are compared)."""
    return " ".join(text.split())


__all__ = [
    "AMBIGUOUS_MENTIONS",
    "DELIVERED_EXPORTS",
    "ERASURE_BOUNDARY",
    "HEADING",
    "IN_SCOPE",
    "KEPT_CARD",
    "LOGS",
    "LOGS_ITEM",
    "OLD_BACKUPS",
    "OUT_OF_SCOPE",
    "PROVIDER_RETENTION",
    "SHARED_MEDIA",
    "VISUAL_ONLY",
    "ErasureBoundary",
    "normalised",
    "render_boundary",
]
