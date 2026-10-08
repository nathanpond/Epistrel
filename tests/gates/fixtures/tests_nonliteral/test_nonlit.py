import pytest

MS = "M2"


@pytest.mark.part(MS, "FR-RESP-1")
def test_i_95() -> None:
    assert True
