"""Opt-in model preload: the workbench asks Ollama to load the model at startup, so a Run's time limit is not spent on it.

browser-01 A ran out of its 180 s limit after the cold load alone took 72 s of it.
"""
import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness.model import ModelSettings
from agent_harness.web import create_app
from test_local_model import LocalPeer


class PreloadPeer(LocalPeer):
    def __init__(self, status: int = 200, delay: float = 0) -> None:
        super().__init__()
        self.loads: list[dict] = []
        self.status, self.delay = status, delay

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/generate":
            self.loads.append(json.loads(request.content))
            await asyncio.sleep(self.delay)
            return httpx.Response(self.status, json={"model": "contract-model", "done": True, "done_reason": "load"})
        return await super().__call__(request)


def _settled(client: TestClient) -> dict:
    deadline = time.monotonic() + 10
    while True:
        preload = client.get("/api/config").json()["preload"]
        if preload["status"] != "loading" or time.monotonic() > deadline:
            return preload
        time.sleep(0.05)


def test_the_model_is_loaded_in_the_background_when_asked(tmp_path: Path) -> None:
    peer = PreloadPeer(delay=0.3)
    settings = ModelSettings.local("contract-model", stream=False)
    with TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer),
                               preload_model=True)) as client:
        first = client.get("/api/config").json()["preload"]  # startup did not wait for the load
        assert first["enabled"] and first["status"] == "loading"
        preload = _settled(client)
    assert preload["status"] == "ready" and preload["keep_alive"] == "30m" and preload["seconds"] >= 0.3
    assert peer.loads == [{"model": "contract-model", "keep_alive": "30m"}]  # no prompt: Ollama only loads


def test_a_failed_preload_is_reported_and_changes_nothing_else(tmp_path: Path) -> None:
    peer = PreloadPeer(status=500)
    settings = ModelSettings.local("contract-model", stream=False)
    with TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer),
                               preload_model=True)) as client:
        preload = _settled(client)
        assert client.get("/api/config").status_code == 200
    assert preload["status"] == "failed" and preload["error"] == "HTTPStatusError"


def test_nothing_is_loaded_by_default(tmp_path: Path) -> None:
    peer = PreloadPeer()
    settings = ModelSettings.local("contract-model", stream=False)
    with TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer))) as client:
        assert client.get("/api/config").json()["preload"] == {"enabled": False}
    assert peer.loads == []


def test_only_a_local_model_can_be_preloaded(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="local Ollama"):
        create_app(tmp_path / "store", preload_model=True)
