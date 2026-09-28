"""TLS trust handling against a local self-signed HTTPS Open WebUI mock.

Reproduces the reported failure: direct https to a server whose CA is not in the
process trust store, then verifies ``apply_tls_settings`` fixes it via either
the CA bundle (secure) or verification bypass.
"""

from __future__ import annotations

import http.server
import json
import os
import ssl
import subprocess
import threading
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest
from openwebui_sdk import OpenWebUIClient

from openwebui_mcp.config import Settings
from openwebui_mcp.server import apply_tls_settings


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(
            {"data": [{"id": "m1", "name": "Model One", "info": {"meta": {}}}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


def _make_cert(tmp: Any) -> tuple[str, str, str]:
    """Generate a self-signed cert; return (cert, key, base_url)."""
    key = tmp / "key.pem"
    cert = tmp / "cert.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "2",
            "-nodes",
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    return str(cert), str(key), str(cert)


@pytest.fixture
def tls_server(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Start a self-signed https server; return its base_url."""
    cert, key, _ca = _make_cert(tmp_path)
    srv: Any = __import__("socketserver").TCPServer(("127.0.0.1", 0), _Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"https://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    urllib.request.urlcleanup()  # drop cached opener (proxy/TLS context)


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force direct, untrusted-verified client behavior: no proxy, no mitm CA."""
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    urllib.request.urlcleanup()


def test_direct_https_to_self_signed_fails_without_fix(
    tls_server: str, isolated_env: None
) -> None:
    with pytest.raises(
        Exception, match="CERTIFICATE_VERIFY_FAILED|self-signed|unable to get local"
    ):
        OpenWebUIClient(base_url=tls_server, token="sk-x").list_models()


def test_verify_false_allows_self_signed(tls_server: str, isolated_env: None) -> None:
    apply_tls_settings(Settings(base_url=tls_server, ssl_verify=False))
    urllib.request.urlcleanup()
    models = OpenWebUIClient(base_url=tls_server, token="sk-x").list_models()
    assert models and models[0].id == "m1"


def test_ca_bundle_allows_self_signed(
    tls_server: str, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, isolated_env: None
) -> None:
    _cert, _key, ca = _make_cert(tmp_path)
    apply_tls_settings(Settings(base_url=tls_server, ssl_ca_bundle=ca))
    assert os.environ.get("SSL_CERT_FILE") == ca
    urllib.request.urlcleanup()
    models = OpenWebUIClient(base_url=tls_server, token="sk-x").list_models()
    assert models and models[0].id == "m1"


def test_no_tls_config_is_noop(tls_server: str, isolated_env: None) -> None:
    before = ssl._create_default_https_context
    apply_tls_settings(Settings(base_url=tls_server))
    assert ssl._create_default_https_context is before
