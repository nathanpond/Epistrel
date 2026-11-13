"""Generate the two indexes at the top of the ADR log (``docs/05-adr-log.md``) from the log
itself (#889).

    uv run python -m epistrel.tools.adr_index --check   # exit 1 when an index is stale
    uv run python -m epistrel.tools.adr_index --write   # regenerate both indexes

Only the two index tables are generated: the table under ``### By requirement area`` and the
one under ``### By ADR``. Every other line of the log is kept byte for byte. An ADR is the text
from its ``### ADR-NNN — title`` heading to the next ``---`` line.

**By ADR.** One row per ADR, in log order: its number, its title (the heading, with any inline
``*[…]*`` marker removed), its standing, and the ADRs to read with it.

- **Superseded** when its **Status** line says so; otherwise **Partly superseded** when its own
  text carries a supersession marker (``*[superseded by ADR-…]*`` or a variant the log uses,
  the rule ``tests/gates/test_adr_markers.py`` enforces); otherwise **Amended** when it has an
  **Amended by** line; otherwise **Current**.
- *Read with* lists every ADR its **Amended by** line names, in order of first mention
  (``ADR-054/061`` names both), or ``—``.

**By requirement area.** One row per requirement family that docs/03 heads a section with
(``## 4. Memory tiers (FR-MEM)``), in docs/03's order, with that section's number; a ``###``
family heading without a number (the NFR families) takes its ``##`` section's. An ADR is
listed under an area when its text names a requirement id of that family (``FR-MEM-3``),
inline markers included and the **Status** and **Amended by** lines left out (they describe
other ADRs). ``CURATED`` adds the links the hand-built index made to areas an ADR does not
cite; an entry the ADR's text already derives is an error, so the table only shrinks. An area
with no ADR has no row. A cited family docs/03 does not define is an error.

CI runs ``--check`` in the ``lint`` job, byte for byte.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from epistrel.tools.generated import REPO_ROOT, check, write

LOG_PATH = REPO_ROOT / "docs" / "05-adr-log.md"
SPEC_PATH = REPO_ROOT / "docs" / "03-requirements-spec.md"
PROG = "epistrel.tools.adr_index"

AREA_HEADING = "### By requirement area"
ADR_HEADING = "### By ADR"
HEADING = re.compile(r"^### ADR-(\d{3}) — (.+)$")
STATUS = "**Status:**"
AMENDED_BY = "**Amended by:**"
MARKER = re.compile(r"\s*\*\[[^\]]*\]\*")
SUPERSEDED = re.compile(r"\*\[[^\]]*?superseded (?:in part )?by ADR-\d", re.IGNORECASE)
NAMED = re.compile(r"ADR-(\d{3})((?:/\d{3})*)")
REQUIREMENT = re.compile(r"\b((?:N?FR)-[A-Z]+)-\d")
SPEC_SECTION = re.compile(r"^(#{2,3}) (?:(\d+(?:\.\d+)*)\.? )?(.*)$")
SPEC_FAMILY = re.compile(r"\(((?:N?FR)-[A-Z]+)\)")

CURATED: Mapping[str, tuple[str, ...]] = {
    # Links the hand-built index made (before #889) from an ADR to a requirement area whose ids
    # the ADR's text does not name. They cannot be derived, so they are listed here.
    "149": ("NFR-SEC",),
    "150": ("NFR-PORT",),
    "158": ("FR-PROF",),
    "162": ("FR-OBS",),
    "165": ("FR-PROP", "FR-VERIFY"),
    "167": ("FR-RET", "FR-PROP"),
    "168": ("FR-BELIEF",),
    "169": ("FR-MEM", "FR-GUARD"),
    "170": ("FR-MODEL",),
    "173": ("FR-VERIFY",),
    "174": ("FR-EPI", "FR-VERIFY"),
    "175": ("FR-CONC", "NFR-REL", "NFR-SCALE"),
    "182": ("FR-PROF", "FR-IMG"),
    "191": ("NFR-SCALE",),
}


class AdrIndexError(Exception):
    """The log or docs/03 cannot be indexed as written."""


@dataclass(frozen=True)
class Adr:
    number: str
    title: str
    lines: tuple[str, ...]

    @property
    def amended_by(self) -> list[str]:
        return [line for line in self.lines if line.startswith(AMENDED_BY)]

    @property
    def standing(self) -> str:
        if any(line.startswith(f"{STATUS} Superseded") for line in self.lines):
            return "Superseded"
        if any(SUPERSEDED.search(line) for line in self.lines if not line.startswith(AMENDED_BY)):
            return "Partly superseded"
        return "Amended" if self.amended_by else "Current"

    @property
    def read_with(self) -> list[str]:
        named: list[str] = []
        for line in self.amended_by:
            for match in NAMED.finditer(line):
                for number in [match.group(1), *re.findall(r"\d{3}", match.group(2))]:
                    if number not in named:
                        named.append(number)
        return named

    @property
    def cited_families(self) -> set[str]:
        return {
            family
            for line in self.lines
            if not line.startswith((STATUS, AMENDED_BY))
            for family in REQUIREMENT.findall(line)
        }


def parse_adrs(log: str) -> list[Adr]:
    """Every ADR in the log, in log order."""
    adrs: list[Adr] = []
    number: str | None = None
    title = ""
    body: list[str] = []
    for line in [*log.splitlines(), "---"]:
        heading = HEADING.match(line)
        if number is not None and (heading or line == "---"):
            adrs.append(Adr(number, MARKER.sub("", title).strip(), tuple(body)))
            number = None
        if heading:
            number, title, body = heading.group(1), heading.group(2), []
        elif number is not None:
            body.append(line)
    seen = [adr.number for adr in adrs]
    if len(seen) != len(set(seen)):
        raise AdrIndexError(f"the log repeats an ADR number: {sorted(seen)}")
    return adrs


def parse_areas(spec: str) -> dict[str, str]:
    """Each requirement family docs/03 heads a section with, in order, with its § number."""
    areas: dict[str, str] = {}
    chapter = ""
    for line in spec.splitlines():
        section = SPEC_SECTION.match(line)
        if section is None:
            continue
        level, number, text = section.groups()
        if level == "##":
            chapter = number or ""
        family = SPEC_FAMILY.search(text)
        if family and (number or chapter):
            areas.setdefault(family.group(1), f"§{number or chapter}")
    return areas


def area_adrs(
    adrs: Sequence[Adr], areas: Mapping[str, str], curated: Mapping[str, tuple[str, ...]]
) -> dict[str, list[str]]:
    """Each area's ADRs, oldest first (areas with none are left out)."""
    by_number = {adr.number: adr for adr in adrs}
    listed: dict[str, set[str]] = {}
    for adr in adrs:
        for family in adr.cited_families:
            if family not in areas:
                raise AdrIndexError(
                    f"ADR-{adr.number} cites {family}, which docs/03 does not define"
                )
            listed.setdefault(family, set()).add(adr.number)
    for number, families in curated.items():
        if number not in by_number:
            raise AdrIndexError(f"CURATED names ADR-{number}, which the log does not have")
        for family in families:
            if family not in areas:
                raise AdrIndexError(f"CURATED names {family}, which docs/03 does not define")
            if family in by_number[number].cited_families:
                raise AdrIndexError(
                    f"CURATED lists ADR-{number} under {family}, which its text already cites; "
                    "remove the entry"
                )
            listed.setdefault(family, set()).add(number)
    return {family: sorted(listed[family]) for family in areas if family in listed}


def area_table(
    adrs: Sequence[Adr], areas: Mapping[str, str], curated: Mapping[str, tuple[str, ...]]
) -> list[str]:
    rows = ["| Area | Spec | ADRs |", "|---|---|---|"]
    for family, numbers in area_adrs(adrs, areas, curated).items():
        rows.append(f"| {family} | {areas[family]} | {', '.join(numbers)} |")
    return rows


def adr_table(adrs: Sequence[Adr]) -> list[str]:
    rows = ["| ADR | Title | Standing | Read with |", "|---|---|---|---|"]
    for adr in adrs:
        read_with = ", ".join(adr.read_with) or "—"
        rows.append(f"| {adr.number} | {adr.title} | {adr.standing} | {read_with} |")
    return rows


def _replace_table(lines: list[str], heading: str, table: list[str]) -> list[str]:
    """``lines`` with the first table after ``heading`` replaced by ``table``."""
    if lines.count(heading) != 1:
        raise AdrIndexError(f"the log needs exactly one {heading!r} heading")
    start = lines.index(heading) + 1
    while start < len(lines) and not lines[start].startswith("|"):
        if lines[start].startswith("#"):
            raise AdrIndexError(f"no table under {heading!r}")
        start += 1
    end = start
    while end < len(lines) and lines[end].startswith("|"):
        end += 1
    if start == end:
        raise AdrIndexError(f"no table under {heading!r}")
    return [*lines[:start], *table, *lines[end:]]


def render(log: str, spec: str, curated: Mapping[str, tuple[str, ...]] | None = None) -> str:
    """The log with both indexes regenerated from it (``curated`` defaults to ``CURATED``)."""
    curated = CURATED if curated is None else curated
    adrs = parse_adrs(log)
    lines = log.split("\n")
    lines = _replace_table(lines, AREA_HEADING, area_table(adrs, parse_areas(spec), curated))
    lines = _replace_table(lines, ADR_HEADING, adr_table(adrs))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=PROG, description="Generate the ADR log's indexes from the log."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="exit 1 when an index is stale")
    mode.add_argument("--write", action="store_true", help="regenerate both indexes")
    parser.add_argument("--path", type=Path, default=LOG_PATH, help=argparse.SUPPRESS)
    parser.add_argument("--spec", type=Path, default=SPEC_PATH, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    generated = render(args.path.read_text("utf-8"), args.spec.read_text("utf-8"))
    if args.check:
        return check(args.path, generated, regenerate=f"uv run python -m {PROG} --write")
    return write(args.path, generated)


if __name__ == "__main__":
    raise SystemExit(main())
