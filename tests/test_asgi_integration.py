"""Real-ASGI integration tests: drive the actual FastMCP HTTP app (not a
mocked get_http_request()) to prove properties the unit tests can't:

* streamable-http: concurrent requests with different apiKey query params
  never cross-talk (the race the per-request-client design exists to
  prevent); the Authorization: Bearer header also works, over a real ASGI
  request; a request with neither credential fails cleanly instead of
  silently falling back to a shared identity.
* SSE: an Authorization: Bearer header set once on the client survives the
  split between the long-lived GET /sse connection and the follow-up
  POST /messages/?session_id=... that delivers each JSON-RPC message - proven
  here with the REAL mcp SDK client (mcp.client.sse.sse_client +
  mcp.ClientSession), not a hand-rolled reproduction. An apiKey query
  parameter on the /sse connection URL, by contrast, never reaches the tool
  call - the SDK's own SSE client resolves the server's relative
  message-endpoint URL via urljoin, which drops the connection URL's query
  string. Both are pinned here so a future change can't silently break
  either property without a test failing (the failure mode is invisible
  otherwise - no error, just the wrong/right key, or a fallback that
  shouldn't exist).

Uses httpx2 (fastmcp's vendored httpx client) and uvicorn - both locked
transitive dependencies of fastmcp, not new project dependencies.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import httpx2
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.sse import sse_client
from openwebui_sdk.models import Model

from openwebui_mcp import server as server_mod
from openwebui_mcp.config import Settings


class _RecordingClient:
    """Stand-in for OpenWebUIClient: records the token it was built with and
    echoes it back in the response, so a caller can tell which identity
    answered its specific request."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url
        self.token = token

    def list_models(self) -> list[Model]:
        return [Model(id=f"model-for-{self.token}", name="M", tool_ids=[])]

    def resolve_tools(self, model_id: str) -> list[str]:
        return ["t1"]


@pytest.fixture
def recording_app(monkeypatch: pytest.MonkeyPatch):
    """A real create_server() app with OpenWebUIClient swapped for the
    recorder, and no client= injected - the actual production code path."""
    monkeypatch.setattr(server_mod, "OpenWebUIClient", _RecordingClient)
    settings = Settings(base_url="http://fake-owui:8080")
    mcp = server_mod.create_server(settings)
    return mcp.http_app(transport="streamable-http")


async def _call_list_models(
    client: httpx2.AsyncClient,
    query_suffix: str = "",
    extra_headers: dict[str, str] | None = None,
) -> str:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        **(extra_headers or {}),
    }
    init_resp = await client.post(
        f"/mcp/{query_suffix}",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
        headers=headers,
    )
    assert init_resp.status_code == 200, init_resp.text
    session_id = init_resp.headers["mcp-session-id"]
    headers["mcp-session-id"] = session_id

    await client.post(
        f"/mcp/{query_suffix}",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=headers,
    )
    call_resp = await client.post(
        f"/mcp/{query_suffix}",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "list_models", "arguments": {}},
        },
        headers=headers,
    )
    assert call_resp.status_code == 200, call_resp.text
    return call_resp.text


@pytest.mark.anyio
async def test_streamable_http_concurrent_requests_no_token_crosstalk(
    recording_app: Any,
) -> None:
    """Two overlapping in-flight requests, different apiKeys, on separate
    client connections (separate MCP sessions). Each must see only its own
    token - the actual race resolve_request_token()'s per-call local client
    exists to prevent."""
    transport = httpx2.ASGITransport(app=recording_app)
    async with (
        recording_app.router.lifespan_context(recording_app),
        httpx2.AsyncClient(
            transport=transport, base_url="http://testserver", follow_redirects=True
        ) as client_a,
        httpx2.AsyncClient(
            transport=transport, base_url="http://testserver", follow_redirects=True
        ) as client_b,
    ):
        text_a, text_b = await asyncio.gather(
            _call_list_models(client_a, "?apiKey=sk-user-a"),
            _call_list_models(client_b, "?apiKey=sk-user-b"),
        )
    assert "model-for-sk-user-a" in text_a, text_a
    assert "model-for-sk-user-b" in text_b, text_b
    assert "sk-user-b" not in text_a
    assert "sk-user-a" not in text_b


async def _call_ask_with_tools(
    client: httpx2.AsyncClient, query_suffix: str
) -> str:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    init_resp = await client.post(
        f"/mcp/{query_suffix}",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
        headers=headers,
    )
    assert init_resp.status_code == 200, init_resp.text
    headers["mcp-session-id"] = init_resp.headers["mcp-session-id"]

    await client.post(
        f"/mcp/{query_suffix}",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=headers,
    )
    call_resp = await client.post(
        f"/mcp/{query_suffix}",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "ask",
                "arguments": {"model": "m1", "prompt": "hi", "use_tools": True},
            },
        },
        headers=headers,
    )
    assert call_resp.status_code == 200, call_resp.text
    return call_resp.text


@pytest.mark.anyio
async def test_streamable_http_ask_tools_path_concurrent_no_token_crosstalk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same race-safety proof as list_models, but for ask(use_tools=True) -
    a separate code path with its own resolved_token capture that passes a
    bare token= to sockets.run_chat_with_tools instead of going through an
    OpenWebUIClient instance. Never previously concurrency-tested."""

    async def _fake_run_chat_with_tools(**kwargs: Any) -> dict[str, Any]:
        return {
            "answer": f"answer-for-{kwargs['token']}",
            "reasoning": None,
            "tool_calls": [],
        }

    monkeypatch.setattr(
        "openwebui_sdk.sockets.run_chat_with_tools", _fake_run_chat_with_tools
    )
    monkeypatch.setattr(server_mod, "OpenWebUIClient", _RecordingClient)
    settings = Settings(base_url="http://fake-owui:8080")
    mcp = server_mod.create_server(settings)
    app = mcp.http_app(transport="streamable-http")

    transport = httpx2.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=transport, base_url="http://testserver", follow_redirects=True
        ) as client_a,
        httpx2.AsyncClient(
            transport=transport, base_url="http://testserver", follow_redirects=True
        ) as client_b,
    ):
        text_a, text_b = await asyncio.gather(
            _call_ask_with_tools(client_a, "?apiKey=sk-user-a"),
            _call_ask_with_tools(client_b, "?apiKey=sk-user-b"),
        )
    assert "answer-for-sk-user-a" in text_a, text_a
    assert "answer-for-sk-user-b" in text_b, text_b
    assert "sk-user-b" not in text_a
    assert "sk-user-a" not in text_b


@pytest.mark.anyio
async def test_streamable_http_bearer_header(recording_app: Any) -> None:
    """An Authorization: Bearer header works over a real ASGI request too,
    not just the apiKey query parameter."""
    transport = httpx2.ASGITransport(app=recording_app)
    async with (
        recording_app.router.lifespan_context(recording_app),
        httpx2.AsyncClient(
            transport=transport, base_url="http://testserver", follow_redirects=True
        ) as client,
    ):
        text = await _call_list_models(
            client, extra_headers={"Authorization": "Bearer sk-bearer-caller"}
        )
    assert "model-for-sk-bearer-caller" in text, text


@pytest.mark.anyio
async def test_streamable_http_raises_without_credential(recording_app: Any) -> None:
    """No apiKey, no Authorization header -> the tool call fails cleanly.
    There is no shared fallback identity to silently use."""
    transport = httpx2.ASGITransport(app=recording_app)
    async with (
        recording_app.router.lifespan_context(recording_app),
        httpx2.AsyncClient(
            transport=transport, base_url="http://testserver", follow_redirects=True
        ) as client,
    ):
        text = await _call_list_models(client)
    assert '"isError":true' in text, text
    assert "no Open WebUI identity supplied" in text, text


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _run_sse_server(settings: Settings):
    """Start a real uvicorn server for the SSE app; yields its base URL."""
    mcp = server_mod.create_server(settings)
    app = mcp.http_app(transport="sse")
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    uv_server = uvicorn.Server(config)
    server_task = asyncio.create_task(uv_server.serve())
    for _ in range(100):
        if uv_server.started:
            break
        await asyncio.sleep(0.05)
    else:
        raise RuntimeError("uvicorn did not start in time")
    return uv_server, server_task, f"http://127.0.0.1:{port}/sse"


@pytest.mark.anyio
async def test_sse_bearer_header_reaches_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bearer header survives SSE's GET -> POST split, driven by the REAL mcp
    SDK client (mcp.client.sse.sse_client + mcp.ClientSession) - not a
    hand-rolled reproduction. The SDK posts every message through the same
    client instance (and therefore the same configured headers) used for the
    GET connection - see mcp/client/sse.py's post_writer."""
    monkeypatch.setattr(server_mod, "OpenWebUIClient", _RecordingClient)
    settings = Settings(base_url="http://fake-owui:8080")
    uv_server, server_task, url = await _run_sse_server(settings)
    try:
        async with (
            sse_client(
                url, headers={"Authorization": "Bearer sk-bearer-caller"}
            ) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            result = await session.call_tool("list_models", {})
            text = result.model_dump_json()
    finally:
        uv_server.should_exit = True
        await server_task

    assert "model-for-sk-bearer-caller" in text, text


@pytest.mark.anyio
async def test_sse_apikey_query_param_does_not_reach_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the documented SSE limitation for the query-param mechanism
    specifically: ?apiKey= on the /sse connection URL cannot reach the tool
    call (the MCP SDK's own SSE client resolves the server's relative
    message-endpoint URL via urljoin, dropping the connection URL's query
    string - see resolve_request_token's docstring). No credential at all
    reaches the call, so it fails, same as the streamable-http case."""
    monkeypatch.setattr(server_mod, "OpenWebUIClient", _RecordingClient)
    settings = Settings(base_url="http://fake-owui:8080")
    uv_server, server_task, url = await _run_sse_server(settings)
    try:
        async with (
            sse_client(f"{url}?apiKey=sk-sse-caller") as (
                read_stream,
                write_stream,
            ),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            result = await session.call_tool("list_models", {})
            text = result.model_dump_json()
    finally:
        uv_server.should_exit = True
        await server_task

    assert result.is_error, text
    assert "no Open WebUI identity supplied" in text, text
    assert "sk-sse-caller" not in text
