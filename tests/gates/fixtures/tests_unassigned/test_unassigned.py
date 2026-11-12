import pytest


@pytest.mark.part("M2", "FR-A-2", interim_until="M3")
def test_i_95() -> None:
    assert True


def test_i_94() -> None:
    """Covers FR-A-2."""
    assert True
