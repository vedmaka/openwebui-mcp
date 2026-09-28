"""Config resolution tests (env-driven Settings)."""

from __future__ import annotations

import pytest

from openwebui_mcp.config import Settings

# Clear any ambient Open WebUI env vars so tests are hermetic.
_AMBIENT = (
    "OPENWEBUI_BASE_URL",
    "OPENWEBUI_URL",
    "OWUI_URL",
    "OPENWEBUI_API_KEY",
    "OWUI_API_KEY",
    "OPENWEBUI_DEFAULT_MODEL",
    "OWUI_DEFAULT_MODEL",
    "OPENWEBUI_ENFORCE_DEFAULT_MODEL",
    "OWUI_ENFORCE_DEFAULT_MODEL",
    "OWUI_TIMEOUT_MS",
    "OPENWEBUI_SSL_VERIFY",
    "OPENWEBUI_CA_BUNDLE",
    "OPENWEBUI_ASK_DESCRIPTION",
    "OWUI_ASK_DESCRIPTION",
    "OPENWEBUI_INSTRUCTIONS",
    "OWUI_INSTRUCTIONS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _AMBIENT:
        monkeypatch.delenv(name, raising=False)


def test_from_env_basic(monkeypatch: pytest.MonkeyPatch) -> None:
    """base_url alone is enough; token is optional (stdio-only fallback)."""
    monkeypatch.setenv("OPENWEBUI_BASE_URL", "http://owui:8080")
    s = Settings.from_env()
    assert s.base_url == "http://owui:8080"
    assert s.token is None
    assert s.transport == "stdio"
    assert s.timeout_ms == 120_000


def test_token_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENWEBUI_BASE_URL", "http://h:1")
    monkeypatch.setenv("OPENWEBUI_API_KEY", "sk-configured")
    assert Settings.from_env().token == "sk-configured"


def test_token_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENWEBUI_BASE_URL", "http://h:1")
    monkeypatch.setenv("OWUI_API_KEY", "sk-alias")
    assert Settings.from_env().token == "sk-alias"


def test_from_env_missing_base_url_raises() -> None:
    with pytest.raises(ValueError, match="not configured"):
        Settings.from_env()


def test_from_env_alias_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    # First listed alias wins over the later fallbacks.
    monkeypatch.setenv("OPENWEBUI_BASE_URL", "first")
    monkeypatch.setenv("OPENWEBUI_URL", "second")
    s = Settings.from_env()
    assert s.base_url == "first"


def test_from_env_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENWEBUI_BASE_URL", "http://h:1")
    monkeypatch.setenv("OWUI_TIMEOUT_MS", "30000")
    s = Settings.from_env()
    assert s.timeout_ms == 30_000


def test_from_env_invalid_transport_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENWEBUI_BASE_URL", "http://h:1")
    monkeypatch.setenv("OPENWEBUI_MCP_TRANSPORT", "telepathy")
    with pytest.raises(ValueError, match="invalid transport"):
        Settings.from_env()


def test_from_env_overrides_win(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENWEBUI_BASE_URL", "http://h:1")
    s = Settings.from_env(transport="streamable-http", name="custom")
    assert s.transport == "streamable-http"
    assert s.name == "custom"


def test_tls_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENWEBUI_BASE_URL", "http://h:1")
    monkeypatch.setenv("OPENWEBUI_SSL_VERIFY", "false")
    monkeypatch.setenv("OPENWEBUI_CA_BUNDLE", "/etc/owui-ca.pem")
    s = Settings.from_env()
    assert not s.ssl_verify
    assert s.ssl_ca_bundle == "/etc/owui-ca.pem"


def test_tls_verify_defaults_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENWEBUI_BASE_URL", "http://h:1")
    assert Settings.from_env().ssl_verify  # truthy when "true"
    assert Settings.from_env().ssl_ca_bundle is None


def test_default_model_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENWEBUI_BASE_URL", "http://h:1")
    monkeypatch.setenv("OPENWEBUI_DEFAULT_MODEL", "sample-workspace-model-1")
    assert Settings.from_env().default_model == "sample-workspace-model-1"


def test_default_model_alias_and_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENWEBUI_BASE_URL", "http://h:1")
    # alias works
    monkeypatch.setenv("OWUI_DEFAULT_MODEL", "m-alias")
    assert Settings.from_env().default_model == "m-alias"
    # unset stays None
    monkeypatch.delenv("OWUI_DEFAULT_MODEL")
    assert Settings.from_env().default_model is None
    # explicit overrides env takes precedence
    monkeypatch.setenv("OWUI_DEFAULT_MODEL", "env-model")
    assert (
        Settings.from_env(default_model="override-model").default_model
        == "override-model"
    )


def test_enforce_default_model_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENWEBUI_BASE_URL", "http://h:1")
    monkeypatch.setenv("OPENWEBUI_DEFAULT_MODEL", "m1")
    # defaults to False when unset
    assert not Settings.from_env().enforce_default_model
    # true form
    monkeypatch.setenv("OPENWEBUI_ENFORCE_DEFAULT_MODEL", "true")
    assert Settings.from_env().enforce_default_model
    # falsey forms
    monkeypatch.setenv("OPENWEBUI_ENFORCE_DEFAULT_MODEL", "0")
    assert not Settings.from_env().enforce_default_model
    monkeypatch.setenv("OPENWEBUI_ENFORCE_DEFAULT_MODEL", "false")
    assert not Settings.from_env().enforce_default_model
    # alias works
    monkeypatch.delenv("OPENWEBUI_ENFORCE_DEFAULT_MODEL")
    monkeypatch.setenv("OWUI_ENFORCE_DEFAULT_MODEL", "1")
    assert Settings.from_env().enforce_default_model


def test_ask_description_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENWEBUI_BASE_URL", "http://h:1")
    # unset stays None (fallback to built-in docstring in the server)
    assert Settings.from_env().ask_description is None
    monkeypatch.setenv("OPENWEBUI_ASK_DESCRIPTION", "Always answer in German")
    assert Settings.from_env().ask_description == "Always answer in German"
    # alias works
    monkeypatch.delenv("OPENWEBUI_ASK_DESCRIPTION")
    monkeypatch.setenv("OWUI_ASK_DESCRIPTION", "alias text")
    assert Settings.from_env().ask_description == "alias text"


def test_instructions_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENWEBUI_BASE_URL", "http://h:1")
    # unset stays None
    assert Settings.from_env().instructions is None
    monkeypatch.setenv(
        "OPENWEBUI_INSTRUCTIONS", "Always use ask() for genealogy questions"
    )
    assert (
        Settings.from_env().instructions == "Always use ask() for genealogy questions"
    )
    # alias works
    monkeypatch.delenv("OPENWEBUI_INSTRUCTIONS")
    monkeypatch.setenv("OWUI_INSTRUCTIONS", "use tools proactively")
    assert Settings.from_env().instructions == "use tools proactively"
