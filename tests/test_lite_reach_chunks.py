"""The Lite reach pass's chunking (#700; FR-LITE-8): at most ``lite_reach_chunk_messages``
lines a chunk, closed early when the next line would pass ``lite_reach_chunk_tokens`` (#53's
estimate: four characters a token), and a single line over the token cap left unsearched."""

from __future__ import annotations

from epistrel.orchestrator.rewrite.lite import _Line, chunked


def _line(n: int, chars: int) -> _Line:
    return _Line(turn=n, speaker="Ada", text="x" * chars)


def test_chunks_close_at_the_line_cap() -> None:
    lines = [_line(n, 4) for n in range(1, 8)]
    chunks, skipped = chunked(lines, 3, 1_000)
    assert [len(c) for c in chunks] == [3, 3, 1]
    assert skipped == 0
    assert [line.turn for c in chunks for line in c] == list(range(1, 8))


def test_chunks_close_early_at_the_token_cap() -> None:
    # 400 characters = 100 tokens a line; 250 tokens a chunk holds two lines.
    lines = [_line(n, 400) for n in range(1, 6)]
    chunks, skipped = chunked(lines, 60, 250)
    assert [len(c) for c in chunks] == [2, 2, 1]
    assert skipped == 0


def test_a_line_over_the_token_cap_is_left_unsearched() -> None:
    lines = [_line(1, 40), _line(2, 4_000), _line(3, 40)]
    chunks, skipped = chunked(lines, 60, 500)
    assert skipped == 1
    assert [line.turn for c in chunks for line in c] == [1, 3]
