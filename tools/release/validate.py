"""Validate a `v*` tag against pyproject's version and the releases published so far, and pick the
floating tags a known-good version heads when it is re-promoted (#96)."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import Path

TAG_RE = re.compile(r"^v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:-rc\.(?P<rc>\d+))?$")
PYPROJECT_RC_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:(?:-rc\.|rc|\.rc|-rc)(\d+))?$")


class ReleaseError(Exception):
    """The tag must not be released; the message says why."""


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int
    rc: int | None = None

    @property
    def stable(self) -> bool:
        return self.rc is None

    @property
    def bare(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return base if self.rc is None else f"{base}-rc.{self.rc}"

    @property
    def pep440(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return base if self.rc is None else f"{base}rc{self.rc}"


def parse_tag(tag: str) -> Version:
    match = TAG_RE.match(tag.strip())
    if match is None:
        raise ReleaseError(
            f"tag {tag!r} must look like vX.Y.Z or vX.Y.Z-rc.N (the only pre-release form)"
        )
    rc = match.group("rc")
    return Version(
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        None if rc is None else int(rc),
    )


def parse_project_version(text: str) -> Version:
    match = PYPROJECT_RC_RE.match(text.strip())
    if match is None:
        raise ReleaseError(f"pyproject version {text!r} is not X.Y.Z or X.Y.ZrcN")
    rc = match.group(4)
    return Version(
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        None if rc is None else int(rc),
    )


def read_project_version(pyproject: Path) -> str:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    version = data.get("project", {}).get("version")
    if not isinstance(version, str):
        raise ReleaseError(f"{pyproject}: [project].version is missing")
    return version


@dataclass(frozen=True)
class Outputs:
    version: str
    prerelease: bool
    highest: bool
    highest_minor: bool
    highest_major: bool
    already_released: bool

    def lines(self) -> list[str]:
        def b(v: bool) -> str:
            return "true" if v else "false"

        return [
            f"version={self.version}",
            f"prerelease={b(self.prerelease)}",
            f"highest={b(self.highest)}",
            f"highest_minor={b(self.highest_minor)}",
            f"highest_major={b(self.highest_major)}",
            f"already_released={b(self.already_released)}",
        ]


def read_released(text: str) -> tuple[list[str], set[str]]:
    """`(released, demoted)` tags from `gh release list --json tagName,isDraft,isPrerelease`.

    Drafts are not released. A stable tag flagged prerelease is one a rollback demoted.
    """
    try:
        rows = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"released file is not `gh release list --json` output: {exc}") from exc
    if not isinstance(rows, list):
        raise ReleaseError("released file must be a JSON array of releases")
    released: list[str] = []
    demoted: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("isDraft", False):
            continue
        tag = str(row.get("tagName", ""))
        released.append(tag)
        if row.get("isPrerelease", False) and any(v.stable for v in _versions([tag])):
            demoted.add(
                tag
            )  # an rc is a pre-release by design; tags outside the scheme don't count
    return released, demoted


def _versions(tags: Iterable[str]) -> list[Version]:
    versions: list[Version] = []
    for tag in tags:
        try:
            versions.append(parse_tag(tag))
        except ReleaseError:
            continue  # tags outside the scheme never count as published releases
    return versions


def _heads(version: Version, rivals: Iterable[Version]) -> tuple[bool, bool, bool]:
    """Whether a stable `version` is above every stable rival overall, in its X.Y, in its X.

    The caller leaves out `version` itself and every demoted release: a rollback's demotion means
    that release no longer holds `latest`, `X.Y` or `X`.
    """
    stables = [r for r in rivals if r.stable]
    if not version.stable:
        return False, False, False
    same_minor = [r for r in stables if (r.major, r.minor) == (version.major, version.minor)]
    same_major = [r for r in stables if r.major == version.major]
    return (
        all(version > r for r in stables),
        all(version > r for r in same_minor),
        all(version > r for r in same_major),
    )


def validate(
    tag: str, project_version: str, released_tags: list[str], demoted: Collection[str] = ()
) -> Outputs:
    """Decide whether `tag` may be released and which floating tags it may move.

    `released_tags` are the `v*` tags whose release exists (a successful earlier run); `demoted` are
    the stable ones a rollback demoted, which never count as higher (the rule `retag` uses).
    """
    version = parse_tag(tag)
    project = parse_project_version(project_version)
    if version != project:
        raise ReleaseError(
            f"tag {tag} is version {version.pep440} but pyproject.toml says {project.pep440}; "
            "bump [project].version in the release PR before tagging"
        )
    released = _versions(released_tags)
    others = [r for r in released if r != version]
    if not version.stable:
        stable_same = Version(version.major, version.minor, version.patch)
        if stable_same in others:
            raise ReleaseError(
                f"{tag} is a pre-release of {stable_same.bare}, which is already released"
            )
    down = set(_versions(demoted))
    highest, highest_minor, highest_major = _heads(version, [r for r in others if r not in down])
    return Outputs(
        version=version.bare,
        prerelease=not version.stable,
        highest=highest,
        highest_minor=highest_minor,
        highest_major=highest_major,
        already_released=version in released,
    )


def retag(good: str, released_tags: list[str], demoted: Collection[str] = ()) -> list[str]:
    """The floating tags (`latest`, `X.Y`, `X`) a bare re-promotion of `good` re-points.

    `good` counts even while a rollback has it demoted; other demoted releases never count as
    higher.
    """
    version = parse_tag(f"v{good}")
    if not version.stable:
        raise ReleaseError(f"good version {good!r} must be bare X.Y.Z (no pre-release)")
    released = _versions(released_tags)
    if version not in released:
        raise ReleaseError(f"release v{good} is not published")
    down = set(_versions(demoted))
    rivals = [r for r in released if r != version and r not in down]
    highest, highest_minor, highest_major = _heads(version, rivals)
    names = ("latest", f"{version.major}.{version.minor}", f"{version.major}")
    return [n for n, on in zip(names, (highest, highest_minor, highest_major), strict=True) if on]
