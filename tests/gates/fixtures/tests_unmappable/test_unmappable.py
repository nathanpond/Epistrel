import pytest


@pytest.mark.part("M2", "FR-STORE-10")
def test_1x() -> None:
    assert True


@pytest.mark.part("M2", "FR-STORE-10")
def test__x() -> None:
    assert True


@pytest.mark.part("M2", "FR-STORE-10")
def test_9_lives() -> None:
    assert True


@pytest.mark.part("M2", "FR-STORE-10")
def test_i_1x() -> None:
    assert True


def test_2y() -> None:
    """Covers FR-STORE-10. A docstring part is not a §15 entry, so its name is not checked."""
    assert True
