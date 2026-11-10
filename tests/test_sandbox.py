"""python_run: an opt-in tool that runs model-written Python in a locked-down Docker container.

Most tests replace Docker with a recording runner, so they check the exact command (no network,
read-only data, limits) and what is staged, without Docker. One test runs a real container when
Docker and the image are available (CI pulls it); elsewhere it is skipped with the reason.
"""
import asyncio
import hashlib
import os
from pathlib import Path
import shutil
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness.contracts import RunFailure
from agent_harness.model import ModelSettings
from agent_harness.sandbox import MAX_STDOUT_BYTES, PythonSandbox, is_run_evidence
from agent_harness.web import create_app
from test_completion import CompletionPeer
from test_workbench import finish, tool_reply

IMAGE = os.environ.get("SANDBOX_TEST_IMAGE", "python:3.12-slim")


class Recorder:
    """Stands in for Docker: records each command and what the container would see at /data."""

    def __init__(self, *results: tuple[int | None, bytes, bytes]) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    async def __call__(self, argv: list[str], stdin: bytes, timeout: float, kill: list[str]) -> tuple[int | None, bytes, bytes]:
        if argv[1:3] == ["image", "inspect"]:
            return 0, b"sha256:abc\n", b""
        mount = next(part for part in argv if part.startswith("type=bind,"))
        data = Path(mount.split("source=", 1)[1].split(",target=", 1)[0])
        seen = {path.relative_to(data).as_posix(): path.read_bytes() for path in data.rglob("*") if path.is_file()}
        self.calls.append({"argv": argv, "code": stdin.decode("utf-8"), "timeout": timeout, "kill": kill, "seen": seen})
        return self.results.pop(0) if self.results else (0, b"ok\n", b"")


def _workspace(tmp_path: Path) -> tuple[Path, list[dict]]:
    root = tmp_path / "workspace"
    (root / "uploads").mkdir(parents=True)
    (root / "sales.csv").write_bytes(b"region,amount\nnorth,10\nsouth,32\n")
    (root / "uploads" / "notes.md").write_bytes(b"Target: 40\n")
    (root / "private.md").write_bytes(b"not selected\n")
    sources = [{"path": path, "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest()}
               for path in ("sales.csv", "uploads/notes.md")]
    return root, sources


def run(sandbox: PythonSandbox, code: str, root: Path, sources: list[dict]) -> dict:
    return asyncio.run(sandbox.run(code, root, sources))


def test_the_container_is_locked_down() -> None:
    argv = PythonSandbox(IMAGE).command("agent-harness-py-test", Path("/tmp/data"))
    joined = " ".join(argv)
    for flag in ("--rm", "--pull never", "--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges",
                 "--user 65534:65534", "--memory 512m", "--memory-swap 512m", "--pids-limit 128", "--cpus 1"):
        assert flag in joined, flag
    assert f"type=bind,source={Path('/tmp/data')},target=/data,readonly" in argv
    assert argv[-4:] == [IMAGE, "python", "-I", "-"]
    assert "--privileged" not in argv and "-v" not in argv


def test_a_successful_run_is_evidence_and_sees_only_the_selected_sources(tmp_path: Path) -> None:
    root, sources = _workspace(tmp_path)
    recorder = Recorder((0, b"total 42\r\n", b""))
    result = run(PythonSandbox(IMAGE, runner=recorder), "print('total', 42)", root, sources)

    call = recorder.calls[0]
    assert call["seen"] == {"sales.csv": b"region,amount\nnorth,10\nsouth,32\n", "uploads/notes.md": b"Target: 40\n"}
    assert call["code"] == "print('total', 42)" and call["kill"][1:3] == ["kill", call["argv"][call["argv"].index("--name") + 1]]
    assert result["evidence_type"] == "python_run" and result["path"] == "python_run" and result["text"] == "total 42\n"
    assert result["data_files"] == sources and result["exit_code"] == 0 and result["truncated"] is False
    assert is_run_evidence(result)
    mount = call["argv"][call["argv"].index("--mount") + 1]
    assert not Path(mount.split("source=", 1)[1].split(",target=", 1)[0]).exists()  # the staged copy is removed


@pytest.mark.parametrize(("result", "code"), [
    ((1, b"", b"Traceback ...\nZeroDivisionError: division by zero\n"), "python_failed"),
    ((137, b"", b""), "python_failed"),
    ((None, b"", b""), "python_timeout"),
    ((125, b"", b"docker: Error response from daemon: No such image"), "sandbox_unavailable"),
])
def test_failures_go_back_with_a_reason(tmp_path: Path, result: tuple, code: str) -> None:
    root, sources = _workspace(tmp_path)
    with pytest.raises(RunFailure) as caught:
        run(PythonSandbox(IMAGE, runner=Recorder(result)), "1/0", root, sources)
    assert caught.value.code == code
    if code == "python_failed" and result[0] == 1:
        assert "ZeroDivisionError" in caught.value.message and caught.value.correctable_fields == ("code",)


def test_long_output_is_cut_and_labelled(tmp_path: Path) -> None:
    root, sources = _workspace(tmp_path)
    result = run(PythonSandbox(IMAGE, runner=Recorder((0, b"x" * (MAX_STDOUT_BYTES + 1), b""))), "print('x' * 10**6)", root, sources)
    assert result["truncated"] is True and result["text"].startswith("[truncated:")


def test_a_changed_source_is_not_staged(tmp_path: Path) -> None:
    root, sources = _workspace(tmp_path)
    (root / "sales.csv").write_bytes(b"region,amount\nnorth,999\n")
    recorder = Recorder()
    with pytest.raises(RunFailure) as caught:
        run(PythonSandbox(IMAGE, runner=recorder), "print(1)", root, sources)
    assert caught.value.code == "source_changed" and recorder.calls == []


# The workbench: off unless the app enables it and the Run asks.

def _client(tmp_path: Path, replies: list[httpx.Response], **options: object) -> tuple[TestClient, CompletionPeer]:
    peer = CompletionPeer(replies)
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    return TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer),
                                 **options)), peer  # type: ignore[arg-type]


def _start(client: TestClient, tmp_path: Path, **body: object) -> httpx.Response:
    root, _ = _workspace(tmp_path)
    workspace = client.post("/api/workspaces", json={"path": str(root)}).json()
    session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
    return client.post(f"/api/sessions/{session['id']}/runs",
                       json={"task": "What is the median amount?", "sources": ["sales.csv"], **body})


def test_a_run_with_the_sandbox_cites_its_output(tmp_path: Path) -> None:
    recorder = Recorder((0, b"median 21\n", b""))
    app, peer = _client(tmp_path, [
        tool_reply("plan_update", {"steps": ["Compute the median in Python", "Answer"]}, "plan"),
        tool_reply("python_run", {"code": "import csv, statistics\nrows = list(csv.DictReader(open('sales.csv')))\n"
                                          "print('median', statistics.median(int(r['amount']) for r in rows))"}, "run"),
        tool_reply("task_finish", {"summary": "The median amount is 21 [python_run E1]."}, "finish"),
    ], python_image=IMAGE, sandbox_runner=recorder)
    with app as client:
        assert client.get("/api/config").json()["python"]["image"] == IMAGE
        response = _start(client, tmp_path, python_sandbox=True, completion_workflow="answer")
        assert response.status_code == 202, response.text
        final, events = finish(client, response.json())

    assert final["status"] == "completed", final["error"]
    assert final["policy"]["python_sandbox"] is True
    assert recorder.calls[0]["seen"] == {"sales.csv": b"region,amount\nnorth,10\nsouth,32\n"}
    verified = next(event["data"] for event in events if event["kind"] == "answer.verified")
    assert verified["cited"][0]["path"] == "python_run"
    assert any("python_run" in [tool["function"]["name"] for tool in request.get("tools") or []] for request in peer.requests)


def test_the_sandbox_is_off_unless_the_app_and_the_run_both_ask(tmp_path: Path) -> None:
    app, _ = _client(tmp_path / "off", [])
    with app as client:
        assert client.get("/api/config").json()["python"] == {"enabled": False}
        refused = _start(client, tmp_path / "off", python_sandbox=True)
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == "python_sandbox_unavailable"

    recorder = Recorder()
    app, peer = _client(tmp_path / "on", [
        tool_reply("plan_update", {"steps": ["Try Python"]}, "plan"),
        tool_reply("python_run", {"code": "print(1)"}, "run"),
    ], python_image=IMAGE, sandbox_runner=recorder)
    with app as client:
        final, events = finish(client, _start(client, tmp_path / "on", limits={"max_corrections": 0}).json())
    assert recorder.calls == []
    assert all("python_run" not in [tool["function"]["name"] for tool in request.get("tools") or []] for request in peer.requests)


def test_command_line_flag_checks_the_image_name() -> None:
    from agent_harness.__main__ import build_python_image, parser

    assert build_python_image(parser().parse_args(["--python-sandbox", "python:3.12-slim"])) == "python:3.12-slim"
    assert build_python_image(parser().parse_args([])) is None
    for bad in ("--rm", "python:3.12 slim", ""):
        with pytest.raises(ValueError):
            build_python_image(parser().parse_args([f"--python-sandbox={bad}"]))


def _docker_ready() -> str | None:
    docker = shutil.which("docker")
    if docker is None:
        return "docker is not installed"
    try:
        found = subprocess.run([docker, "image", "inspect", IMAGE], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"docker is not usable: {error}"
    return None if found.returncode == 0 else f"docker or the image {IMAGE} is not available (docker pull {IMAGE})"


def test_a_real_container_computes_but_cannot_reach_the_network_or_write_data(tmp_path: Path) -> None:
    reason = _docker_ready()
    if reason is not None:
        if os.environ.get("REQUIRE_DOCKER_SANDBOX"):
            pytest.fail(reason)  # CI must run this test, not skip it
        pytest.skip(reason)
    root, sources = _workspace(tmp_path)
    sandbox = PythonSandbox(IMAGE, timeout=20)
    code = """
import csv, socket, os
print("sum", sum(int(row["amount"]) for row in csv.DictReader(open("sales.csv"))))
print("files", sorted(os.path.relpath(os.path.join(d, f), "/data") for d, _, fs in os.walk("/data") for f in fs))
try:
    socket.create_connection(("1.1.1.1", 53), timeout=3)
    print("network open")
except OSError:
    print("network blocked")
try:
    open("sales.csv", "a").write("x")
    print("data writable")
except OSError:
    print("data read-only")
print("user", os.getuid())
"""
    result = run(sandbox, code, root, sources)
    assert result["text"].splitlines() == ["sum 42", "files ['sales.csv', 'uploads/notes.md']", "network blocked",
                                           "data read-only", "user 65534"]
    with pytest.raises(RunFailure) as caught:
        run(PythonSandbox(IMAGE, timeout=3), "while True:\n    pass\n", root, sources)
    assert caught.value.code == "python_timeout"
