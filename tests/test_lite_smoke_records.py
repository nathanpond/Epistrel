"""#1064 item 1: the P-04 smoke runner reads Locust's records, and says why when there are none."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from perf.lite_smoke.__main__ import read_records


def test_records_are_read_line_by_line(tmp_path: Path) -> None:
    rows = [{"event": "submit", "tick": 1}, {"event": "done", "tick": 1}]
    (tmp_path / "records.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n\n")
    assert read_records(tmp_path, 0) == rows


def test_no_records_names_locust_and_the_perf_group(tmp_path: Path) -> None:
    """Complement: a run where Locust never started (``No module named locust``, exit 1) fails
    with that cause, not a FileNotFoundError."""
    with pytest.raises(SystemExit, match=r"locust exited 1 and wrote no records\.jsonl") as caught:
        read_records(tmp_path, 1)
    assert "uv sync --group perf" in str(caught.value)
