"""Validate a `v*` tag against pyproject's version and the releases published so far."""

from __future__ import annotations

import re
import tomllib
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


def validate(tag: str, project_version: str, released_tags: list[str]) -> Outputs:
    """Decide whether `tag` may be released and which floating tags it may move.

    `released_tags` are the `v*` tags whose release exists (a successful earlier run).
    """
    version = parse_tag(tag)
    project = parse_project_version(project_version)
    if version != project:
        raise ReleaseError(
            f"tag {tag} is version {version.pep440} but pyproject.toml says {project.pep440}; "
            "bump [project].version in the release PR before tagging"
        )
    released: list[Version] = []
    for other in released_tags:
        try:
            released.append(parse_tag(other))
        except ReleaseError:
            continue  # tags outside the scheme never count as published releases
    others = [r for r in released if r != version]
    if not version.stable:
        stable_same = Version(version.major, version.minor, version.patch)
        if stable_same in others:
            raise ReleaseError(
                f"{tag} is a pre-release of {stable_same.bare}, which is already released"
            )
    stables = [r for r in others if r.stable]
    highest = version.stable and all(version > r for r in stables)
    same_minor = [r for r in stables if (r.major, r.minor) == (version.major, version.minor)]
    same_major = [r for r in stables if r.major == version.major]
    return Outputs(
        version=version.bare,
        prerelease=not version.stable,
        highest=highest,
        highest_minor=version.stable and all(version > r for r in same_minor),
        highest_major=version.stable and all(version > r for r in same_major),
        already_released=version in released,
    )
