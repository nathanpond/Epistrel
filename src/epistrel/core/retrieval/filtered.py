"""An already viewpoint-filtered collection (FR-RET-7, NFR-PRIV-4, U-31; #170).

``ViewpointFiltered[T]`` is what the context assembler (``core.retrieval.assemble.assemble``)
accepts instead of a plain iterable: the items and the viewpoint string that filtered them. Its
constructor is sealed: a value made without the module-private ``_SEAL`` raises ``TypeError``,
so only the modules that apply a viewpoint filter make one: retrieval's pipeline, after #159's
filter composed into every arm (``core.retrieval.pipeline.retrieve``), and a character's context
assembly (``core.epistemic.context``). mypy strict enforces the boundary at the call sites and
``assemble``'s runtime ``isinstance`` check backs it, so the assembler cannot widen what a
character sees: it only places, in priority order, what the filter already let through.
``tests/test_u_31.py`` checks that ``_seal`` is called nowhere else.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

_SEAL = object()


@dataclass(frozen=True, slots=True)
class ViewpointFiltered[T]:
    """``items`` as ``viewpoint`` may read them (see the module docstring)."""

    items: tuple[T, ...]
    viewpoint: str
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _SEAL:
            raise TypeError(
                "a ViewpointFiltered set is made only by the module that filtered it "
                "(core.retrieval.pipeline, core.epistemic.context)"
            )

    def __iter__(self) -> Iterator[T]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)


def _seal[T](items: Iterable[T], viewpoint: str) -> ViewpointFiltered[T]:
    """``items``, already filtered for ``viewpoint`` by the caller, as a ``ViewpointFiltered``.
    Module-private by convention: only the filtering modules call it."""
    return ViewpointFiltered(tuple(items), viewpoint, _SEAL)


__all__ = ["ViewpointFiltered"]
