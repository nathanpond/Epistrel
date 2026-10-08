import pytest

from epistrel.config import ConfigError, Settings, load_settings


def test_settings_load_from_env(monkeypatch: pytest.MonkeyPatch, dummy_dsn: str) -> None:
    monkeypatch.setenv("EPISTREL_DATABASE_URL", dummy_dsn)
    monkeypatch.setenv("EPISTREL_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("EPISTREL_PORT", "9000")
    settings = load_settings()
    assert isinstance(settings, Settings)
    assert settings.database_url.get_secret_value() == dummy_dsn
    assert settings.log_level == "DEBUG"
    assert settings.port == 9000
    assert settings.host == "0.0.0.0"  # noqa: S104 - asserting the documented default
    assert settings.debug is False


def test_missing_database_url_is_named() -> None:
    with pytest.raises(ConfigError) as exc:
        load_settings()
    assert exc.value.lines == ["error: EPISTREL_DATABASE_URL: is required"]


def test_non_postgres_url_rejected_without_leaking_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EPISTREL_DATABASE_URL", "sqlite:///x")
    with pytest.raises(ConfigError) as exc:
        load_settings()
    assert exc.value.lines == ["error: EPISTREL_DATABASE_URL: must be a postgresql+psycopg:// URL"]
    assert "sqlite" not in "\n".join(exc.value.lines)


def test_unknown_variable_rejected(monkeypatch: pytest.MonkeyPatch, dummy_dsn: str) -> None:
    monkeypatch.setenv("EPISTREL_DATABASE_URL", dummy_dsn)
    monkeypatch.setenv("EPISTREL_FOO", "1")
    with pytest.raises(ConfigError) as exc:
        load_settings()
    assert exc.value.lines == ["error: EPISTREL_FOO: unknown setting"]


def test_test_prefix_is_not_a_setting(monkeypatch: pytest.MonkeyPatch, dummy_dsn: str) -> None:
    monkeypatch.setenv("EPISTREL_DATABASE_URL", dummy_dsn)
    monkeypatch.setenv("EPISTREL_TEST_DATABASE_URL", dummy_dsn)
    assert load_settings().port == 8000


def test_all_problems_reported_together(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EPISTREL_BAR", "1")
    with pytest.raises(ConfigError) as exc:
        load_settings()
    assert exc.value.lines == [
        "error: EPISTREL_DATABASE_URL: is required",
        "error: EPISTREL_BAR: unknown setting",
    ]
