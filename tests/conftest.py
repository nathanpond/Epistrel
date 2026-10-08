import os

import pytest

DUMMY_DSN = "postgresql+psycopg://u:p@localhost:5432/epistrel_test"


@pytest.fixture(autouse=True)
def _clean_epistrel_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every EPISTREL_* variable so tests see only what they set (EPISTREL_TEST_* kept)."""
    for name in list(os.environ):
        if name.upper().startswith("EPISTREL_") and not name.upper().startswith("EPISTREL_TEST_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def dummy_dsn() -> str:
    return DUMMY_DSN
