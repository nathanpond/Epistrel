import pytest

from epistrel.config import ConfigError, Settings, load_settings

CREDENTIAL = "c" * 32


def test_settings_load_from_env(monkeypatch: pytest.MonkeyPatch, dummy_dsn: str) -> None:
    monkeypatch.setenv("EPISTREL_SERVICE_CREDENTIALS", CREDENTIAL)
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
    monkeypatch.setenv("EPISTREL_SERVICE_CREDENTIALS", CREDENTIAL)
    monkeypatch.setenv("EPISTREL_DATABASE_URL", dummy_dsn)
    monkeypatch.setenv("EPISTREL_FOO", "1")
    with pytest.raises(ConfigError) as exc:
        load_settings()
    assert exc.value.lines == ["error: EPISTREL_FOO: unknown setting"]


def test_test_prefix_is_not_a_setting(monkeypatch: pytest.MonkeyPatch, dummy_dsn: str) -> None:
    monkeypatch.setenv("EPISTREL_SERVICE_CREDENTIALS", CREDENTIAL)
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


def test_service_credentials_split_and_trimmed(
    monkeypatch: pytest.MonkeyPatch, dummy_dsn: str
) -> None:
    """FR-AUTH-1: several comma-separated credentials, all kept, trimmed, empties dropped."""
    monkeypatch.setenv("EPISTREL_DATABASE_URL", dummy_dsn)
    monkeypatch.setenv("EPISTREL_SERVICE_CREDENTIALS", f" {'a' * 32} ,{'b' * 40},, ")
    creds = [c.get_secret_value() for c in load_settings().service_credentials]
    assert creds == ["a" * 32, "b" * 40]


def test_credentials_required_unless_auth_disabled(
    monkeypatch: pytest.MonkeyPatch, dummy_dsn: str
) -> None:
    monkeypatch.setenv("EPISTREL_DATABASE_URL", dummy_dsn)
    with pytest.raises(ConfigError) as exc:
        load_settings()
    assert exc.value.lines == [
        "error: EPISTREL_SERVICE_CREDENTIALS: at least one credential of 32+ characters is required"
    ]
    monkeypatch.setenv("EPISTREL_AUTH_DISABLED", "1")
    monkeypatch.setenv("EPISTREL_DEBUG", "true")
    assert load_settings().service_credentials == []


def test_short_credential_rejected_without_leaking_value(
    monkeypatch: pytest.MonkeyPatch, dummy_dsn: str
) -> None:
    monkeypatch.setenv("EPISTREL_DATABASE_URL", dummy_dsn)
    monkeypatch.setenv("EPISTREL_SERVICE_CREDENTIALS", f"{'a' * 32},short-secret")
    with pytest.raises(ConfigError) as exc:
        load_settings()
    assert exc.value.lines == [
        "error: EPISTREL_SERVICE_CREDENTIALS: credential #2 is shorter than 32 characters"
    ]
    assert "short-secret" not in str(exc.value)


def test_auth_disabled_refused_without_debug(
    monkeypatch: pytest.MonkeyPatch, dummy_dsn: str
) -> None:
    monkeypatch.setenv("EPISTREL_DATABASE_URL", dummy_dsn)
    monkeypatch.setenv("EPISTREL_SERVICE_CREDENTIALS", CREDENTIAL)
    monkeypatch.setenv("EPISTREL_AUTH_DISABLED", "1")
    with pytest.raises(ConfigError) as exc:
        load_settings()
    assert exc.value.lines == [
        "error: EPISTREL_AUTH_DISABLED: is refused unless EPISTREL_DEBUG is on"
    ]


def test_config_key_is_a_known_secret_setting(
    monkeypatch: pytest.MonkeyPatch, dummy_dsn: str
) -> None:
    monkeypatch.setenv("EPISTREL_DATABASE_URL", dummy_dsn)
    monkeypatch.setenv("EPISTREL_SERVICE_CREDENTIALS", CREDENTIAL)
    monkeypatch.setenv("EPISTREL_CONFIG_KEY", "k" * 44)
    settings = load_settings()
    assert settings.config_key is not None
    assert "k" * 44 not in repr(settings)
    assert Settings.model_fields["config_key"].default is None
