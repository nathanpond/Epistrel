"""#222's import gate: only the history read reaches ``as_originally_recorded``."""

from __future__ import annotations

from tools.gates import as_recorded


def test_only_the_history_read_reaches_the_as_recorded_view() -> None:
    """The source holds no reference outside the allowed modules; a reference planted in a
    player-facing module is reported (complement)."""
    assert as_recorded.check() == []
    planted = "from epistrel.core.views import as_originally_recorded\n"
    assert as_recorded.scan_source("src/epistrel/core/fork.py", planted) == [
        ("src/epistrel/core/fork.py", 1)
    ]
