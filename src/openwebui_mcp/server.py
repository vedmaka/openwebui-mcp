"""FastMCP server exposing Open WebUI as MCP tools.

Two tools, per spec:

* ``ask`` - send a prompt to a model. The model is a required argument. Runs
  the Open WebUI tool-calling loop: any tools attached to the model are
  enabled automatically (``OpenWebUIClient.resolve_tools``) and results come
  back as a structured dict (answer, reasoning, tool_calls).
* ``list_models`` - list the models the connected Open WebUI user can see.

Authentication: the server itself talks to Open WebUI with a bearer token (as
``Authorization: Bearer``) carried by the SDK client. When a static MCP token
is configured (``OPENWEBUI_MCP_TOKEN``) the MCP endpoint additionally requires
the token as ``Authorization: Bearer <token>`` or the ``apiKey`` URL parameter.

Note on the event loop: the SDK ships a sync ``run_chat`` that wraps its async
Socket.IO runner in ``asyncio.run``. That works for the CLI (main thread, no
loop running) but NOT inside an async server: ``asyncio.run`` from a running
loop raises, and nesting it in ``asyncio.to_thread`` starves the socket
background tasks so completion events never arrive (the tool hangs). So the
``ask`` handler awaits the SDK's async ``sockets.run_chat_with_tools`` directly
on this event loop - one loop owns the aiohttp session, the socketio client and
the completion event, exactly like the CLI's single loop. Blocking SDK work
without tools (plain HTTP) is still offloaded via ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import secrets
import ssl
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, AuthProvider
from openwebui_sdk import OpenWebUIClient
from starlette.datastructures import MutableHeaders, QueryParams
from starlette.middleware import Middleware
from starlette.types import ASGIApp, Receive, Scope, Send

from openwebui_mcp.config import Settings

logger = logging.getLogger(__name__)

# SDK models attach tools under info.meta.toolIds; we surface that in
# list_models so callers can see which tools a model can invoke.
_TOOL_FIELDS = "tool_ids"


class ApiKeyQueryMiddleware:
    """Pass ``apiKey`` query credentials to FastMCP's Bearer auth backend."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = MutableHeaders(scope=scope)
            if headers.get("authorization") is None:
                api_key = QueryParams(scope.get("query_string", b"")).get("apiKey")
                if api_key:
                    headers["authorization"] = f"Bearer {api_key}"
        await self.app(scope, receive, send)


class StaticTokenVerifier(AuthProvider):
    """Accept exactly one configured Bearer or ``apiKey`` query token.

    FastMCP 4 wraps everything auth-related in a single ``AuthProvider``; the
    verifier IS the provider, so subclassing gives us the same fixed bearer
    token behavior the SDK v1 verifier had, without a real OAuth server. Query
    credentials pass through the same constant-time token verification.
    """

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def get_middleware(self) -> list[Middleware]:
        """Accept query credentials before FastMCP applies Bearer auth."""
        return [Middleware(ApiKeyQueryMiddleware), *super().get_middleware()]

    async def verify_token(self, token: str) -> AccessToken | None:
        if not secrets.compare_digest(token, self._token):
            return None
        return AccessToken(
            token=token,
            client_id="openwebui-mcp",
            scopes=["all"],
            subject=token,
        )


def ask_description(fn: Any, settings: Settings) -> str:
    """Description shown for the ``ask`` tool to MCP agents.

    The full docstring is always kept; ``settings.ask_description`` (from
    ``OPENWEBUI_ASK_DESCRIPTION``) only replaces the first summary line so
    operators can reword it without losing the stateless/history guidance.
    """
    doc = inspect.getdoc(fn) or ""
    summary, sep, rest = doc.partition("\n")
    summary = settings.ask_description or summary
    return f"{summary}{sep}{rest}".strip()


def apply_tls_settings(settings: Settings) -> None:
    """Apply TLS trust config to this process before any SDK request.

    ``OPENWEBUI_CA_BUNDLE`` - set ``SSL_CERT_FILE`` so every
    ``create_default_context`` caller (urllib for the JSON routes, aiohttp for
    the Socket.IO tool loop) trusts the given PEM CA. ``OPENWEBUI_SSL_VERIFY``
    false - drop certificate verification for the JSON (urllib) routes against
    self-signed / private-CA servers. No-op (and safe to call always) when
    neither is configured.
    """
    if settings.ssl_ca_bundle:
        os.environ["SSL_CERT_FILE"] = settings.ssl_ca_bundle
    if not settings.ssl_verify and hasattr(ssl, "_create_default_https_context"):
        ssl._create_default_https_context = ssl._create_unverified_context


def create_server(
    settings: Settings, *, client: OpenWebUIClient | None = None
) -> FastMCP:
    """Build a configured FastMCP instance exposing the Open WebUI tools.

    ``client`` is injectable for tests; when omitted a client is created from
    ``settings`` (base URL + bearer token). When ``settings.mcp_token`` is set
    the MCP endpoint accepts that token through ``Authorization: Bearer`` or
    the ``apiKey`` URL parameter.
    """
    apply_tls_settings(settings)
    owui = client or OpenWebUIClient(base_url=settings.base_url, token=settings.token)

    mcp = FastMCP(
        settings.name,
        instructions=settings.instructions,
        auth=(StaticTokenVerifier(settings.mcp_token) if settings.mcp_token else None),
    )

    def _list_models_sync() -> list[dict[str, Any]]:
        models = owui.list_models()
        return [
            {
                "id": m.id,
                "name": m.name,
                _TOOL_FIELDS: m.tool_ids or [],
            }
            for m in models
        ]

    @mcp.tool()
    async def list_models() -> list[dict[str, Any]]:
        """List the models available on the connected Open WebUI server.

        Returns one entry per model with its id, display name, and the ids of
        any tools attached to it. Use the returned ids as the ``model``
        argument of the ``ask`` tool.
        """
        return await asyncio.to_thread(_list_models_sync)

    def _ask_no_tools_sync(
        model: str,
        messages: list[dict[str, str]],
        temperature: float | None,
        timeout_s: int,
    ) -> dict[str, Any]:
        """Plain HTTP streaming path (no Socket.IO). Blocking; run in a thread."""
        result = owui.run_chat(
            model=model,
            messages=messages,
            tool_ids=[],
            temperature=temperature,
            timeout=timeout_s,
        )
        return {
            "answer": result.answer,
            "reasoning": result.reasoning,
            "tool_calls": result.tool_calls,
        }

    async def ask(
        prompt: str,
        model: str | None = None,
        system: str | None = None,
        temperature: float | None = None,
        use_tools: bool = True,
        history: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Ask an Open WebUI model a question, with tool support.

        IMPORTANT - this MCP server is stateless: every call starts a FRESH
        conversation on the remote model. The remote model does NOT remember
        previous calls. To carry context between questions, pass the relevant
        prior exchanges in ``history`` as ``[{"role": "user"|"assistant",
        "content": ...}]`` (your earlier question and its answer, in order),
        or include the needed context directly in ``prompt``.

        When ``model`` is omitted the configured default
        (``OPENWEBUI_DEFAULT_MODEL``) is used; if that is also unset the call
        fails. When ``OPENWEBUI_ENFORCE_DEFAULT_MODEL`` is on the configured
        default is always used and the caller's ``model`` is ignored. Tools
        attached to the model are enabled by default (``use_tools``) and run
        server-side through Open WebUI's Socket.IO tool loop, so answers may
        be produced with real tool calls.

        Args:
            prompt: The user message to send to the model.
            model: Open WebUI model id to ask, e.g. "sample-workspace-model-1",
                or from ``list_models``. Defaults to OPENWEBUI_DEFAULT_MODEL.
            system: Optional system prompt leading the conversation.
            temperature: Optional sampling temperature override.
            use_tools: Enable tools attached to the model (default True).
            history: Optional prior turns ``[{"role": "user"|"assistant",
                "content": ...}]`` sent before ``prompt`` (and after any
                ``system`` prompt) so the remote model keeps context from
                earlier questions.

        Returns:
            Dict with the answer text, optional reasoning, and any tool calls
            made: {"answer", "reasoning", "tool_calls"}. The "answer" may contain
            links, treat these as information sources, and cite as needed.
        """
        import time

        from openwebui_sdk import sockets

        if settings.enforce_default_model:
            # Enforce mode: the server-side default always wins over caller input.
            model = settings.default_model
        else:
            model = model or settings.default_model
        if not model:
            raise ValueError(
                "no model specified: pass a model to ask or set OPENWEBUI_DEFAULT_MODEL"
            )

        # SDK timeouts are in SECONDS (http.DEFAULT_TIMEOUT=60); settings in ms.
        timeout_s = max(1, settings.timeout_ms // 1000)
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": prompt})

        tool_ids = (
            await asyncio.to_thread(owui.resolve_tools, model) if use_tools else []
        )
        logger.info(
            "ask: model=%s tools=%s timeout=%ss", model, tool_ids or "(none)", timeout_s
        )

        started = time.monotonic()
        try:
            if tool_ids:
                # Await the SDK's async Socket.IO runner DIRECTLY on this event
                # loop - exactly how the CLI awaits it on its loop. Nesting the
                # SDK's asyncio.run inside asyncio.to_thread broke event
                # delivery (socket background tasks never pumped); running on
                # this loop keeps the aiohttp session + socketio client + the
                # completion event on one loop, so chat-events flow back.
                data = await asyncio.wait_for(
                    sockets.run_chat_with_tools(
                        base_url=settings.base_url,
                        token=settings.token,
                        model=model,
                        messages=messages,
                        tool_ids=tool_ids,
                        timeout=timeout_s,
                        on_status=lambda s: logger.info(
                            "ask[%s]: status: %s", model, s
                        ),
                        on_tool=lambda s: logger.info("ask[%s]: tool: %s", model, s),
                        on_reasoning=lambda s: logger.debug(
                            "ask[%s]: reasoning: %s", model, (s or "")[:200]
                        ),
                    ),
                    timeout=timeout_s + 60,
                )
                result = {
                    "answer": data.get("answer", ""),
                    "reasoning": data.get("reasoning"),
                    "tool_calls": data.get("tool_calls", []),
                }
            else:
                # No tools: plain HTTP streaming path (blocking) - offload.
                result = await asyncio.to_thread(
                    _ask_no_tools_sync,
                    model,
                    messages,
                    temperature,
                    timeout_s,
                )
        except TimeoutError:
            logger.error("ask: timed out after %ss for model=%s", timeout_s + 60, model)
            raise RuntimeError(
                f"Open WebUI did not complete within {timeout_s + 60}s"
            ) from None
        logger.info(
            "ask: done in %.1fs (tools=%d)",
            time.monotonic() - started,
            len(result.get("tool_calls") or []),
        )
        return result

    # Register ``ask`` with a composed description: the full docstring is
    # always shown, with only the first summary line replaceable via env.
    mcp.tool(description=ask_description(ask, settings), name="ask")(ask)

    return mcp
