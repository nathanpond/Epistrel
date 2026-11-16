"""The schema-compile probe (#785) against the stdlib mock endpoint: no live call.

No test here carries a part marker: this is measurement tooling, not a requirement's behaviour.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest

from epistrel.tools import schema_compile_probe as probe
from tests.model_support import mock_openai

KEY = "sk-probe-test-0000"


def test_every_schema_with_a_strict_form_is_probed() -> None:
    names = [name for name, _ in probe.schemas()]
    assert len(names) == len(set(names))
    for expected in ("GuardrailVerdict", "EntailmentVerdict", "DirectorOrder"):
        assert expected in names
    # No strict wire form, so never sent strictly: no grammar to compile.
    for absent in ("DirectiveParseResult", "CharacterRender", "NarratorRender"):
        assert absent not in names


def test_a_round_sends_each_schema_cold_then_warm_with_a_fresh_nonce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two rounds of two schemas: four calls a round, the cold and warm call of a schema in a
    round identical, each round's nonce new and required; the report names every schema and
    the thresholds, and never the key."""
    monkeypatch.setenv(probe.API_KEY_ENV, KEY)
    only = ["GuardrailVerdict", "EntailmentVerdict"]
    with mock_openai() as mock:
        out = io.StringIO()
        argv = ["--base-url", f"{mock.root}/v1", "--model", "m", "--rounds", "2"]
        with redirect_stdout(out):
            code = probe.main([*argv, *(a for name in only for a in ("--schema", name))])
    assert code == 0
    sent = [r.body for r in mock.requests if r.path.endswith("/chat/completions")]
    assert len(sent) == 2 * 2 * 2
    nonces = []
    for request in sent:
        spec = request["response_format"]["json_schema"]
        assert spec["strict"] is True
        assert request["max_completion_tokens"] == probe.MAX_TOKENS
        (added,) = [p for p in spec["schema"]["properties"] if p.startswith("probe_")]
        assert added in spec["schema"]["required"]
        nonces.append(added)
    assert sent[0] == sent[1]  # one schema's cold and warm call in a round
    assert len(set(nonces)) == 2  # one nonce a round
    text = out.getvalue()
    assert "| `GuardrailVerdict` |" in text
    assert "| `EntailmentVerdict` |" in text
    assert "45 s" in text
    assert "60 s" in text
    assert "210 s" in text
    assert KEY not in text


def test_without_a_key_it_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(probe.API_KEY_ENV, raising=False)
    assert probe.main(["--base-url", "http://x/v1", "--model", "m"]) == 2
