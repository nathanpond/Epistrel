"""Generate the model guide (``docs/models.md``) from the purpose registry (FR-MODEL-10,13).

    uv run python -m epistrel.tools.model_guide --check   # exit 1 when docs/models.md is stale
    uv run python -m epistrel.tools.model_guide --write   # regenerate it

The guide lists every delivered purpose in registry order: what it does, whether it is on the
hot path, the ``model_config`` row it defaults to, its template, its capability floor, the
structured result it returns, and its data-flow row (each input field it receives with that
field's content category, and the categories it never receives). Then come the reserved
purposes, the content categories, and one hosted-provider retention note. CI runs ``--check``
in the ``lint`` job, and the comparison is byte for byte.

A purpose without a capability floor or without a data-flow row (no declared input) cannot be
rendered: ``GuideError`` names it, so the generator and the check both fail.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping, Sequence

from epistrel.orchestrator.models.registry import (
    DELIVERED,
    RESERVED,
    CapabilityFloor,
    ContentCategory,
    Purpose,
    ReservedPurpose,
    StructuredOutput,
    field_categories,
)
from epistrel.tools.generated import REPO_ROOT
from epistrel.tools.generated import main as generated_main

GUIDE_PATH = REPO_ROOT / "docs" / "models.md"
PROG = "epistrel.tools.model_guide"

CATEGORY_DESCRIPTIONS: Mapping[ContentCategory, str] = {
    ContentCategory.RATING_GUIDANCE: "The story's content-rating guidance text.",
    ContentCategory.CUSTOM_INSTRUCTIONS: "The story's custom instructions.",
    ContentCategory.CHARACTER_DEFINITION: "Character names and definitions.",
    ContentCategory.PINNED_MEMORY: "Pinned memories of the story.",
    ContentCategory.ROLLING_SUMMARY: "The story's rolling summary segments.",
    ContentCategory.RECENT_MESSAGE: "Earlier messages of the story (who, what was said and done).",
    ContentCategory.PLAYER_INPUT: "The player's input for this turn.",
    ContentCategory.POLICY_TEXT: (
        "A policy's prompt text and opaque subject labels; for generation, only the messages "
        "of output policies an earlier draft broke."
    ),
    ContentCategory.SUBJECT_TRUE_AGE: "A resolved subject's true age.",
    ContentCategory.CHECKED_TEXT: "The text a guardrail check is judging (input or output).",
}

STRUCTURED_LABELS: Mapping[StructuredOutput, str] = {
    StructuredOutput.NONE: "not required",
    StructuredOutput.JSON_SCHEMA: "constrained `json_schema` decoding",
    StructuredOutput.PARSE_VALIDATE_RETRY: (
        "constrained `json_schema` decoding, or output reliable enough to parse, validate and retry"
    ),
}

BANNER = (
    "<!-- Generated from src/epistrel/orchestrator/models/registry.py by "
    "`uv run python -m epistrel.tools.model_guide --write`. Do not edit by hand: CI fails "
    "when this file differs from what the registry generates (FR-MODEL-13). -->"
)

INTRO = """\
# Model guide

Every model call Epistrel makes names a **purpose**. This guide is generated from the purpose
registry, so it always matches what the code sends. For each purpose delivered so far it gives
the recommended **capability floor** (FR-MODEL-10) and the **data-flow row**: every input field
a request for that purpose may carry, with its content category, and the categories it never
receives (FR-MODEL-13). A prompt can carry only fields its purpose declares. A template that
references anything else fails the build, and so does a payload that adds a field.

Speaking the OpenAI-compatible protocol does not make a model capable for a purpose. A model is
capable for a purpose only when it meets that purpose's floor (NFR-PORT-3). The measured results
per model tier are in [model-matrix.md](model-matrix.md).
"""

RETENTION = """\
## Hosted providers and retention

Each purpose sends the inputs listed in its row to the endpoint configured for it (its
`base_url`, inherited from the `default` row unless the purpose overrides it). When that
endpoint is a hosted provider, those inputs leave the deployment and are kept under that
provider's terms, outside Epistrel's erasure boundary (FR-ADMIN-8, FR-IMG-13). This includes the
true ages `guardrail_check` receives for age-conditioned policies. A deployment that needs any
of this data kept in-house points the purposes concerned at self-hosted models.
`GET /admin/model-config` names the endpoint for every purpose (FR-MODEL-1).
"""


class GuideError(Exception):
    """A purpose the guide cannot describe (no capability floor, or no data-flow row)."""


def _code(text: str) -> str:
    return f"`{text}`"


def _leaf_paths(categories: Mapping[str, set[ContentCategory]]) -> list[tuple[str, str]]:
    """``(field path, category)`` for every leaf field, in declaration order."""
    paths = list(categories)
    leaves: list[tuple[str, str]] = []
    for path in paths:
        if any(other.startswith(f"{path}.") for other in paths):
            continue
        leaves.append((path, ", ".join(sorted(categories[path]))))
    return leaves


def problems(purposes: Iterable[Purpose]) -> list[str]:
    """Why each purpose cannot appear in the guide (empty when all can)."""
    found: list[str] = []
    for purpose in purposes:
        if not isinstance(purpose.capability_floor, CapabilityFloor):
            found.append(f"{purpose.name}: no capability floor (FR-MODEL-10)")
        if not field_categories(purpose.input_schema):
            found.append(f"{purpose.name}: no data-flow row (it declares no input field)")
    return found


def _summary(purposes: Sequence[Purpose]) -> list[str]:
    lines = [
        "## Purposes delivered",
        "",
        "| Purpose | Hot path | Default model | Structured output | Minimum context |",
        "|---|---|---|---|---|",
    ]
    for p in purposes:
        floor = p.capability_floor
        lines.append(
            f"| {_code(p.name)} | {'yes' if p.hot_path else 'no'} | "
            f"{_code(p.default_model_key)} | {_code(floor.structured_output.value)} | "
            f"{floor.min_context_tokens:,} tokens |"
        )
    return [*lines, ""]


def _purpose(purpose: Purpose) -> list[str]:
    floor = purpose.capability_floor
    lines = [
        f"### {_code(purpose.name)}",
        "",
        purpose.description,
        "",
        f"- **Hot path:** {'yes' if purpose.hot_path else 'no'}",
        f"- **Default model:** the {_code(purpose.default_model_key)} row of `model_config`, "
        "unless the purpose has its own",
        f"- **Template:** {_code(purpose.template)}",
        "- **Capability floor:** structured output: "
        f"{STRUCTURED_LABELS[floor.structured_output]}; minimum context "
        f"{floor.min_context_tokens:,} tokens",
    ]
    schema = purpose.response_schema
    if schema is None:
        lines.append("- **Returns:** free text")
    else:
        fields = ", ".join(_code(name) for name in schema.model_fields)
        lines.append(
            f"- **Returns:** {_code(schema.__name__)} ({fields}), validated strictly; no "
            "free-text field"
        )
    lines += ["", "**Receives**", "", "| Field | Content category |", "|---|---|"]
    for path, category in _leaf_paths(field_categories(purpose.input_schema)):
        lines.append(f"| {_code(path)} | {_code(category)} |")
    never = ", ".join(_code(c.value) for c in ContentCategory if c in purpose.never_receives)
    lines += ["", f"**Never receives:** {never or '—'}", ""]
    return lines


def _reserved(reserved: Iterable[ReservedPurpose]) -> list[str]:
    lines = [
        "## Reserved purposes",
        "",
        "Declared now and delivered later (docs/03 §22). Each joins the sections above, with",
        "its floor and data-flow row, in the milestone that delivers it.",
        "",
        "| Purpose | Kind | Milestone | Defaults to | Description |",
        "|---|---|---|---|---|",
    ]
    for r in reserved:
        default = _code(r.defaults_to) if r.defaults_to else "—"
        lines.append(
            f"| {_code(r.name)} | {r.kind} | {r.milestone} | {default} | {r.description} |"
        )
    return [*lines, ""]


def _categories() -> list[str]:
    lines = ["## Content categories", "", "| Category | What it is |", "|---|---|"]
    for category in ContentCategory:
        description = CATEGORY_DESCRIPTIONS.get(category)
        if description is None:
            raise GuideError(f"content category {category.value!r} has no guide description")
        lines.append(f"| {_code(category.value)} | {description} |")
    return [*lines, ""]


def render(
    delivered: Mapping[str, Purpose] = DELIVERED,
    reserved: Mapping[str, ReservedPurpose] = RESERVED,
) -> str:
    """The model guide for ``delivered`` and ``reserved`` (the registry's by default)."""
    purposes = list(delivered.values())
    found = problems(purposes)
    if found:
        raise GuideError("; ".join(found))
    lines = [BANNER, "", INTRO, *_summary(purposes)]
    for purpose in purposes:
        lines += _purpose(purpose)
    lines += _reserved(reserved.values())
    lines += _categories()
    return "\n".join(lines) + "\n" + RETENTION


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return generated_main(
            argv,
            prog=PROG,
            description="Generate docs/models.md from the purpose registry.",
            default_path=GUIDE_PATH,
            generate=render,
        )
    except GuideError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
