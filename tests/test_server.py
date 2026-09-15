"""Server tests: tool registration, tool behaviour with a fake client, auth."""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastmcp import FastMCP
from fastmcp.tools.function_tool import FunctionTool
from openwebui_sdk import OpenWebUIClient
from openwebui_sdk.chat import ChatResult
from openwebui_sdk.models import Model
from starlette.testclient import TestClient

from openwebui_mcp.config import Settings
from openwebui_mcp.server import StaticTokenVerifier, create_server

MS_SAMPLE = [
    Model(id="m1", name="Model One", tool_ids=["t1", "t2"]),
    Model(id="m2", name="Model Two", tool_ids=None),
]


class FakeClient:
    """Duck-typed stand-in for OpenWebUIClient; records calls, returns canned data."""

    def __init__(
        self,
        *,
        models: list[Model] | None = None,
        tool_ids: list[str] | None = None,
        result: ChatResult | None = None,
    ) -> None:
        self.models = models or []
        self.tool_ids = tool_ids or []
        self.result = result or ChatResult(
            answer="hi", reasoning="rt", tool_calls=[{"name": "x"}]
        )
        self.resolve_calls: list[str] = []
        self.chat_calls: list[dict[str, Any]] = []

    def list_models(self) -> list[Model]:
        return self.models

    def resolve_tools(self, model_id: str) -> list[str]:
        self.resolve_calls.append(model_id)
        return self.tool_ids

    def run_chat(self, **kwargs: Any) -> ChatResult:
        self.chat_calls.append(kwargs)
        return self.result


@pytest.fixture
def sockets_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace sockets.run_chat_with_tools with an async recorder."""
    calls: list[dict[str, Any]] = []

    async def _fake(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "answer": "hi",
            "reasoning": "rt",
            "tool_calls": [{"name": "x"}],
            "raw_content": "",
        }

    monkeypatch.setattr("openwebui_sdk.sockets.run_chat_with_tools", _fake)
    return calls


async def _call(mcp: FastMCP, name: str, **kwargs: Any) -> Any:
    """Await a registered tool's underlying function with kwargs."""
    tool = cast(FunctionTool, await mcp.get_tool(name))
    assert tool is not None, f"tool {name!r} not registered"
    return await tool.fn(**kwargs)


def _fake_settings(**kw: Any) -> Settings:
    base = {"base_url": "http://owui:8080", "token": "sk-x", **kw}
    return Settings(**base)


def _initialize_status(
    server: FastMCP, url: str, headers: dict[str, str] | None = None
) -> int:
    """Send an HTTP MCP initialize request and return its response status."""
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    request_headers = {
        "Accept": "application/json, text/event-stream",
        **(headers or {}),
    }
    app = server.http_app(path="/mcp", stateless_http=True)
    with TestClient(app) as client:
        return client.post(url, json=request, headers=request_headers).status_code


def test_server_instructions_field() -> None:
    """Server-wide instructions surface for Codex-style hosts to read."""
    server = create_server(
        _fake_settings(instructions="Always use ask() for genealogy questions"),
        client=cast(OpenWebUIClient, FakeClient()),
    )
    assert server.instructions == "Always use ask() for genealogy questions"
    # unset -> None
    plain = create_server(_fake_settings(), client=cast(OpenWebUIClient, FakeClient()))
    assert plain.instructions is None


@pytest.mark.anyio
async def test_server_exposes_both_tools() -> None:
    server = create_server(_fake_settings(), client=cast(OpenWebUIClient, FakeClient()))
    names = {t.name for t in await server.list_tools()}
    assert names == {"ask", "list_models"}


@pytest.mark.anyio
async def test_ask_params_optionality() -> None:
    """model is optional (falls back to OPENWEBUI_DEFAULT_MODEL); prompt required."""
    server = create_server(_fake_settings(), client=cast(OpenWebUIClient, FakeClient()))
    tool = await server.get_tool("ask")
    assert tool is not None
    assert tool.parameters is not None
    assert "prompt" in tool.parameters.get("required", [])
    assert "model" not in tool.parameters.get("required", [])
    # history is an optional array param, exposed for context carry-over
    assert "history" not in tool.parameters.get("required", [])
    history_schema = tool.parameters["properties"]["history"]
    schema_types = [history_schema.get("type")] + [
        s.get("type") for s in history_schema.get("anyOf", [])
    ]
    assert "array" in schema_types


@pytest.mark.anyio
async def test_ask_description_env_override() -> None:
    """ENV replaces only the first summary line; the rest always stays."""
    server = create_server(
        _fake_settings(ask_description="Always answer in German"),
        client=cast(OpenWebUIClient, FakeClient()),
    )
    tool = await server.get_tool("ask")
    assert tool is not None
    assert tool.description is not None
    # env text is the new first line
    assert tool.description.startswith("Always answer in German")
    # the default summary line is replaced (not just prepended)
    assert "a question, with tool support" not in tool.description.splitlines()[0]
    # the stateless/history guidance survives
    assert "IMPORTANT - this MCP server is stateless" in tool.description
    assert "history" in tool.description


@pytest.mark.anyio
async def test_ask_description_falls_back_to_docstring() -> None:
    """No ENV description -> the built-in docstring is used verbatim."""
    server = create_server(_fake_settings(), client=cast(OpenWebUIClient, FakeClient()))
    tool = await server.get_tool("ask")
    assert tool is not None
    assert tool.description is not None
    assert tool.description.startswith(
        "Ask an Open WebUI model a question, with tool support."
    )
    assert "IMPORTANT - this MCP server is stateless" in tool.description
    assert "history" in tool.description


@pytest.mark.anyio
async def test_ask_with_default_model(
    sockets_calls: list[dict[str, Any]],
) -> None:
    """Omitted model falls back to settings.default_model."""
    fake = FakeClient(tool_ids=["t1"])
    server = create_server(
        _fake_settings(default_model="m-default"),
        client=cast(OpenWebUIClient, fake),
    )
    out = await _call(server, "ask", prompt="hi", use_tools=True)
    assert fake.resolve_calls == ["m-default"]
    assert sockets_calls[0]["model"] == "m-default"
    assert out == {"answer": "hi", "reasoning": "rt", "tool_calls": [{"name": "x"}]}


@pytest.mark.anyio
async def test_ask_without_model_or_default_raises() -> None:
    """No model and no configured default is an explicit error, not a hang."""
    fake = FakeClient()
    server = create_server(_fake_settings(), client=cast(OpenWebUIClient, fake))
    with pytest.raises(ValueError, match="OPENWEBUI_DEFAULT_MODEL"):
        await _call(server, "ask", prompt="hi", use_tools=False)
    assert fake.chat_calls == []


@pytest.mark.anyio
async def test_ask_with_default_model_no_tools() -> None:
    """HTTP path honours the default model too."""
    fake = FakeClient()
    server = create_server(
        _fake_settings(default_model="m-default"),
        client=cast(OpenWebUIClient, fake),
    )
    await _call(server, "ask", prompt="hi", use_tools=False)
    assert fake.chat_calls[0]["model"] == "m-default"


@pytest.mark.anyio
async def test_ask_enforce_default_model_overrides_input(
    sockets_calls: list[dict[str, Any]],
) -> None:
    """With enforce on, the caller's model is ignored and default wins."""
    fake = FakeClient(tool_ids=["t1"])
    server = create_server(
        _fake_settings(default_model="m-default", enforce_default_model=True),
        client=cast(OpenWebUIClient, fake),
    )
    # caller passes a different model -> must be ignored
    out = await _call(server, "ask", prompt="hi", model="caller-picked", use_tools=True)
    assert fake.resolve_calls == ["m-default"]
    assert sockets_calls[0]["model"] == "m-default"
    assert out == {"answer": "hi", "reasoning": "rt", "tool_calls": [{"name": "x"}]}


@pytest.mark.anyio
async def test_ask_enforce_without_default_raises() -> None:
    """Enforce on but no default_model -> clear error, defaults don't get stuck."""
    fake = FakeClient(tool_ids=["t1"])
    server = create_server(
        _fake_settings(enforce_default_model=True),
        client=cast(OpenWebUIClient, fake),
    )
    with pytest.raises(ValueError, match="OPENWEBUI_DEFAULT_MODEL"):
        await _call(server, "ask", prompt="hi", model="caller-picked", use_tools=True)
    assert fake.resolve_calls == []


@pytest.mark.anyio
async def test_ask_history_prepended() -> None:
    """history turns come before prompt (after system) so remote model has context."""
    fake = FakeClient()
    server = create_server(_fake_settings(), client=cast(OpenWebUIClient, fake))
    await _call(
        server,
        "ask",
        prompt="Now question B",
        model="m1",
        system="Be terse",
        history=[
            {"role": "user", "content": "Question A"},
            {"role": "assistant", "content": "Answer A"},
        ],
        use_tools=False,
    )
    assert fake.chat_calls[0]["messages"] == [
        {"role": "system", "content": "Be terse"},
        {"role": "user", "content": "Question A"},
        {"role": "assistant", "content": "Answer A"},
        {"role": "user", "content": "Now question B"},
    ]


@pytest.mark.anyio
async def test_ask_history_passes_through_tools_path(
    sockets_calls: list[dict[str, Any]],
) -> None:
    """history flows to the socket runner too, not just the HTTP path."""
    fake = FakeClient(tool_ids=["t1"])
    server = create_server(_fake_settings(), client=cast(OpenWebUIClient, fake))
    await _call(
        server,
        "ask",
        prompt="B",
        model="m1",
        history=[{"role": "user", "content": "A"}],
        use_tools=True,
    )
    assert sockets_calls[0]["messages"] == [
        {"role": "user", "content": "A"},
        {"role": "user", "content": "B"},
    ]


@pytest.mark.anyio
async def test_ask_with_tools_calls_sockets_runner(
    sockets_calls: list[dict[str, Any]],
) -> None:
    """Tools path must await sockets.run_chat_with_tools directly, not owui.run_chat."""
    fake = FakeClient(tool_ids=["t1"])
    server = create_server(_fake_settings(), client=cast(OpenWebUIClient, fake))
    out = await _call(
        server, "ask", model="m1", prompt="What time is it?", use_tools=True
    )
    # resolve_tools was called
    assert fake.resolve_calls == ["m1"]
    # the sync run_chat (which nests asyncio.run) was NOT called
    assert fake.chat_calls == []
    # the async socket runner was called with the right args
    call = sockets_calls[0]
    assert call["model"] == "m1"
    assert call["tool_ids"] == ["t1"]
    assert call["messages"] == [{"role": "user", "content": "What time is it?"}]
    assert out == {"answer": "hi", "reasoning": "rt", "tool_calls": [{"name": "x"}]}


@pytest.mark.anyio
async def test_ask_no_tools_uses_http_path() -> None:
    """No tools -> plain HTTP streaming via owui.run_chat (offloaded to a thread)."""
    fake = FakeClient()
    server = create_server(_fake_settings(), client=cast(OpenWebUIClient, fake))
    await _call(
        server, "ask", model="m1", prompt="hi", system="Be terse", use_tools=False
    )
    call = fake.chat_calls[0]
    assert call["messages"] == [
        {"role": "system", "content": "Be terse"},
        {"role": "user", "content": "hi"},
    ]
    assert call["tool_ids"] == []


@pytest.mark.anyio
async def test_ask_passes_temperature() -> None:
    fake = FakeClient()
    server = create_server(_fake_settings(), client=cast(OpenWebUIClient, fake))
    await _call(
        server, "ask", model="m1", prompt="hi", temperature=0.5, use_tools=False
    )
    assert fake.chat_calls[0]["temperature"] == 0.5


@pytest.mark.anyio
async def test_list_models_shape() -> None:
    server = create_server(
        _fake_settings(), client=cast(OpenWebUIClient, FakeClient(models=MS_SAMPLE))
    )
    out = await _call(
        server,
        "list_models",
    )
    assert out == [
        {"id": "m1", "name": "Model One", "tool_ids": ["t1", "t2"]},
        {"id": "m2", "name": "Model Two", "tool_ids": []},
    ]


@pytest.mark.anyio
async def test_ask_timeout_passed_in_seconds() -> None:
    """SDK timeouts are seconds, not ms (regression for the hang)."""
    fake = FakeClient()
    server = create_server(
        _fake_settings(timeout_ms=120_000), client=cast(OpenWebUIClient, fake)
    )
    await _call(server, "ask", model="m1", prompt="hi", use_tools=False)
    assert fake.chat_calls[0]["timeout"] == 120  # seconds, not 120000


@pytest.mark.anyio
async def test_ask_with_tools_timeout_passed_in_seconds(
    sockets_calls: list[dict[str, Any]],
) -> None:
    fake = FakeClient(tool_ids=["t1"])
    server = create_server(
        _fake_settings(timeout_ms=120_000), client=cast(OpenWebUIClient, fake)
    )
    await _call(server, "ask", model="m1", prompt="hi", use_tools=True)
    assert sockets_calls[0]["timeout"] == 120


def test_mcp_auth_wired_when_token_set() -> None:
    server = create_server(
        _fake_settings(mcp_token="s3cret"), client=cast(OpenWebUIClient, FakeClient())
    )
    assert isinstance(server.auth, StaticTokenVerifier)


def test_mcp_auth_absent_without_token() -> None:
    server = create_server(_fake_settings(), client=cast(OpenWebUIClient, FakeClient()))
    assert server.auth is None


@pytest.mark.anyio
async def test_static_verifier_accepts_and_rejects() -> None:
    verifier = StaticTokenVerifier("right-token")
    ok = await verifier.verify_token("right-token")
    assert ok is not None
    assert await verifier.verify_token("wrong-token") is None
    assert await verifier.verify_token("") is None


def test_mcp_auth_accepts_api_key_query_parameter() -> None:
    """HTTP clients without header support can authenticate through apiKey."""
    server = create_server(
        _fake_settings(mcp_token="s3cret"), client=cast(OpenWebUIClient, FakeClient())
    )
    assert _initialize_status(server, "/mcp?apiKey=s3cret") == 200


@pytest.mark.parametrize(
    ("url", "headers"),
    [
        ("/mcp", {"Authorization": "Bearer s3cret"}),
        ("/mcp?apiKey=wrong", {"Authorization": "Bearer s3cret"}),
    ],
)
def test_mcp_auth_accepts_bearer_header(
    url: str, headers: dict[str, str]
) -> None:
    """Bearer auth stays valid and takes precedence over the query parameter."""
    server = create_server(
        _fake_settings(mcp_token="s3cret"), client=cast(OpenWebUIClient, FakeClient())
    )
    assert _initialize_status(server, url, headers) == 200


@pytest.mark.parametrize(
    ("url", "headers"),
    [
        ("/mcp?apiKey=wrong", {}),
        ("/mcp?apiKey=s3cret", {"Authorization": "Bearer wrong"}),
    ],
)
def test_mcp_auth_rejects_invalid_credentials(
    url: str, headers: dict[str, str]
) -> None:
    """Invalid query tokens fail, and a query token cannot override Bearer auth."""
    server = create_server(
        _fake_settings(mcp_token="s3cret"), client=cast(OpenWebUIClient, FakeClient())
    )
    assert _initialize_status(server, url, headers) == 401


@pytest.mark.anyio
async def test_call_tool_end_to_end(sockets_calls: list[dict[str, Any]]) -> None:
    """Drive both tools through FastMCP's call_tool pipeline (protocol level)."""
    fake = FakeClient(models=MS_SAMPLE, tool_ids=["t1"])
    server = create_server(_fake_settings(), client=cast(OpenWebUIClient, fake))

    res = await server.call_tool("list_models", {})
    assert not res.is_error
    assert res.structured_content is not None
    assert [r["id"] for r in res.structured_content["result"]] == ["m1", "m2"]

    res_b = await server.call_tool(
        "ask",
        {"model": "m1", "prompt": "what is 6*7?", "use_tools": True},
    )
    assert not res_b.is_error
    assert fake.resolve_calls == ["m1"]
    assert sockets_calls[0]["tool_ids"] == ["t1"]
    # sockets fake returns answer "hi"
    assert res_b.structured_content is not None
    assert res_b.structured_content["answer"] == "hi"
