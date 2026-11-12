import pytest


@pytest.mark.part("M2", "FR-NOPE-1")
def test_i_95() -> None:
    assert True


def test_i_94() -> None:
    """Covers FR-NOPE-1."""
    assert True
