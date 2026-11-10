"""python_run: run model-written Python in a locked-down Docker container. Off unless the user enables it.

The workbench offers the tool only when it starts with --python-sandbox IMAGE and the Run asks for
it. Each call starts a fresh container from that image, already pulled (`--pull never`), with:

- no network (`--network none`);
- the Run's selected sources only, copied after their hashes are checked and mounted read-only at
  /data under their workspace paths; nothing else of the computer is visible;
- a read-only root file system, a small /tmp, no Linux capabilities, no privilege gain, an
  unprivileged user, and limits on memory, CPU, processes and time (the container is killed when
  the time is up).

The code goes in on stdin. A run that exits 0 becomes evidence: its stdout, the code's SHA-256 and
the hashes of the files it could read, cited as [python_run EID]. A failure goes back to the model
with the end of stderr. Output is capped and a cut is labelled.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any
from uuid import uuid4

from .contracts import RunFailure
from .sources import source_bytes

Record = dict[str, Any]
# (argv, stdin, timeout seconds, argv that kills the container) -> (exit code or None on timeout, stdout, stderr)
Runner = Callable[[list[str], bytes, float, list[str]], Awaitable[tuple[int | None, bytes, bytes]]]
TOOL_NAME = "python_run"
MAX_CODE_CHARS = 20_000
MAX_STDOUT_BYTES = 65_536
MAX_STDERR_BYTES = 4_096
TIMEOUT_SECONDS = 30.0
MEMORY = "512m"
CPUS = "1"
PIDS = "128"
PARAMETERS: Record = {"type": "object", "properties": {"code": {
    "type": "string", "minLength": 1, "maxLength": MAX_CODE_CHARS, "pattern": r"\S"}},
    "required": ["code"], "additionalProperties": False}
SANDBOX_ERRORS = frozenset({"python_failed", "python_timeout"})
EVIDENCE_KEYS = frozenset({"evidence_type", "path", "image", "code", "code_sha256", "data_files", "exit_code",
                           "duration_ms", "truncated", "text", "sha256", "stderr", "line_start", "line_end", "evidence_id"})


async def _read_capped(stream: asyncio.StreamReader | None, limit: int) -> tuple[bytes, bool]:
    """Read a stream to its end, keeping at most `limit` bytes so a flood of output cannot fill memory."""
    kept = bytearray()
    cut = False
    if stream is None:
        return b"", False
    while chunk := await stream.read(65_536):
        room = limit - len(kept)
        if len(chunk) > room:
            cut = True
        kept += chunk[:max(room, 0)]
    return bytes(kept), cut


async def docker_runner(argv: list[str], stdin: bytes, timeout: float, kill: list[str]) -> tuple[int | None, bytes, bytes]:
    try:
        process = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.PIPE)
    except OSError as error:
        raise RunFailure("sandbox_unavailable", f"Docker could not be started: {type(error).__name__}.") from error
    assert process.stdin is not None
    process.stdin.write(stdin)
    await process.stdin.drain()
    process.stdin.close()
    reading = asyncio.gather(_read_capped(process.stdout, MAX_STDOUT_BYTES + 1), _read_capped(process.stderr, MAX_STDERR_BYTES * 4))
    try:
        (stdout, _), (stderr, _) = await asyncio.wait_for(asyncio.shield(reading), timeout)
        return await process.wait(), stdout, stderr
    except TimeoutError:
        killer = await asyncio.create_subprocess_exec(*kill, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await killer.wait()
        process.kill()
        await process.wait()
        reading.cancel()
        return None, b"", b""


def is_run_evidence(result: Record) -> bool:
    """Whether a checkpointed python_run result has the shape this module produces (the code is not re-run)."""
    text, code, files = result.get("text"), result.get("code"), result.get("data_files")
    return (set(result) <= EVIDENCE_KEYS and result.get("evidence_type") == "python_run" and result.get("path") == TOOL_NAME
            and isinstance(files, list) and all(isinstance(item, dict) and set(item) == {"path", "sha256"}
                                                and all(isinstance(value, str) for value in item.values()) for item in files)
            and isinstance(text, str) and isinstance(code, str) and result.get("exit_code") == 0
            and result.get("code_sha256") == hashlib.sha256(code.encode("utf-8")).hexdigest()
            and result.get("sha256") == hashlib.sha256(text.encode("utf-8")).hexdigest())


class PythonSandbox:
    def __init__(self, image: str, *, docker: str = "docker", runner: Runner | None = None,
                 timeout: float = TIMEOUT_SECONDS) -> None:
        self.image = image
        self.docker = docker
        self.timeout = timeout
        self._runner = runner or docker_runner

    def status(self) -> Record:
        return {"enabled": True, "image": self.image, "timeout_seconds": self.timeout, "memory": MEMORY, "network": False}

    def definition(self) -> Record:
        return {"type": "function", "function": {
            "name": TOOL_NAME,
            "description": (f"Run Python 3 code in an isolated container: no network, the selected sources read-only under "
                            f"/data at their workspace paths, {int(self.timeout)} s and {MEMORY} limits. print() the results; "
                            "the output of a successful run is evidence cited as [python_run EID]."),
            "parameters": PARAMETERS}}

    async def check(self) -> None:
        """Fail at startup, not mid-Run, if Docker or the image is missing."""
        code, _, stderr = await self._runner([self.docker, "image", "inspect", "--format", "{{.Id}}", self.image], b"", 30.0,
                                             [self.docker, "version"])
        if code != 0:
            raise RuntimeError(f"--python-sandbox: image {self.image!r} is not available to {self.docker!r} "
                               f"(pull it first with `docker pull {self.image}`): {stderr.decode('utf-8', 'replace')[:200]}")

    def command(self, name: str, data: Path) -> list[str]:
        return [self.docker, "run", "--rm", "--name", name, "--pull", "never", "--interactive",
                "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
                "--memory", MEMORY, "--memory-swap", MEMORY, "--cpus", CPUS, "--pids-limit", PIDS,
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "65534:65534",
                "--workdir", "/data", "--mount", f"type=bind,source={data},target=/data,readonly",
                self.image, "python", "-I", "-"]

    @staticmethod
    def _stage(root: Path, sources: list[Record], data: Path) -> list[Record]:
        """Copy the selected sources, as admitted, into the folder the container sees."""
        files = []
        for source in sources:
            raw = source_bytes(root, source["path"])
            if hashlib.sha256(raw).hexdigest() != source["sha256"]:
                raise RunFailure("source_changed", "The source changed after task admission. Start a new task with its current version.")
            target = data.joinpath(*Path(source["path"]).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            files.append({"path": source["path"], "sha256": source["sha256"]})
        for folder, _, names in os.walk(data):  # readable by the container's unprivileged user
            os.chmod(folder, 0o755)
            for file in names:
                os.chmod(os.path.join(folder, file), 0o644)
        return files

    async def run(self, code: str, root: Path, sources: list[Record]) -> Record:
        data = Path(tempfile.mkdtemp(prefix="agent-harness-py-"))
        try:
            files = await asyncio.to_thread(self._stage, root, sources, data)
            name = f"agent-harness-py-{uuid4().hex[:12]}"
            started = time.monotonic()
            exit_code, stdout, stderr = await self._runner(self.command(name, data), code.encode("utf-8"), self.timeout,
                                                           [self.docker, "kill", name])
        finally:
            await asyncio.to_thread(shutil.rmtree, data, True)
        duration = int((time.monotonic() - started) * 1000)
        error_tail = stderr.decode("utf-8", "replace")[-MAX_STDERR_BYTES:].strip()
        if exit_code is None:
            raise RunFailure("python_timeout", f"The code ran longer than {int(self.timeout)} s and was stopped.",
                             correctable_fields=("code",))
        if exit_code == 125 or (exit_code != 0 and "Unable to find image" in error_tail):
            raise RunFailure("sandbox_unavailable", "Docker could not start the sandbox container: " + error_tail[:300])
        if exit_code != 0:
            hint = " (out of memory?)" if exit_code == 137 else ""
            raise RunFailure("python_failed", f"The code exited with {exit_code}{hint}. stderr:\n{error_tail or '(empty)'}",
                             correctable_fields=("code",))
        cut = len(stdout) > MAX_STDOUT_BYTES
        text = stdout[:MAX_STDOUT_BYTES].decode("utf-8", "replace").replace("\r\n", "\n")
        if cut:
            text = "[truncated: only the beginning of the output was kept]\n" + text
        return {"evidence_type": "python_run", "path": TOOL_NAME, "image": self.image, "code": code,
                "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(), "data_files": files, "exit_code": 0,
                "duration_ms": duration, "truncated": cut, "text": text,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                **({"stderr": error_tail} if error_tail else {}),
                "line_start": 1, "line_end": max(1, len(text.splitlines()))}
