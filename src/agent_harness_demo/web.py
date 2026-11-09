"""The public HTTP/WebSocket interface for the local workbench."""

import asyncio
import difflib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response
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
from .hooks import HookRunner
from .instructions import read_instructions
from .token_usage import session_usage
from .uploads import save_upload, upload_limit
from .mcp_hub import MODEL_NAME, McpHub
from .skills import SKILL_NAME, SkillLibrary
from .local import MIN_TOKEN_LENGTH, AccessTokenMiddleware, LocalOriginMiddleware, SecurityHeadersMiddleware
from .model import DecisionModel, FixtureTransport, ModelAdapter, ModelSettings
from .ownership import StoreLease
from .runtime import Runtime
from .sources import MAX_SOURCES, admit_sources, catalog, read_source, relevant_excerpts, workspace_root
from .storage import Store

# Host names the app answers to; anything else (DNS rebinding) is refused.
TRUSTED_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost")


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
    # Pause at the plan: nothing runs until the user approves it, and each revised plan needs approval too.
    require_plan_approval: bool = Field(default=False, strict=True)
    # Apply the workspace's AGENTS.md; its text and SHA-256 are pinned into the Run at admission.
    project_instructions: bool = Field(default=False, strict=True)
    publication_workflow: Literal["direct", "draft_review"] = "direct"
    completion_workflow: Literal["model_summary", "artifact_receipt"] = "model_summary"
    # Read-only MCP tools this Run may use, chosen like its sources (names from /api/config).
    mcp_tools: list[Annotated[str, Field(pattern=MODEL_NAME.pattern)]] = Field(default_factory=list, max_length=16)
    # One of the configured models (and, for Claude, an effort level) instead of the defaults.
    model: str | None = Field(default=None, min_length=1, max_length=100)
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    # Installed skills whose descriptions this Run sees; their instructions load on request.
    skills: list[Annotated[str, Field(pattern=SKILL_NAME.pattern, max_length=64)]] = Field(default_factory=list, max_length=16)


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


class MessageInput(BaseModel):
    """A message the user adds to a running Run; `message_id` makes re-sending idempotent."""
    model_config = ConfigDict(extra="forbid")
    message_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    text: str = Field(min_length=1, max_length=4000)


def create_app(
    store_directory: Path, *, model_transport: httpx.AsyncBaseTransport | None = None,
    model_settings: ModelSettings | None = None,
    anthropic_transport: "httpx2.AsyncBaseTransport | None" = None,
    fault_once_source_read: bool = False,
    hold_first_tool: Literal["source_read", "artifact_write"] | None = None,
    access_token: str | None = None,
    mcp_config: Path | None = None,
    skills_dir: Path | None = None,
    prices: dict[str, dict[str, float]] | None = None,
    hooks_config: Path | None = None,
    hooks_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the workbench app.

    `model_transport` replaces the HTTP transport for Chat Completions providers
    (fixture, local Ollama, OpenAI-compatible). `anthropic_transport` replaces the
    official Anthropic SDK's transport for Claude. Tests use them; production
    leaves both unset.

    `access_token`, when set, is required on every request and WebSocket (the
    command-line entry point always sets one). Without it the app relies only on
    the loopback Host and same-origin checks, which suits in-process use and tests.

    `mcp_config` names an `mcpServers` file; its servers start with the app and
    only their allowed, read-only tools can be selected by a Run.

    `prices` maps a model name to US dollars per million input and output tokens;
    a Run pins its model's price at admission and reports an estimated cost.
    """
    if access_token is not None and len(access_token) < MIN_TOKEN_LENGTH:
        raise ValueError(f"The access token must have at least {MIN_TOKEN_LENGTH} characters.")
    storage: Store
    runtime: Runtime
    model: DecisionModel
    settings = model_settings or ModelSettings()
    validation: dict[str, Any] = {"fault_once_source_read": fault_once_source_read}
    if hold_first_tool is not None:
        validation["hold_first_tool"] = hold_first_tool
    mcp = McpHub.from_config(mcp_config) if mcp_config is not None else None
    skills = SkillLibrary.from_directory(skills_dir) if skills_dir is not None else None
    hooks = HookRunner.from_config(hooks_config, hooks_transport) if hooks_config is not None else None

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
                if mcp is not None:
                    await mcp.start()
                if settings.mode == "anthropic":
                    from .anthropic_adapter import AnthropicAdapter

                    claude = AnthropicAdapter(settings, transport=anthropic_transport)
                    await claude.prepare()
                    model = claude
                    runtime = Runtime(storage, model, hold_first_tool=hold_first_tool, mcp=mcp, skills=skills, hooks=hooks)
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
                        runtime = Runtime(storage, model, hold_first_tool=hold_first_tool, mcp=mcp, skills=skills, hooks=hooks)
                        try:
                            yield
                        finally:
                            await runtime.close()
            finally:
                if mcp is not None:
                    await mcp.close()
                if hooks is not None:
                    await hooks.close()
                await storage.close()
        finally:
            await asyncio.to_thread(lease.close)

    app = FastAPI(title="Agent Harness Demo", lifespan=lifespan)
    # Added last runs first: headers on every response, then Host, Origin and the token.
    if access_token is not None:
        app.add_middleware(AccessTokenMiddleware, token=access_token)
    app.add_middleware(LocalOriginMiddleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(TRUSTED_HOSTS))
    app.add_middleware(SecurityHeadersMiddleware)
    package = Path(__file__).resolve().parent
    implementation = source_identity(package)
    app.mount("/assets", StaticFiles(directory=package / "static"), name="assets")

    @app.get("/", response_class=FileResponse)
    async def home() -> FileResponse:
        return FileResponse(package / "static" / "index.html")

    @app.get("/api/config")
    async def config() -> dict[str, Any]:
        return {"mode": settings.mode, "model": model.info, "demo_path": str(package / "demo_sources"),
                "validation": validation, "implementation": implementation,
                "mcp": mcp.status() if mcp is not None else {"servers": {}},
                "skills": skills.status() if skills is not None else {"available": [], "excluded": []},
                "prices": prices or {}, "hooks": hooks.status() if hooks is not None else []}

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

    @app.get("/api/workspaces/{workspace_id}/instructions")
    async def workspace_instructions(workspace_id: str) -> dict[str, Any]:
        """The workspace's AGENTS.md, which a Run applies only when it opts in."""
        workspace = await storage.workspace(workspace_id)
        if not workspace:
            raise HTTPException(404, "Workspace not found.")
        try:
            return {"instructions": await asyncio.to_thread(read_instructions, Path(workspace["path"])), "error": None}
        except RunFailure as error:
            return {"instructions": None, "error": {"code": error.code, "message": error.message}}

    @app.post("/api/workspaces/{workspace_id}/uploads")
    async def upload_file(workspace_id: str, request: Request, name: str = Query(max_length=200)) -> JSONResponse:
        """Save the raw request body as uploads/<name> in the workspace; it is then listed as a source."""
        workspace = await storage.workspace(workspace_id)
        if not workspace:
            raise HTTPException(404, "Workspace not found.")
        statuses = {"invalid_upload_name": 422, "source_unavailable": 422, "source_too_large": 413,
                    "upload_exists": 409, "uploads_unavailable": 409}
        try:
            limit = upload_limit(name)
            body = bytearray()
            async for chunk in request.stream():  # stop reading as soon as the limit is passed
                body += chunk
                if len(body) > limit:
                    raise RunFailure("source_too_large", "The file exceeds the size limit for its type.")
            saved = await asyncio.to_thread(save_upload, Path(workspace["path"]), name, bytes(body))
        except RunFailure as error:
            raise HTTPException(statuses.get(error.code, 422), {"code": error.code, "message": error.message}) from None
        return JSONResponse(saved, status_code=201 if saved["created"] else 200)

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
        for optional in ("model", "effort"):
            if effective[optional] is None:
                del effective[optional]
        for flag in ("require_plan_approval", "project_instructions"):
            if not effective[flag]:
                del effective[flag]
        if not body.skills:
            del effective["skills"]
        else:
            effective["skills"] = sorted(set(body.skills))
        if not body.mcp_tools:
            del effective["mcp_tools"]
        else:
            effective["mcp_tools"] = sorted(set(body.mcp_tools))
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
            if body.require_plan_approval:
                policy["require_plan_approval"] = True
            if body.project_instructions:
                instructions = await asyncio.to_thread(read_instructions, root)
                if instructions is None:
                    raise RunFailure("project_instructions_unavailable", "This workspace has no AGENTS.md with instructions.")
                policy["project_instructions"] = instructions
            if body.publication_workflow == "draft_review":
                policy["publication_workflow"] = body.publication_workflow
            if body.completion_workflow == "artifact_receipt":
                policy["completion_workflow"] = body.completion_workflow
            if body.mcp_tools:
                if mcp is None:
                    raise RunFailure("mcp_tool_unavailable", "No MCP servers are configured for this workbench.")
                mcp.select(body.mcp_tools)
                policy["mcp_tools"] = sorted(set(body.mcp_tools))
            if body.skills:
                if skills is None:
                    raise RunFailure("skill_unavailable", "No skills directory is configured for this workbench.")
                skills.select(body.skills)
                policy["skills"] = sorted(set(body.skills))
            run_model = dict(model.info)
            choices = model.info.get("choices", {})
            if body.model is not None and body.model != model.info["name"]:
                if body.model not in choices.get("models", []):
                    raise RunFailure("model_unavailable", f"{body.model} is not one of this workbench's models.")
                run_model["name"] = body.model
            if body.effort is not None:
                if body.effort not in choices.get("efforts", []):
                    raise RunFailure("effort_unavailable", "This model provider has no effort setting.")
                run_model["effort"] = body.effort
            if (prices or {}).get(run_model["name"]):
                run_model["price_per_mtok"] = dict((prices or {})[run_model["name"]])
            run, created = await storage.add_run(session_id, body.task, sources, body.limits.model_dump(), run_model,
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

    @app.get("/api/sessions/{session_id}/usage")
    async def get_session_usage(session_id: str) -> dict[str, Any]:
        """Token totals over the Session's Runs and, when they have prices, a summed estimate."""
        if not await storage.session(session_id):
            raise HTTPException(404, "Session not found.")
        return session_usage(await storage.session_runs(session_id))

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
            allowed_names = model.info.get("choices", {}).get("models", [model.info.get("name")])
            if checkpoint["model"].get("name") not in allowed_names or any(
                    checkpoint["model"].get(key) != model.info.get(key) for key in
                    ("mode", "digest", "context_profile", "stream", "context_capacity_tokens")) or (
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
            resumed_model = {**model.info, "name": checkpoint["model"]["name"],
                             **({"effort": checkpoint["model"]["effort"]} if "effort" in checkpoint["model"] else {})}
            # The resumed Run's requests are new spending, so they use today's configured price.
            if (prices or {}).get(resumed_model["name"]):
                resumed_model["price_per_mtok"] = dict((prices or {})[resumed_model["name"]])
            run, created = await storage.add_run(parent["session_id"], checkpoint["task"], sources, checkpoint["limits"], resumed_model,
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

    @app.post("/api/runs/{run_id}/messages", status_code=202)
    async def add_message(run_id: str, body: MessageInput) -> dict[str, Any]:
        if not body.text.strip():
            raise HTTPException(422, "The message is empty.")
        try:
            result = await storage.add_message(run_id, body.message_id, body.text)
        except KeyError:
            raise HTTPException(404, "Run not found.") from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
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

    async def verified_text(artifact: dict[str, Any]) -> str:
        try:
            raw: bytes = await asyncio.to_thread((storage.directory / "artifacts" / str(artifact["id"])).read_bytes)
        except OSError:
            raise HTTPException(409, "The saved artifact is unavailable.") from None
        if hashlib.sha256(raw).hexdigest() != artifact["sha256"]:
            raise HTTPException(409, "The saved artifact no longer matches its recorded hash.")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(409, "The saved artifact is not UTF-8 text.") from None

    @app.get("/api/runs/{run_id}/artifacts/{artifact_id}/diff")
    async def diff_artifact(run_id: str, artifact_id: str,
                            against: Annotated[str | None, Query(pattern=r"^[a-f0-9]{32}$")] = None) -> dict[str, Any]:
        """Unified diff from another version in the same Session (default: the parent) to this artifact."""
        run = await get_run(run_id)
        newer = await storage.artifact(run_id, artifact_id)
        if newer is None:
            raise HTTPException(404, "Artifact not found.")
        other = against or newer.get("parent_artifact_id")
        if other is None:
            raise HTTPException(409, "This artifact has no earlier version to compare with.")
        older = await storage.session_artifact(run["session_id"], other)
        if older is None:
            raise HTTPException(404, "The other version is not an artifact of this Session.")
        before, after = await verified_text(older), await verified_text(newer)
        lines = list(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                          fromfile=f"{older['name']} v{older['version']}",
                                          tofile=f"{newer['name']} v{newer['version']}"))
        changed = lines[2:]  # only the first two lines are file headers; a removed "---" rule reads "----"
        summary = ("id", "run_id", "name", "version", "sha256")
        return {"from": {key: older[key] for key in summary}, "to": {key: newer[key] for key in summary},
                "identical": before == after, "diff": "".join(lines),
                "added": sum(line.startswith("+") for line in changed),
                "removed": sum(line.startswith("-") for line in changed)}

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
