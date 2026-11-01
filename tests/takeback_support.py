"""What the M5 helpers leave on the shared database, taken back after each test (#517).

A ``profile_defaults`` write validates every profile version in use and every story's overrides
on the shared session database (``core.profiles.effective.runnable_under``), so a test that
leaves a story's overrides or a profile of its own behind can fail a later, unrelated test
(``test_lite_defaults_write_is_recorded``). The helpers that write them record what they wrote
here: the story overrides of ``perception_support.perceive``, ``testimony_support
.enable_testimony``, ``nested_support.enable_nested`` and ``multi_support.per_character``, and
the profiles ``seeded_partitions_support.partitioned_profile`` creates. ``tests/conftest.py``'s
autouse ``_m5_leftovers_taken_back`` calls ``take_back`` after every test: the override keys go,
and each profile is deleted, its stories moved to the seeded ``Structured`` profile first (a
deleted profile's last version still counts as in use while a story runs on it).

A helper given a scratch database records ids the shared database does not hold: taking those
back changes nothing there, and the scratch database is its test's own."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import Connection, text

from epistrel.core.seeds.profiles import STRUCTURED_M4_PROFILE_ID

_OVERRIDES: dict[str, set[str]] = {}
_PROFILES: set[str] = set()

_FORGET = text(
    "UPDATE story_profile_overrides SET settings = settings - CAST(:keys AS text[]) "
    "WHERE story_id = :s"
)
_MOVE = text(
    "UPDATE stories SET engine_profile_id = :structured, engine_profile_version_pin = NULL "
    "WHERE engine_profile_id = :p"
)
_DELETE = text(
    "UPDATE engine_profiles SET deleted_at = now(), updated_at = now() "
    "WHERE id = :p AND deleted_at IS NULL"
)


def overridden(story_id: str, keys: Iterable[str]) -> None:
    """The story's overrides now set these top-level settings (module keys)."""
    _OVERRIDES.setdefault(story_id, set()).update(keys)


def created_profile(profile_id: str) -> None:
    """A test created this profile."""
    _PROFILES.add(profile_id)


def pending() -> tuple[dict[str, set[str]], set[str]]:
    """What is recorded and not yet taken back (a copy)."""
    return {s: set(k) for s, k in _OVERRIDES.items()}, set(_PROFILES)


def clear() -> None:
    """Forget what is recorded without touching any database."""
    _OVERRIDES.clear()
    _PROFILES.clear()


def take_back(conn: Connection) -> None:
    """Remove the recorded override keys and delete the recorded profiles (see the module
    docstring), in ``conn``'s transaction; the record is cleared."""
    try:
        for story_id, keys in sorted(_OVERRIDES.items()):
            conn.execute(_FORGET, {"s": story_id, "keys": sorted(keys)})
        for profile_id in sorted(_PROFILES):
            conn.execute(_MOVE, {"p": profile_id, "structured": STRUCTURED_M4_PROFILE_ID})
            conn.execute(_DELETE, {"p": profile_id})
    finally:
        clear()


__all__ = ["clear", "created_profile", "overridden", "pending", "take_back"]
