"""The public HTTP/WebSocket interface for the local workbench."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, WebSocket
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .contracts import RunFailure, RunLimits

if TYPE_CHECKING:
    # The Anthropic SDK takes about two seconds to import; load it only for Claude.
    import httpx2
from .checkpoints import resume_blockers
from .event_stream import stream_run
from .implementation import source_identity
from .local import LocalOriginMiddleware
from .model import DecisionModel, FixtureTransport, ModelAdapter, ModelSettings
from .ownership import StoreLease
from .runtime import Runtime
from .sources import MAX_SOURCES, admit_sources, catalog, read_source, relevant_excerpts, workspace_root
from .storage import Store


class WorkspaceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    workspace_id: str
    title: str = Field(default="新任務", min_length=1, max_length=100)


class ArtifactReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    artifact_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    task: str = Field(min_length=1, max_length=8000)
    sources: list[str] = Field(min_length=1, max_length=MAX_SOURCES)
    submission_key: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,128}$", strict=True)
    base_artifact: ArtifactReference | None = None
    limits: RunLimits = Field(default_factory=RunLimits)
    require_publish_approval: bool = Field(default=False, strict=True)
    publication_workflow: Literal["direct", "draft_review"] = "direct"
    completion_workflow: Literal["model_summary", "artifact_receipt"] = "model_summary"


class ResumeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    checkpoint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    submission_key: str = Field(pattern=r"^[A-Za-z0-9_-]{8,128}$")


class MemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)
    title: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=2000)
    kind: Literal["preference", "fact"] = "preference"
    keywords: list[Annotated[str, Field(min_length=1, max_length=40)]] = Field(default_factory=list, max_length=12)
    source_note: str = Field(default="", max_length=300)


class MemoryRevisionInput(MemoryInput):
    expected_version: int = Field(ge=1, strict=True)


class MemoryDeleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, strict=True)


class ApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "deny"]
    parameters_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def create_app(
    store_directory: Path, *, model_transport: httpx.AsyncBaseTransport | None = None,
    model_settings: ModelSettings | None = None,
    anthropic_transport: "httpx2.AsyncBaseTransport | None" = None,
    fault_once_source_read: bool = False,
    hold_first_tool: Literal["source_read", "artifact_write"] | None = None,
) -> FastAPI:
    """Build the workbench app.

    `model_transport` replaces the HTTP transport for Chat Completions providers
    (fixture, local Ollama, OpenAI-compatible). `anthropic_transport` replaces the
    official Anthropic SDK's transport for Claude. Tests use them; production
    leaves both unset.
    """
    storage: Store
    runtime: Runtime
    model: DecisionModel
    settings = model_settings or ModelSettings()
    validation: dict[str, Any] = {"fault_once_source_read": fault_once_source_read}
    if hold_first_tool is not None:
        validation["hold_first_tool"] = hold_first_tool

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal storage, runtime, model
        lease = await asyncio.to_thread(StoreLease, store_directory)
        try:
            storage = await asyncio.to_thread(Store, store_directory)
            transport = model_transport
            if transport is None and settings.mode == "fixture":
                transport = httpx.MockTransport(FixtureTransport())
            try:
                await storage.recover_interrupted()
                if settings.mode == "anthropic":
                    from .anthropic_adapter import AnthropicAdapter

                    claude = AnthropicAdapter(settings, transport=anthropic_transport)
                    await claude.prepare()
                    model = claude
                    runtime = Runtime(storage, model, hold_first_tool=hold_first_tool)
                    try:
                        yield
                    finally:
                        await runtime.close()
                        await claude.aclose()
                else:
                    async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                                                 timeout=httpx.Timeout(connect=5, read=None, write=30, pool=5)) as client:
                        adapter = ModelAdapter(client, settings, transport_fixture=transport is not None)
                        await adapter.prepare()
                        model = adapter
                        runtime = Runtime(storage, model, hold_first_tool=hold_first_tool)
                        try:
                            yield
                        finally:
                            await runtime.close()
            finally:
                await storage.close()
        finally:
            await asyncio.to_thread(lease.close)

    app = FastAPI(title="Agent Harness Demo", lifespan=lifespan)
    app.add_middleware(LocalOriginMiddleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    package = Path(__file__).resolve().parent
    implementation = source_identity(package)
    app.mount("/assets", StaticFiles(directory=package / "static"), name="assets")

    @app.get("/", response_class=FileResponse)
    async def home() -> FileResponse:
        return FileResponse(package / "static" / "index.html")

    @app.get("/api/config")
    async def config() -> dict[str, Any]:
        return {"mode": settings.mode, "model": model.info, "demo_path": str(package / "demo_sources"),
                "validation": validation, "implementation": implementation}

    @app.post("/api/workspaces", status_code=201)
    async def add_workspace(body: WorkspaceInput) -> dict[str, Any]:
        try:
            root = await asyncio.to_thread(workspace_root, body.path)
        except RunFailure as error:
            raise HTTPException(422, error.message) from None
        return await storage.add_workspace(root)

    @app.get("/api/workspaces")
    async def list_workspaces() -> list[dict[str, Any]]:
        return await storage.workspaces()

    @app.get("/api/workspaces/{workspace_id}/sources")
    async def list_sources(workspace_id: str) -> dict[str, Any]:
        workspace = await storage.workspace(workspace_id)
        if not workspace:
            raise HTTPException(404, "Workspace not found.")
        sources = await asyncio.to_thread(catalog, Path(workspace["path"]))
        return {"sources": sources}

    @app.get("/api/workspaces/{workspace_id}/source")
    async def preview_source(workspace_id: str, path: str = Query(max_length=500)) -> dict[str, Any]:
        workspace = await storage.workspace(workspace_id)
        if not workspace:
            raise HTTPException(404, "Workspace not found.")
        root = Path(workspace["path"])
        try:
            await asyncio.to_thread(admit_sources, root, [path])
            return await asyncio.to_thread(read_source, root, path)
        except RunFailure as error:
            raise HTTPException(422, {"code": error.code, "message": error.message}) from None

    @app.post("/api/workspaces/{workspace_id}/memories", status_code=201)
    async def add_memory(workspace_id: str, body: MemoryInput) -> dict[str, Any]:
        if not await storage.workspace(workspace_id):
            raise HTTPException(404, "Workspace not found.")
        return await storage.add_memory(workspace_id, body.title, body.content, body.kind, body.keywords, body.source_note)

    @app.get("/api/workspaces/{workspace_id}/memories")
    async def list_memories(workspace_id: str) -> list[dict[str, Any]]:
        if not await storage.workspace(workspace_id):
            raise HTTPException(404, "Workspace not found.")
        return await storage.memories(workspace_id)

    @app.get("/api/workspaces/{workspace_id}/memories/{memory_id}")
    async def get_memory(workspace_id: str, memory_id: str) -> dict[str, Any]:
        memory = await storage.memory(workspace_id, memory_id)
        if memory is None:
            raise HTTPException(404, "Memory not found in this workspace.")
        return memory

    @app.get("/api/workspaces/{workspace_id}/memories/{memory_id}/versions")
    async def memory_versions(workspace_id: str, memory_id: str) -> list[dict[str, Any]]:
        await get_memory(workspace_id, memory_id)
        return await storage.memory_versions(workspace_id, memory_id)

    async def revise_memory(workspace_id: str, memory_id: str, version: int,
                            replacement: dict[str, Any] | None) -> dict[str, Any]:
        try:
            result = await storage.revise_memory(workspace_id, memory_id, version, replacement)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        if result is None:
            raise HTTPException(404, "Memory not found in this workspace.")
        return result

    @app.post("/api/workspaces/{workspace_id}/memories/{memory_id}/versions", status_code=201)
    async def edit_memory(workspace_id: str, memory_id: str, body: MemoryRevisionInput) -> dict[str, Any]:
        return await revise_memory(workspace_id, memory_id, body.expected_version,
                                   body.model_dump(exclude={"expected_version"}))

    @app.post("/api/workspaces/{workspace_id}/memories/{memory_id}/delete", status_code=201)
    async def delete_memory(workspace_id: str, memory_id: str, body: MemoryDeleteInput) -> dict[str, Any]:
        return await revise_memory(workspace_id, memory_id, body.expected_version, None)

    @app.post("/api/sessions", status_code=201)
    async def add_session(body: SessionInput) -> dict[str, Any]:
        if not await storage.workspace(body.workspace_id):
            raise HTTPException(404, "Workspace not found.")
        return await storage.add_session(body.workspace_id, body.title)

    @app.get("/api/sessions")
    async def list_sessions() -> list[dict[str, Any]]:
        return await storage.sessions()

    @app.post("/api/sessions/{session_id}/runs", status_code=202)
    async def start_run(session_id: str, body: RunInput) -> dict[str, Any]:
        session = await storage.session(session_id)
        if not session:
            raise HTTPException(404, "Session not found.")
        workspace = await storage.workspace(session["workspace_id"])
        assert workspace is not None
        root = Path(workspace["path"])
        effective = body.model_dump(exclude={"submission_key"})
        if body.publication_workflow == "direct":
            del effective["publication_workflow"]
        if body.completion_workflow == "model_summary":
            del effective["completion_workflow"]
        effective["sources"] = sorted(effective["sources"])
        digest = hashlib.sha256(json.dumps(effective, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
        try:
            if body.submission_key is not None:
                prior = await storage.submission(session_id, body.submission_key, digest)
                if prior is not None:
                    return prior
            sources = await asyncio.to_thread(admit_sources, root, body.sources)
            excerpts = await asyncio.to_thread(relevant_excerpts, root, sources, body.task, body.limits.max_excerpt_bytes)
            policy: dict[str, Any] = {"require_publish_approval": body.require_publish_approval}
            if body.publication_workflow == "draft_review":
                policy["publication_workflow"] = body.publication_workflow
            if body.completion_workflow == "artifact_receipt":
                policy["completion_workflow"] = body.completion_workflow
            run, created = await storage.add_run(session_id, body.task, sources, body.limits.model_dump(), model.info,
                                        validation,
                                        policy,
                                        body.base_artifact.model_dump() if body.base_artifact else None, excerpts,
                                        body.submission_key, digest)
        except RunFailure as error:
            if body.submission_key is not None:
                try:
                    prior = await storage.submission(session_id, body.submission_key, digest)
                except ValueError as conflict:
                    raise HTTPException(409, str(conflict)) from None
                if prior is not None:
                    return prior
            raise HTTPException(422, {"code": error.code, "message": error.message}) from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        if created:
            runtime.start(run["id"], root)
        return run

    @app.get("/api/sessions/{session_id}/runs")
    async def list_runs(session_id: str) -> list[dict[str, Any]]:
        if not await storage.session(session_id):
            raise HTTPException(404, "Session not found.")
        return await storage.session_runs(session_id)

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        run = await storage.run(run_id)
        if run is None:
            raise HTTPException(404, "Run not found.")
        return run

    @app.get("/api/runs/{run_id}/checkpoint")
    async def get_checkpoint(run_id: str) -> dict[str, Any]:
        await get_run(run_id)
        try:
            checkpoint = await storage.checkpoint(run_id)
        except RunFailure as error:
            raise HTTPException(409, {"code": error.code, "message": error.message}) from None
        if checkpoint is None:
            raise HTTPException(409, "This Run has no recovery checkpoint.")
        return {**checkpoint, "resume_blockers": resume_blockers(checkpoint)}

    @app.post("/api/runs/{run_id}/resume", status_code=202)
    async def resume_run(run_id: str, body: ResumeInput) -> dict[str, Any]:
        parent = await get_run(run_id)
        digest = hashlib.sha256(json.dumps({"resume_from": run_id, "checkpoint_sha256": body.checkpoint_sha256},
                                          sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        try:
            previous = await storage.submission(parent["session_id"], body.submission_key, digest)
            if previous is not None:
                return previous
            checkpoint = await storage.checkpoint(run_id)
            if checkpoint is None or checkpoint["sha256"] != body.checkpoint_sha256:
                raise RunFailure("checkpoint_changed", "Reload the saved checkpoint before continuing.")
            if resume_blockers(checkpoint):
                raise RunFailure("checkpoint_blocked", "Unverified effects must be resolved before continuing.")
            if any(checkpoint["model"].get(key) != model.info.get(key) for key in
                   ("mode", "name", "digest", "context_profile", "stream", "context_capacity_tokens")) or (
                    checkpoint["model"].get("decision_protocol", "native") != model.info.get("decision_protocol", "native")):
                raise RunFailure("checkpoint_model_changed", "Continue with the same verified model configuration.")
            session = await storage.session(parent["session_id"])
            assert session is not None
            workspace = await storage.workspace(session["workspace_id"])
            assert workspace is not None
            root = Path(workspace["path"])
            sources = await asyncio.to_thread(admit_sources, root, [source["path"] for source in checkpoint["sources"]])
            if sources != checkpoint["sources"]:
                raise RunFailure("checkpoint_sources_changed", "Selected sources no longer match the checkpoint versions.")
            base = parent["base_artifact"]
            base_reference = {"run_id": base["run_id"], "artifact_id": base["id"]} if base else None
            run, created = await storage.add_run(parent["session_id"], checkpoint["task"], sources, checkpoint["limits"], model.info,
                validation, checkpoint["policy"], base_reference, submission_key=body.submission_key,
                request_sha256=digest, resume_checkpoint=checkpoint)
        except RunFailure as error:
            try:
                previous = await storage.submission(parent["session_id"], body.submission_key, digest)
            except ValueError as conflict:
                raise HTTPException(409, str(conflict)) from None
            if previous is not None:
                return previous
            raise HTTPException(409, {"code": error.code, "message": error.message}) from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        if created:
            runtime.start(run["id"], root)
        return run

    @app.get("/api/runs/{run_id}/events")
    async def get_events(run_id: str) -> dict[str, Any]:
        await get_run(run_id)
        return {"events": await storage.events(run_id)}

    @app.post("/api/runs/{run_id}/approvals/{approval_id}")
    async def decide_approval(run_id: str, approval_id: str, body: ApprovalInput) -> dict[str, Any]:
        try:
            result = await storage.decide_approval(run_id, approval_id, body.decision, body.parameters_sha256)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        if result is None:
            raise HTTPException(404, "Approval not found.")
        await runtime.notify()
        return result

    @app.post("/api/runs/{run_id}/cancel", status_code=202)
    async def cancel_run(run_id: str) -> dict[str, Any]:
        await get_run(run_id)
        await runtime.cancel(run_id)
        return await get_run(run_id)

    @app.post("/api/runs/{run_id}/validation/{call_id}/release")
    async def release_validation(run_id: str, call_id: str) -> dict[str, bool]:
        if hold_first_tool is None or not await runtime.release_validation(run_id, call_id):
            raise HTTPException(404, "No controlled validation gate is held for this call.")
        return {"released": True}

    @app.get("/api/runs/{run_id}/artifacts/{artifact_id}")
    async def download(run_id: str, artifact_id: str) -> Response:
        artifact = await storage.artifact(run_id, artifact_id)
        if artifact is None:
            raise HTTPException(404, "Artifact not found.")
        try:
            raw = await asyncio.to_thread((storage.directory / "artifacts" / artifact_id).read_bytes)
        except OSError:
            raise HTTPException(409, "The saved artifact is unavailable.") from None
        if hashlib.sha256(raw).hexdigest() != artifact["sha256"]:
            raise HTTPException(409, "The saved artifact no longer matches its recorded hash.")
        return Response(raw, media_type="text/plain; charset=utf-8", headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(artifact['name'])}",
            "X-Content-Type-Options": "nosniff",
        })

    @app.websocket("/api/runs/{run_id}/stream")
    async def stream(websocket: WebSocket, run_id: str, after: str = "0", flow: str = "") -> None:
        await websocket.accept()
        run = await storage.event_position(run_id)
        if run is None:
            await websocket.close(code=4404)
            return
        if not re.fullmatch(r"[0-9]{1,18}", after) or int(after) > run["last_sequence"] or flow not in {"", "ack"}:
            await websocket.close(code=4400, reason="invalid_cursor")
            return
        await stream_run(websocket, storage, runtime.changed, run_id, int(after), flow == "ack")

    return app
