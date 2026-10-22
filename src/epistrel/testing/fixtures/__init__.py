"""Generated fixtures for the performance suites (#153): ``scale`` builds the structured story
of ~50k memories / ~100k events that P-01, P-02 and P-06 measure (docs/04 §11). Dev tooling:
nothing in ``create_app`` or the Engine imports it."""

from epistrel.testing.fixtures.scale import ScaleFixture, ScaleSpec, build_scale_fixture

__all__ = ["ScaleFixture", "ScaleSpec", "build_scale_fixture"]
