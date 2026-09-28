"""FastMCP server exposing Open WebUI as MCP tools.

Two tools, per spec:

* ``ask`` - send a prompt to a model. The model is a required argument. Runs
  the Open WebUI tool-calling loop: any tools attached to the model are
  enabled automatically (``OpenWebUIClient.resolve_tools``) and results come
  back as a structured dict (answer, reasoning, tool_calls).
* ``list_models`` - list the models the connected Open WebUI user can see.

Authentication: the server talks to Open WebUI with a bearer token carried by
the SDK client. On streamable-http and SSE there is no pre-configured
fallback identity: every caller supplies their own via an ``Authorization:
Bearer`` header or an ``apiKey`` query parameter on the MCP URL
(``resolve_request_token``), so multiple users can share one HTTP endpoint
under their own Open WebUI identity. A request with neither credential
fails. stdio has no per-request channel at all (no URL, no headers), so it
falls back to ``OPENWEBUI_API_KEY`` (``settings.token``) - the only
transport where that setting is used; a stdio call fails only when that
fallback is also unset.

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
import ssl
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from openwebui_sdk import OpenWebUIClient

from openwebui_mcp.config import Settings

logger = logging.getLogger(__name__)

# SDK models attach tools under info.meta.toolIds; we surface that in
# list_models so callers can see which tools a model can invoke.
_TOOL_FIELDS = "tool_ids"


def resolve_request_token(settings: Settings) -> str:
    """Resolve the Open WebUI bearer token for the current request.

    On streamable-http and SSE, every caller supplies their own identity -
    ``settings.token`` is never consulted on those transports, so one
    caller's key can't leak into another's request. Checked in this order:

    1. ``Authorization: Bearer <token>`` header.
    2. ``apiKey`` query parameter on the MCP URL (e.g.
       ``https://host/mcp?apiKey=sk-...``).

    The ``Authorization`` header works on both streamable-http and SSE. The
    ``apiKey`` query parameter works on streamable-http only - see the SSE
    note below for why. The header wins when both are present (and a
    present-but-empty ``Bearer`` header does not count as supplied - it falls
    through to ``apiKey``, then to the "no credential" error below). An HTTP
    request with neither credential raises.

    stdio has no per-request channel at all (no URL, no headers), so it
    falls back to ``settings.token`` (``OPENWEBUI_API_KEY``) - the only
    transport where that setting is read. No configured token there raises
    too.

    SSE note: unlike the query parameter, the Bearer header survives SSE's
    split between the long-lived ``GET /sse`` connection and the follow-up
    ``POST /messages/?session_id=...`` that delivers each JSON-RPC message. A
    header is configured once on the client and resent on every request that
    client makes; a URL query string is not - it lives only on the one
    connection URL, and the SDK's SSE client resolves the server's relative
    message-endpoint path via ``urljoin``, which drops it. Confirmed live,
    both with a raw HTTP client and with the real ``mcp`` SDK's own SSE
    client (``mcp.client.sse.sse_client``), which posts through the very
    same client instance - and therefore the same configured headers - used
    for the GET connection.
    """
    try:
        request = get_http_request()
    except RuntimeError:
        if settings.token:
            return settings.token
        raise ValueError(
            "no Open WebUI identity available: stdio transport has no "
            "per-request channel to supply one, and no OPENWEBUI_API_KEY "
            "is configured as a fallback"
        ) from None

    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        bearer_token = auth_header[len("bearer ") :].strip()
        if bearer_token:
            return bearer_token

    api_key = request.query_params.get("apiKey")
    if api_key:
        return api_key

    raise ValueError(
        "no Open WebUI identity supplied: pass an Authorization: Bearer "
        "header or an apiKey query parameter"
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
    if not settings.ssl_verify and hasattr(
        ssl, "_create_default_https_context"
    ):
        ssl._create_default_https_context = ssl._create_unverified_context


def create_server(
    settings: Settings, *, client: OpenWebUIClient | None = None
) -> FastMCP:
    """Build a configured FastMCP instance exposing the Open WebUI tools.

    ``client`` is injectable for tests; when given, that exact instance is
    used for every call, regardless of any per-request identity. In
    production (``client`` omitted) each call resolves its own Open WebUI
    identity via ``resolve_request_token`` and talks to Open WebUI through a
    fresh, cheap ``OpenWebUIClient`` built from that token - so concurrent
    requests from different users on the same HTTP endpoint never share or
    race on token state.
    """
    apply_tls_settings(settings)

    mcp = FastMCP(settings.name, instructions=settings.instructions)

    def _list_models_sync(owui: OpenWebUIClient) -> list[dict[str, Any]]:
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
        owui = client or OpenWebUIClient(
            base_url=settings.base_url, token=resolve_request_token(settings)
        )
        return await asyncio.to_thread(_list_models_sync, owui)

    def _ask_no_tools_sync(
        owui: OpenWebUIClient,
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

        # When a client is injected (tests), ignore the live request entirely -
        # that instance's identity wins unconditionally, including for the
        # tools-enabled Socket.IO path below, which takes a bare token= rather
        # than the client object itself.
        resolved_token = (
            "" if client is not None else resolve_request_token(settings)
        )
        owui = client or OpenWebUIClient(
            base_url=settings.base_url, token=resolved_token
        )

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

        tool_ids: list[str] = (
            await asyncio.to_thread(owui.resolve_tools, model)
            if use_tools
            else []
        )
        logger.info(
            "ask: model=%s tools=%s timeout=%ss",
            model,
            tool_ids or "(none)",
            timeout_s,
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
                        token=resolved_token,
                        model=model,
                        messages=messages,
                        tool_ids=tool_ids,
                        timeout=timeout_s,
                        on_status=lambda s: logger.info(
                            "ask[%s]: status: %s", model, s
                        ),
                        on_tool=lambda s: logger.info(
                            "ask[%s]: tool: %s", model, s
                        ),
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
                    owui,
                    model,
                    messages,
                    temperature,
                    timeout_s,
                )
        except TimeoutError:
            logger.error(
                "ask: timed out after %ss for model=%s", timeout_s + 60, model
            )
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
