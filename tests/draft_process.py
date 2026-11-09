"""Real child process with only the external model HTTP transport replaced."""

from dataclasses import dataclass
from contextlib import contextmanager
from collections.abc import Iterator
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import pytest

from test_recovery import RunningService, stop


@dataclass
class DraftService(RunningService):
    model_requests: Path


@contextmanager
def running_draft_service(store: Path, replies: list[httpx.Response], *, hold_publication: bool = False, package_root: Path | None = None, decision_protocol: str = "native", exact_assets: tuple[Path, Path] | None = None, qwen_4b: bool = False) -> Iterator[DraftService]:
    store.parent.mkdir(parents=True, exist_ok=True)
    identity = uuid4().hex
    helper = store.parent / f"draft-service-{identity}.py"
    fixture = store.parent / f"draft-replies-{identity}.json"
    fixture.write_text(json.dumps([reply.json() for reply in replies]), encoding="utf-8")
    helper.write_text('''import asyncio,json,sys
from pathlib import Path
import httpx,uvicorn
sys.path.insert(0,sys.argv[4])
sys.path.insert(0,sys.argv[6])
from agent_harness.web import create_app
from agent_harness.model import ModelSettings
from test_completion import CompletionPeer
scripted=CompletionPeer([httpx.Response(200,json=value) for value in json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))])
async def peer(request):
    if sys.argv[8] or sys.argv[10]=="4b":
        if request.url.path=="/api/version":
            return httpx.Response(200,json={"version":"0.40.10"})
        if request.url.path=="/api/tags":
            return httpx.Response(200,json={"models":[{"name":"qwen3.5:4b-q4_K_M" if sys.argv[10]=="4b" else "qwen3.5:9b-q4_K_M","digest":"d8b0f5e9760cd1682034f292d7ef72ec46f432149be0df7574bf2d6e92e38c04" if sys.argv[10]=="4b" else "56671c2ab9385f9cfcb404638e32cd62d88e3501d44822208363c010179a3c90"}]})
        if request.url.path=="/api/show":
            from test_session_versions import qwen_metadata
            return httpx.Response(200,json=qwen_metadata())
    if request.url.path=="/v1/chat/completions" and len(scripted.requests)>=len(scripted.replies):
        await asyncio.Event().wait()
    response = await scripted(request)
    await asyncio.to_thread(lambda: Path(sys.argv[2]+".requests.json").write_text(json.dumps(scripted.requests),encoding="utf-8"))
    return response
settings={"stream":False,"thinking":True}
model_name="contract-model"
if sys.argv[10]=="4b":
    model_name="qwen3.5:4b-q4_K_M"
    settings.update(thinking=False)
if sys.argv[8]:
    model_name="qwen3.5:9b-q4_K_M"
    settings.update(thinking=False,exact_tokenizer_library=Path(sys.argv[8]),exact_tokenizer_model=Path(sys.argv[9]))
if sys.argv[7]!="native":
    settings["decision_protocol"]=sys.argv[7]
uvicorn.run(create_app(Path(sys.argv[1]),model_settings=ModelSettings.local(model_name,**settings),model_transport=httpx.MockTransport(peer),hold_first_tool="artifact_write" if sys.argv[5]=="hold" else None),host="127.0.0.1",port=int(sys.argv[3]),access_log=False)
''', encoding="utf-8")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    log = store.parent / f"draft-service-{identity}.log"
    with log.open("wb") as output:
        process = subprocess.Popen([sys.executable, str(helper), str(store), str(fixture), str(port), str(Path(__file__).parent), "hold" if hold_publication else "none", str((package_root or Path(__file__).resolve().parents[1]) / "src"), decision_protocol, str(exact_assets[0]) if exact_assets else "", str(exact_assets[1]) if exact_assets else "", "4b" if qwen_4b else ""],
                                   stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10)
    try:
        deadline = time.monotonic() + 30  # generous: slow CI runners start Python slowly
        while time.monotonic() < deadline:
            assert process.poll() is None, log.read_text(encoding="utf-8", errors="replace")[-4000:]
            try:
                ready = client.get("/api/config").status_code == 200
            except httpx.TransportError:
                ready = False
            if ready:
                # Outside the try: an error raised by the test body must surface, not restart this loop.
                yield DraftService(client, process, log, Path(str(fixture)+".requests.json"))
                return
            time.sleep(.03)
        pytest.fail("Draft child service not ready: " + log.read_text(encoding="utf-8", errors="replace")[-4000:])
    finally:
        client.close()
        stop(process)


def wait_draft_tool(client: httpx.Client, identity: str, kind: str, tool: str, call_id: str | None = None) -> dict:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        events = client.get(f"/api/runs/{identity}/events").raise_for_status().json()["events"]
        match = next((event for event in events if event["kind"] == kind and event["data"]["tool"] == tool and (call_id is None or event["data"]["call_id"] == call_id)), None)
        if match is not None:
            return match
        time.sleep(.02)
    pytest.fail(f"Expected {kind} for {tool} was not saved: {client.get(f'/api/runs/{identity}').json()}")


def finish_draft_run(client: httpx.Client, identity: str) -> tuple[dict, list[dict]]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        run = client.get(f"/api/runs/{identity}").raise_for_status().json()
        if run["status"] in {"completed", "failed", "cancelled", "interrupted"}:
            return run, client.get(f"/api/runs/{identity}/events").raise_for_status().json()["events"]
        time.sleep(.02)
    pytest.fail("Expected a terminal draft Run.")
