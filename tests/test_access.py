"""The launched workbench is usable only by whoever holds the per-launch token."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_harness_demo import web
from agent_harness_demo.local import token_cookie_name
from agent_harness_demo.web import create_app

TOKEN = "test-token-0123456789abcdef"
LOCAL = "http://127.0.0.1:8787"


def test_api_and_page_require_the_token(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "store", access_token=TOKEN), base_url=LOCAL) as client:
        assert client.get("/api/config").status_code == 401
        assert client.get("/").status_code == 401
        assert client.get("/api/config", headers={"Authorization": "Bearer wrong-token-0123456789"}).status_code == 401
        assert client.get("/?token=wrong-token-0123456789", follow_redirects=False).status_code == 401
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/runs/unknown/stream") as stream:
                stream.receive_json()

        assert client.get("/api/config", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_browser_exchanges_the_token_for_a_strict_httponly_cookie(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "store", access_token=TOKEN), base_url=LOCAL) as client:
        entry = client.get(f"/?token={TOKEN}", follow_redirects=False)
        assert entry.status_code == 303 and entry.headers["location"] == "/"
        cookie = entry.headers["set-cookie"]
        assert cookie.startswith(f"{token_cookie_name(TOKEN)}=")
        assert "httponly" in cookie.lower() and "samesite=strict" in cookie.lower()

        assert client.get("/").status_code == 200
        assert client.get("/api/config").status_code == 200


def test_tokens_must_not_be_trivially_short(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        create_app(tmp_path / "store", access_token="short")


def test_every_response_refuses_framing(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "store", access_token=TOKEN), base_url=LOCAL) as client:
        for response in (client.get("/api/config"), client.get(f"/?token={TOKEN}"), client.get("/assets/app.js")):
            assert response.headers["x-frame-options"] == "DENY"
            assert response.headers["content-security-policy"] == "frame-ancestors 'none'"
            assert response.headers["referrer-policy"] == "no-referrer"


def test_shipped_trusted_hosts_reject_the_test_client_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()  # drop the test-only "testserver" host added in conftest
    assert "testserver" not in web.TRUSTED_HOSTS
    with TestClient(create_app(tmp_path / "store")) as client:
        assert client.get("/api/config").status_code == 400
    with TestClient(create_app(tmp_path / "store2"), base_url=LOCAL) as client:
        assert client.get("/api/config").status_code == 200
