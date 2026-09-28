"""Connection and server settings for the Open WebUI MCP server.

Everything is read from environment variables (and an optional ``.env`` file),
the standard way MCP clients inject config. Precedence: first defined env var
wins, so the common aliases act as fallbacks for each other.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

logger = logging.getLogger(__name__)

# Accepted env var names, in precedence order, for each setting.
_BASE_URL_VARS = ("OPENWEBUI_BASE_URL", "OPENWEBUI_URL", "OWUI_URL")
_TOKEN_VARS = ("OPENWEBUI_API_KEY", "OWUI_API_KEY")
_DEFAULT_MODEL_VARS = ("OPENWEBUI_DEFAULT_MODEL", "OWUI_DEFAULT_MODEL")
_ENFORCE_DEFAULT_MODEL_VARS = (
    "OPENWEBUI_ENFORCE_DEFAULT_MODEL",
    "OWUI_ENFORCE_DEFAULT_MODEL",
)
_TRANSPORT_VARS = ("OPENWEBUI_MCP_TRANSPORT", "OWUI_MCP_TRANSPORT")
_SSL_CA_VARS = ("OPENWEBUI_CA_BUNDLE", "OWUI_CA_BUNDLE")
_SSL_VERIFY_VARS = ("OPENWEBUI_SSL_VERIFY", "OWUI_SSL_VERIFY")
_ASK_DESCRIPTION_VARS = ("OPENWEBUI_ASK_DESCRIPTION", "OWUI_ASK_DESCRIPTION")
_INSTRUCTIONS_VARS = ("OPENWEBUI_INSTRUCTIONS", "OWUI_INSTRUCTIONS")

# MCP transports FastMCP can run on; validated at config time.
Transport = Literal["stdio", "sse", "streamable-http"]
_VALID_TRANSPORTS: tuple[str, ...] = ("stdio", "sse", "streamable-http")


def _first(*names: str, default: str | None = None) -> str | None:
    """Return the first env var from ``names`` that is set (non-empty)."""
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


def load_dotenv(path: str | Path | None = None) -> None:
    """Load a ``.env`` file next to the current working directory.

    Uses python-dotenv if installed (it ships with the ``mcp`` dependency);
    does nothing when the file is absent. Harmless no-op otherwise.
    """
    try:
        from dotenv import load_dotenv as _load
    except ImportError:  # pragma: no cover - dotenv is a transitive dep
        logger.debug("python-dotenv not available; skipping .env load")
        return
    target = Path(path) if path else Path.cwd() / ".env"
    if target.exists():
        _load(target)


@dataclass(frozen=True)
class Settings:
    """Resolved server configuration.

    ``base_url`` is required: the Open WebUI server the SDK talks to. On
    streamable-http and SSE, every caller supplies their own identity per
    request, via the ``apiKey`` query parameter or an ``Authorization:
    Bearer`` header (see ``server.resolve_request_token``) - ``token`` is
    never read on those transports, so one caller's key can't leak into
    another's request. stdio has no per-request channel to carry a
    credential at all (no URL, no headers), so ``token`` (from
    ``OPENWEBUI_API_KEY``) is used there as the only way to authenticate;
    without it, every stdio tool call fails with a clear error.
    """

    base_url: str
    # Fallback Open WebUI identity for stdio ONLY (no per-request channel
    # exists there). Never consulted on streamable-http/SSE - see
    # server.resolve_request_token.
    token: str | None = None
    name: str = "openwebui"
    # Model used by ``ask`` when the caller does not pass one explicitly.
    default_model: str | None = None
    # When true, ``ask`` always uses ``default_model`` and ignores the caller's
    # ``model`` argument (must be paired with ``default_model``).
    enforce_default_model: bool = False
    timeout_ms: int = 120_000
    transport: Transport = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000
    # TLS options for talking to Open WebUI over https. ``ssl_ca_bundle`` is a
    # PEM CA file to trust (private/Traefik CA, or this env's mitm CA);
    # ``ssl_verify`` disables certificate verification for the JSON routes.
    ssl_ca_bundle: str | None = None
    ssl_verify: bool = True
    # Custom description for the ``ask`` tool; when set it replaces the built-in
    # docstring description. Lets operators inject agent-facing instructions.
    ask_description: str | None = None
    # Server-wide ``instructions`` surfaced in the MCP initialize response.
    # Codex and other hosts read it as usage guidance for the whole server, so
    # this is where to tell agents to use the tools proactively.
    instructions: str | None = None

    @classmethod
    def from_env(cls, **overrides: str) -> Settings:
        """Build settings from env vars; ``overrides`` win for tests/callers."""
        base_url = _first(*_BASE_URL_VARS) or overrides.get("base_url")
        if not base_url:
            raise ValueError(
                "Open WebUI connection not configured: set OPENWEBUI_BASE_URL "
                "(or the alias OPENWEBUI_URL)"
            )
        default_model = overrides.get("default_model") or _first(
            *_DEFAULT_MODEL_VARS
        )
        transport = (
            overrides.get("transport") or _first(*_TRANSPORT_VARS) or "stdio"
        )
        if transport not in _VALID_TRANSPORTS:
            raise ValueError(
                f"invalid transport {transport!r}; pick one of "
                + ", ".join(_VALID_TRANSPORTS)
            )
        # Membership in _VALID_TRANSPORTS (exactly the three Literal values)
        # makes this cast sound; it just narrows ``str`` back to ``Transport``.
        transport = cast(Transport, transport)
        try:
            timeout_ms = int(
                overrides.get("timeout_ms")
                or os.getenv("OWUI_TIMEOUT_MS", "120000")
            )
        except ValueError as exc:
            raise ValueError(f"invalid OWUI_TIMEOUT_MS: {exc}") from exc
        try:
            port = int(overrides.get("port", "8000"))
        except ValueError as exc:
            raise ValueError(f"invalid port: {exc}") from exc
        verify_raw = (
            overrides.get("ssl_verify")
            or _first(*_SSL_VERIFY_VARS, default="true")
            or "true"
        )
        enforce_raw = (
            overrides.get("enforce_default_model")
            or _first(*_ENFORCE_DEFAULT_MODEL_VARS, default="false")
            or "false"
        )
        return cls(
            base_url=base_url,
            token=overrides.get("token") or _first(*_TOKEN_VARS),
            name=overrides.get("name", "openwebui"),
            default_model=default_model,
            enforce_default_model=enforce_raw.strip().lower()
            not in ("0", "false", "no", "off"),
            transport=transport,
            timeout_ms=timeout_ms,
            host=overrides.get("host", "127.0.0.1"),
            port=port,
            ssl_ca_bundle=overrides.get("ssl_ca_bundle")
            or _first(*_SSL_CA_VARS),
            ssl_verify=verify_raw.strip().lower()
            not in ("0", "false", "no", "off"),
            ask_description=overrides.get("ask_description")
            or _first(*_ASK_DESCRIPTION_VARS),
            instructions=overrides.get("instructions")
            or _first(*_INSTRUCTIONS_VARS),
        )
