import pytest
from pytest import mark


@pytest.mark.part("M2", "FR-STORE-10..12; FR-CONC-1,2")
def test_i_10__event_log() -> None:
    """Event log round trip."""
    assert True


@pytest.mark.part("M4", "FR-COMMIT-1..3")
@pytest.mark.part(milestone="M5", requirements="FR-COMMIT-4")
async def test_u_01() -> None:
    assert True


@mark.part("M3", "FR-PROF-7", interim_until="M7")
def test_i_27__interim() -> None:
    assert True


class TestGrouped:
    @pytest.mark.part("M2", "FR-RESP-1")
    def test_s_double_submit(self) -> None:
        assert True


def test_plain_unmarked() -> None:
    """No requirement here, so not a part."""
    assert True


def test_outbox() -> None:
    """Property test OUTBOX covers FR-RESP-2 and FR-CONC-4..5."""
    assert True
