import pytest


@pytest.mark.part("M3", "FR-GUARD-7", interim_until="M3")
def test_i_95() -> None:
    assert True


@pytest.mark.part("M3", "FR-GUARD-7", interim_until="M2")
def test_i_94() -> None:
    assert True
