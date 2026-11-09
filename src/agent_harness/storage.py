"""SQLite persistence; all connection work runs outside the event loop."""

import asyncio
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import sqlite3
from threading import RLock
from typing import Any, TypeVar
from uuid import uuid4

from .context import history_turn
from .checkpoints import checkpoint_payload, resume_blockers
from .contracts import PreparedCall, RunFailure
from .token_usage import add_token_usage, estimated_cost

T = TypeVar("T")
Record = dict[str, Any]
TERMINAL = {"completed", "failed", "interrupted", "cancelled"}
# A user message can be added while the Run can still make another model decision.
ACCEPTS_MESSAGES = {"queued", "running", "waiting_approval"}
MAX_RUN_MESSAGES = 20


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _artifact_identity(run_id: str, call_id: str) -> str:
    return hashlib.sha256(json.dumps([run_id, call_id], ensure_ascii=True).encode()).hexdigest()[:32]


class Store:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self._lock = RLock()
        self._connection = sqlite3.connect(directory / "workspace.sqlite3", check_same_thread=False)
        try:
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS workspaces (id TEXT PRIMARY KEY, path TEXT UNIQUE, name TEXT)"
            )
            self._connection.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, workspace_id TEXT REFERENCES workspaces(id), title TEXT, created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, session_id TEXT REFERENCES sessions(id), task TEXT,
                    sources_json TEXT, limits_json TEXT, status TEXT, mode TEXT,
                    model_calls INTEGER DEFAULT 0, tool_calls INTEGER DEFAULT 0,
                    result TEXT DEFAULT '', error_json TEXT, created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    run_id TEXT PRIMARY KEY REFERENCES runs(id), sha256 TEXT, payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS submissions (
                    session_id TEXT REFERENCES sessions(id), key TEXT, request_sha256 TEXT,
                    run_id TEXT REFERENCES runs(id), PRIMARY KEY(session_id, key)
                );
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT REFERENCES runs(id), sequence INTEGER, kind TEXT, data_json TEXT, timestamp TEXT,
                    PRIMARY KEY (run_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, run_id TEXT REFERENCES runs(id), name TEXT, sha256 TEXT, bytes INTEGER
                );
                CREATE TABLE IF NOT EXISTS memory_versions (
                    id TEXT, workspace_id TEXT REFERENCES workspaces(id), version INTEGER,
                    title TEXT, content TEXT, kind TEXT, keywords_json TEXT, origin_json TEXT,
                    deleted INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT,
                    PRIMARY KEY(id, version)
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY, run_id TEXT REFERENCES runs(id), call_id TEXT, tool TEXT,
                    arguments_json TEXT, scope_json TEXT, parameters_sha256 TEXT, status TEXT,
                    created_at TEXT, expires_at TEXT, UNIQUE(run_id, call_id)
                );
            """)
            columns = {row[1] for row in self._connection.execute("PRAGMA table_info(runs)")}
            if "model_json" not in columns:
                self._connection.execute("ALTER TABLE runs ADD COLUMN model_json TEXT NOT NULL DEFAULT '{}'")
            if "validation_json" not in columns:
                self._connection.execute("ALTER TABLE runs ADD COLUMN validation_json TEXT NOT NULL DEFAULT '{}'")
            if "policy_json" not in columns:
                self._connection.execute("ALTER TABLE runs ADD COLUMN policy_json TEXT NOT NULL DEFAULT '{}'")
            if "cancellation_json" not in columns:
                self._connection.execute("ALTER TABLE runs ADD COLUMN cancellation_json TEXT")
            if "base_json" not in columns:
                self._connection.execute("ALTER TABLE runs ADD COLUMN base_json TEXT")
            if "context_json" not in columns:
                self._connection.execute("ALTER TABLE runs ADD COLUMN context_json TEXT NOT NULL DEFAULT '{}'")
            if "recovery_json" not in columns:
                self._connection.execute("ALTER TABLE runs ADD COLUMN recovery_json TEXT")
            if "token_usage_json" not in columns:
                self._connection.execute("ALTER TABLE runs ADD COLUMN token_usage_json TEXT NOT NULL DEFAULT '{}'")
            if "resume_json" not in columns:
                self._connection.execute("ALTER TABLE runs ADD COLUMN resume_json TEXT")
            artifact_columns = {row[1] for row in self._connection.execute("PRAGMA table_info(artifacts)")}
            if "parent_artifact_id" not in artifact_columns:
                self._connection.execute("ALTER TABLE artifacts ADD COLUMN parent_artifact_id TEXT REFERENCES artifacts(id)")
            if "version" not in artifact_columns:
                self._connection.execute("ALTER TABLE artifacts ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
            if "call_id" not in artifact_columns:
                self._connection.execute("ALTER TABLE artifacts ADD COLUMN call_id TEXT")
            self._connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS artifact_call_identity ON artifacts(run_id, call_id)")
            self._connection.commit()
        except BaseException:
            self._connection.close()
            raise

    def _transaction(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        with self._lock, self._connection:
            return operation(self._connection)

    async def _run(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        return await asyncio.to_thread(self._transaction, operation)

    def _reconcile_artifact(self, connection: sqlite3.Connection, run: sqlite3.Row, intent: Record) -> Record | None:
        arguments = intent.get("arguments", {})
        if not isinstance(arguments.get("content"), str) or not isinstance(arguments.get("name"), str):
            return None
        raw = arguments["content"].encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        row = connection.execute("SELECT * FROM artifacts WHERE run_id=? AND call_id=?", (run["id"], intent["call_id"])).fetchone()
        identity = row["id"] if row else _artifact_identity(run["id"], intent["call_id"])
        try:
            with (self.directory / "artifacts" / identity).open("rb") as handle:
                if handle.read(len(raw) + 1) != raw:
                    return None
        except OSError:
            return None
        if row is not None:
            if row["name"] != arguments["name"] or row["sha256"] != digest or row["bytes"] != len(raw):
                return None
        else:
            base = json.loads(run["base_json"] or "null")
            connection.execute("INSERT INTO artifacts (id,run_id,name,sha256,bytes,parent_artifact_id,version,call_id) VALUES (?,?,?,?,?,?,?,?)",
                (identity, run["id"], arguments["name"], digest, len(raw), base["id"] if base else None,
                 base["version"] + 1 if base else 1, intent["call_id"]))
            row = connection.execute("SELECT * FROM artifacts WHERE id=?", (identity,)).fetchone()
            assert row is not None
            self._event(connection, run["id"], "artifact.reconciled", {"call_id": intent["call_id"], "artifact_id": identity,
                         "reason": "existing_bytes_match_dispatched_intent"})
        return self._artifact(row)

    async def recover_interrupted(self) -> None:
        """Called only after the new service has acquired exclusive Store ownership."""
        def recover(connection: sqlite3.Connection) -> None:
            rows = connection.execute(
                "SELECT * FROM runs WHERE status IN ('queued','running','waiting_approval','cancel_requested') "
                "OR (status='interrupted' AND recovery_json IS NULL) ORDER BY rowid").fetchall()
            for run in rows:
                outstanding: dict[str, Record] = {}
                for event in connection.execute(
                    "SELECT kind, data_json FROM events WHERE run_id=? AND kind IN ('tool.dispatched','tool.completed','tool.failed') ORDER BY sequence", (run["id"],)):
                    data = json.loads(event["data_json"])
                    if event["kind"] == "tool.dispatched":
                        outstanding[data["call_id"]] = data
                    else:
                        outstanding.pop(data["call_id"], None)
                uncertain = [{"call_id": data["call_id"], "tool": data["tool"],
                              "effect": data.get("effect", "unknown"), "reason": "executor_lost"}
                             for data in outstanding.values()]
                for attempt in uncertain:
                    self._event(connection, run["id"], "tool.unknown", attempt)
                reconciled: list[Record] = []
                for call_id, intent in outstanding.items():
                    if intent["tool"] == "artifact_write" and intent.get("effect") == "immutable_artifact":
                        artifact = self._reconcile_artifact(connection, run, intent)
                        if artifact is not None:
                            self._event(connection, run["id"], "tool.reconciled", {"call_id": call_id,
                                "tool": "artifact_write", "result": artifact, "verification": "operation_identity_and_exact_bytes"})
                            reconciled.append({"call_id": call_id, "artifact_id": artifact["id"], "sha256": artifact["sha256"]})
                reconciled_ids = {attempt["call_id"] for attempt in reconciled}
                uncertain = [attempt for attempt in uncertain if attempt["call_id"] not in reconciled_ids]
                for approval in connection.execute("SELECT id FROM approvals WHERE run_id=? AND status IN ('pending','approved')", (run["id"],)).fetchall():
                    connection.execute("UPDATE approvals SET status='expired' WHERE id=?", (approval["id"],))
                    self._event(connection, run["id"], "approval.expired", {"id": approval["id"], "reason": "service_restarted"})
                events = [{"kind": row["kind"], "data": json.loads(row["data_json"])} for row in connection.execute(
                    "SELECT kind, data_json FROM events WHERE run_id=? AND kind != 'model.text_delta' ORDER BY sequence", (run["id"],))]
                artifacts = [self._artifact(row) for row in connection.execute("SELECT * FROM artifacts WHERE run_id=? ORDER BY rowid", (run["id"],))]
                payload = checkpoint_payload(dict(run), events, artifacts)
                serialized = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
                digest = hashlib.sha256(serialized.encode()).hexdigest()
                connection.execute("INSERT INTO checkpoints VALUES (?, ?, ?)", (run["id"], digest, serialized))
                self._event(connection, run["id"], "checkpoint.saved", {"sha256": digest, "confirmed": len(payload["confirmed"]),
                                                                       "pending": len(payload["pending"]), "blockers": payload["blockers"]})
                recovery = {"reason": "executor_lost", "previous_status": run["status"], "uncertain_attempts": uncertain,
                            "checkpoint_sha256": digest, "reconciled_attempts": reconciled}
                error = {"code": "executor_lost", "message": "The previous service stopped before this Run completed."}
                connection.execute("UPDATE runs SET status='interrupted', recovery_json=?, error_json=? WHERE id=?",
                                   (json.dumps(recovery), json.dumps(error), run["id"]))
                self._close_messages(connection, run["id"], "interrupted")
                self._event(connection, run["id"], "run.interrupted" if run["status"] != "interrupted" else "recovery.detected", recovery)
        await self._run(recover)

    @staticmethod
    def _checkpoint(connection: sqlite3.Connection, identity: str) -> Record | None:
        row = connection.execute("SELECT * FROM checkpoints WHERE run_id=?", (identity,)).fetchone()
        if row is None:
            return None
        if hashlib.sha256(row["payload_json"].encode()).hexdigest() != row["sha256"]:
            raise RunFailure("checkpoint_corrupt", "The saved checkpoint no longer matches its digest.")
        return {**json.loads(row["payload_json"]), "sha256": row["sha256"]}

    async def checkpoint(self, identity: str) -> Record | None:
        return await self._run(lambda connection: self._checkpoint(connection, identity))

    async def add_workspace(self, root: Path) -> Record:
        def add(connection: sqlite3.Connection) -> Record:
            connection.execute(
                "INSERT OR IGNORE INTO workspaces VALUES (?, ?, ?)",
                (uuid4().hex, str(root), root.name or str(root)),
            )
            row = connection.execute("SELECT * FROM workspaces WHERE path=?", (str(root),)).fetchone()
            assert row is not None
            return dict(row)

        return await self._run(add)

    async def workspaces(self) -> list[Record]:
        return await self._run(
            lambda connection: [dict(row) for row in connection.execute("SELECT * FROM workspaces ORDER BY rowid")]
        )

    async def workspace(self, identity: str) -> Record | None:
        def get(connection: sqlite3.Connection) -> Record | None:
            row = connection.execute("SELECT * FROM workspaces WHERE id=?", (identity,)).fetchone()
            return dict(row) if row else None

        return await self._run(get)

    async def close(self) -> None:
        def close() -> None:
            with self._lock:
                self._connection.close()

        await asyncio.to_thread(close)

    @staticmethod
    def _memory(row: sqlite3.Row) -> Record:
        memory = dict(row)
        memory["keywords"] = json.loads(memory.pop("keywords_json"))
        memory["origin"] = json.loads(memory.pop("origin_json"))
        memory["deleted"] = bool(memory["deleted"])
        return memory

    async def add_memory(self, workspace_id: str, title: str, content: str, kind: str,
                         keywords: list[str], source_note: str) -> Record:
        identity, timestamp = uuid4().hex, now()
        def save(connection: sqlite3.Connection) -> Record:
            connection.execute("INSERT INTO memory_versions VALUES (?, ?, 1, ?, ?, ?, ?, ?, 0, ?, ?)",
                (identity, workspace_id, title, content, kind, json.dumps(keywords, ensure_ascii=False),
                 json.dumps({"kind": "user", "note": source_note}, ensure_ascii=False), timestamp, timestamp))
            row = connection.execute("SELECT * FROM memory_versions WHERE id=? AND version=1", (identity,)).fetchone()
            assert row is not None
            return self._memory(row)
        return await self._run(save)

    async def memories(self, workspace_id: str) -> list[Record]:
        def get(connection: sqlite3.Connection) -> list[Record]:
            return [self._memory(row) for row in connection.execute(
                "SELECT * FROM memory_versions m WHERE workspace_id=? AND deleted=0 AND "
                "version=(SELECT MAX(version) FROM memory_versions WHERE id=m.id) ORDER BY created_at, id", (workspace_id,))]
        return await self._run(get)

    async def memory(self, workspace_id: str, identity: str) -> Record | None:
        def get(connection: sqlite3.Connection) -> Record | None:
            row = connection.execute("SELECT * FROM memory_versions WHERE workspace_id=? AND id=? ORDER BY version DESC LIMIT 1",
                                     (workspace_id, identity)).fetchone()
            return self._memory(row) if row is not None else None
        return await self._run(get)

    async def memory_versions(self, workspace_id: str, identity: str) -> list[Record]:
        return await self._run(lambda connection: [self._memory(row) for row in connection.execute(
            "SELECT * FROM memory_versions WHERE workspace_id=? AND id=? ORDER BY version", (workspace_id, identity))])

    async def revise_memory(self, workspace_id: str, identity: str, expected_version: int,
                            replacement: Record | None) -> Record | None:
        def revise(connection: sqlite3.Connection) -> Record | None:
            row = connection.execute("SELECT * FROM memory_versions WHERE workspace_id=? AND id=? ORDER BY version DESC LIMIT 1",
                                     (workspace_id, identity)).fetchone()
            if row is None:
                return None
            current = self._memory(row)
            if current["version"] != expected_version or current["deleted"]:
                raise ValueError("Memory changed or was deleted; reload its current version.")
            values = replacement or current
            origin = {"kind": "user", "note": values["source_note"]} if replacement else current["origin"]
            connection.execute("INSERT INTO memory_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (identity, workspace_id, expected_version + 1, values["title"], values["content"], values["kind"],
                 json.dumps(values["keywords"], ensure_ascii=False), json.dumps(origin, ensure_ascii=False),
                 int(replacement is None), current["created_at"], now()))
            saved = connection.execute("SELECT * FROM memory_versions WHERE id=? AND version=?",
                                       (identity, expected_version + 1)).fetchone()
            assert saved is not None
            return self._memory(saved)
        return await self._run(revise)

    async def add_session(self, workspace_id: str, title: str) -> Record:
        session = {"id": uuid4().hex, "workspace_id": workspace_id, "title": title, "created_at": now()}
        await self._run(lambda connection: connection.execute(
            "INSERT INTO sessions VALUES (:id, :workspace_id, :title, :created_at)", session,
        ))
        return session

    async def sessions(self) -> list[Record]:
        return await self._run(lambda connection: [dict(row) for row in connection.execute(
            "SELECT * FROM sessions ORDER BY rowid DESC"
        )])

    async def session(self, identity: str) -> Record | None:
        def get(connection: sqlite3.Connection) -> Record | None:
            row = connection.execute("SELECT * FROM sessions WHERE id=?", (identity,)).fetchone()
            return dict(row) if row else None

        return await self._run(get)

    @staticmethod
    def _event(connection: sqlite3.Connection, identity: str, kind: str, data: Record) -> None:
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0)+1 FROM events WHERE run_id=?", (identity,)
        ).fetchone()[0]
        connection.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?)", (
            identity, sequence, kind, json.dumps(data, ensure_ascii=False), now(),
        ))

    def _select_memories(self, connection: sqlite3.Connection, workspace_id: str, task: str,
                         sources: list[Record], limits: Record) -> tuple[list[Record], list[Record]]:
        selected: list[Record] = []
        omitted: list[Record] = []
        relevance = (task + " " + " ".join(source["path"] for source in sources)).casefold()
        for row in connection.execute(
            "SELECT * FROM memory_versions m WHERE workspace_id=? AND deleted=0 AND "
            "version=(SELECT MAX(version) FROM memory_versions WHERE id=m.id) ORDER BY created_at, id", (workspace_id,)):
            memory = self._memory(row)
            reason = None
            if memory["keywords"] and not any(word.casefold() in relevance for word in memory["keywords"]):
                reason = "not_relevant"
            elif len(json.dumps(selected + [memory], ensure_ascii=False).encode("utf-8")) > limits.get("max_memory_bytes", 1024):
                reason = "memory_budget"
            if reason:
                omitted.append({"memory_id": memory["id"], "version": memory["version"], "reason": reason})
            else:
                selected.append(memory)
        return selected, omitted

    async def current_memories(self, identity: str) -> tuple[list[Record], list[Record]]:
        def select(connection: sqlite3.Connection) -> tuple[list[Record], list[Record]]:
            run = connection.execute("SELECT runs.*, sessions.workspace_id FROM runs JOIN sessions ON sessions.id=runs.session_id WHERE runs.id=?", (identity,)).fetchone()
            assert run is not None
            return self._select_memories(connection, run["workspace_id"], run["task"], json.loads(run["sources_json"]), json.loads(run["limits_json"]))
        return await self._run(select)

    @staticmethod
    def _submission(connection: sqlite3.Connection, session_id: str, key: str, digest: str) -> str | None:
        row = connection.execute("SELECT * FROM submissions WHERE session_id=? AND key=?", (session_id, key)).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != digest:
            raise ValueError("This submission key already belongs to different request content.")
        return str(row["run_id"])

    async def submission(self, session_id: str, key: str, digest: str) -> Record | None:
        identity = await self._run(lambda connection: self._submission(connection, session_id, key, digest))
        return await self.run(identity) if identity is not None else None

    async def add_run(self, session_id: str, task: str, sources: list[Record], limits: Record, model: Record,
                      validation: Record | None = None, policy: Record | None = None,
                      base_reference: Record | None = None, source_excerpts: list[Record] | None = None,
                      submission_key: str | None = None, request_sha256: str = "",
                      resume_checkpoint: Record | None = None) -> tuple[Record, bool]:
        identity = uuid4().hex

        def add(connection: sqlite3.Connection) -> tuple[str, bool]:
            if submission_key is not None:
                previous = self._submission(connection, session_id, submission_key, request_sha256)
                if previous is not None:
                    return previous, False
            if resume_checkpoint is not None:
                parent = connection.execute("SELECT * FROM runs WHERE id=?", (resume_checkpoint["source_run_id"],)).fetchone()
                saved = self._checkpoint(connection, resume_checkpoint["source_run_id"])
                if parent is None or parent["session_id"] != session_id or parent["status"] != "interrupted":
                    raise RunFailure("invalid_resume", "Only an interrupted Run in this Session can be continued.")
                if saved is None or saved != resume_checkpoint:
                    raise RunFailure("checkpoint_changed", "Reload the current saved checkpoint before continuing.")
                if resume_blockers(saved):
                    raise RunFailure("checkpoint_blocked", "Unverified effects must be resolved before continuing.")
                for artifact in saved["artifacts"]:
                    row = connection.execute("SELECT artifacts.* FROM artifacts JOIN runs ON runs.id=artifacts.run_id WHERE artifacts.id=? AND runs.session_id=?",
                                             (artifact["id"], session_id)).fetchone()
                    try:
                        with (self.directory / "artifacts" / artifact["id"]).open("rb") as handle:
                            raw = handle.read(artifact["bytes"] + 1)
                        valid = row is not None and row["sha256"] == artifact["sha256"] and len(raw) == artifact["bytes"] and hashlib.sha256(raw).hexdigest() == artifact["sha256"]
                    except OSError:
                        valid = False
                    if not valid:
                        raise RunFailure("checkpoint_artifact_unavailable", "A preserved artifact cannot be verified.")
            active = connection.execute(
                "SELECT id FROM runs WHERE session_id=? AND status IN ('queued', 'running', 'waiting_approval', 'cancel_requested')", (session_id,)
            ).fetchone()
            if active:
                raise ValueError("This Session already has an active Run.")
            base = None
            context: Record = {"turns": [], "history": [], "history_run_ids": [], "omitted": []}
            session = connection.execute("SELECT workspace_id FROM sessions WHERE id=?", (session_id,)).fetchone()
            assert session is not None
            context["source_excerpts"] = source_excerpts or []
            context["memories"], context["memory_omitted"] = self._select_memories(connection, session[0], task, sources, limits)
            if base_reference is not None:
                parent_run = connection.execute("SELECT * FROM runs WHERE id=?", (base_reference["run_id"],)).fetchone()
                artifact = connection.execute("SELECT * FROM artifacts WHERE run_id=? AND id=?",
                                              (base_reference["run_id"], base_reference["artifact_id"])).fetchone()
                if parent_run is None or parent_run["session_id"] != session_id or parent_run["status"] != "completed" or artifact is None:
                    raise RunFailure("invalid_base_artifact", "Choose an artifact from a completed Run in this Session.")
                try:
                    raw = (self.directory / "artifacts" / artifact["id"]).read_bytes()
                    if hashlib.sha256(raw).hexdigest() != artifact["sha256"]:
                        raise ValueError("Hash mismatch")
                    base = {**self._artifact(artifact), "text": raw.decode("utf-8"),
                            "sources": json.loads(parent_run["sources_json"])}
                except (OSError, UnicodeError, ValueError) as error:
                    raise RunFailure("base_artifact_unavailable", "The exact saved base artifact cannot be verified.") from error
                admitted = {(source["path"], source["sha256"]) for source in sources}
                if all((source["path"], source["sha256"]) in admitted for source in base["sources"]):
                    prior = json.loads(parent_run["context_json"] or "{}")
                    events = [{**dict(row), "data": json.loads(row["data_json"])} for row in connection.execute(
                        "SELECT * FROM events WHERE run_id=? ORDER BY sequence", (parent_run["id"],))]
                    turns = prior.get("turns", []) + [{"run_id": parent_run["id"],
                        "messages": history_turn(parent_run["id"], parent_run["task"], events)}]
                    budget = limits.get("max_history_bytes", 16384)
                    while turns and len(json.dumps([message for turn in turns for message in turn["messages"]], ensure_ascii=False).encode("utf-8")) > budget:
                        removed = turns.pop(0)
                        context["omitted"].append({"run_id": removed["run_id"], "reason": "history_budget"})
                    context["turns"] = turns
                    context["history"] = [message for turn in turns for message in turn["messages"]]
                    context["history_run_ids"] = [turn["run_id"] for turn in turns]
                else:
                    context["omitted"] = [{"run_id": parent_run["id"], "reason": "sources_not_selected_at_same_version"}]
            connection.execute(
                "INSERT INTO runs (id, session_id, task, sources_json, limits_json, status, mode, created_at, model_json, validation_json, policy_json, base_json, context_json, resume_json) "
                "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)",
                (identity, session_id, task, json.dumps(sources), json.dumps(limits), model["mode"], now(), json.dumps(model), json.dumps(validation or {}), json.dumps(policy or {}), json.dumps(base), json.dumps(context), json.dumps(resume_checkpoint)),
            )
            if submission_key is not None:
                connection.execute("INSERT INTO submissions VALUES (?, ?, ?, ?)",
                                   (session_id, submission_key, request_sha256, identity))
            self._event(connection, identity, "run.accepted", {"mode": model["mode"], "model": model, "validation": validation or {}, "policy": policy or {}})
            return identity, True

        accepted_id, created = await self._run(add)
        result = await self.run(accepted_id)
        assert result is not None
        return result, created

    @staticmethod
    def _artifact(row: sqlite3.Row) -> Record:
        result = dict(row)
        result["download_url"] = f"/api/runs/{row['run_id']}/artifacts/{row['id']}"
        return result

    async def run(self, identity: str) -> Record | None:
        def get(connection: sqlite3.Connection) -> Record | None:
            row = connection.execute("SELECT * FROM runs WHERE id=?", (identity,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["sources"] = json.loads(result.pop("sources_json"))
            result["limits"] = json.loads(result.pop("limits_json"))
            result["model"] = json.loads(result.pop("model_json"))
            result["validation"] = json.loads(result.pop("validation_json"))
            result["policy"] = json.loads(result.pop("policy_json"))
            result["context"] = json.loads(result.pop("context_json") or "{}")
            result["base_artifact"] = json.loads(result.pop("base_json") or "null")
            result["resume_checkpoint"] = json.loads(result.pop("resume_json") or "null")
            result["recovery"] = json.loads(result.pop("recovery_json") or "null")
            result["cancellation"] = json.loads(result.pop("cancellation_json") or "null")
            result["error"] = json.loads(result.pop("error_json") or "null")
            result["usage"] = {"model_calls": result.pop("model_calls"), "tool_calls": result.pop("tool_calls")}
            result["token_usage"] = add_token_usage(json.loads(result.pop("token_usage_json") or "{}"), None, count=False)
            if result["model"].get("price_per_mtok"):
                result["estimated_cost"] = estimated_cost(result["token_usage"], result["model"]["price_per_mtok"])
            result["artifacts"] = [self._artifact(artifact) for artifact in connection.execute(
                "SELECT * FROM artifacts WHERE run_id=? ORDER BY rowid", (identity,)
            )]
            result["inherited_artifacts"] = deepcopy(result["resume_checkpoint"]["artifacts"]) if result["resume_checkpoint"] else []
            result["approvals"] = [self._approval(approval) for approval in connection.execute(
                "SELECT * FROM approvals WHERE run_id=? ORDER BY rowid", (identity,)
            )]
            result["last_sequence"] = connection.execute("SELECT COALESCE(MAX(sequence), 0) FROM events WHERE run_id=?", (identity,)).fetchone()[0]
            return result

        return await self._run(get)

    async def session_runs(self, session_id: str) -> list[Record]:
        identities = await self._run(lambda connection: [row[0] for row in connection.execute(
            "SELECT id FROM runs WHERE session_id=? ORDER BY rowid", (session_id,)
        )])
        result = []
        for identity in identities:
            run = await self.run(identity)
            if run is not None:
                result.append(run)
        return result

    async def record(
        self, identity: str, kind: str, data: Record, *, status: str | None = None,
        usage: Record | None = None, result: str | None = None, error: Record | None = None,
    ) -> None:
        def save(connection: sqlite3.Connection) -> None:
            current = connection.execute("SELECT status FROM runs WHERE id=?", (identity,)).fetchone()
            if current is None or current[0] in TERMINAL:
                return
            if current[0] == "cancel_requested":
                return
            if status is not None:
                connection.execute("UPDATE runs SET status=? WHERE id=?", (status, identity))
            if usage is not None:
                connection.execute("UPDATE runs SET model_calls=?, tool_calls=? WHERE id=?", (
                    usage["model_calls"], usage["tool_calls"], identity,
                ))
            if kind == "model.responded":
                # Totalled with the event, in one transaction, so the totals always match the saved replies.
                saved = connection.execute("SELECT token_usage_json FROM runs WHERE id=?", (identity,)).fetchone()[0]
                connection.execute("UPDATE runs SET token_usage_json=? WHERE id=?", (
                    json.dumps(add_token_usage(json.loads(saved or "{}"), data.get("usage"))), identity,
                ))
            if result is not None:
                connection.execute("UPDATE runs SET result=? WHERE id=?", (result, identity))
            if error is not None:
                connection.execute("UPDATE runs SET error_json=? WHERE id=?", (json.dumps(error), identity))
            if status in TERMINAL:
                for approval in connection.execute("SELECT * FROM approvals WHERE run_id=? AND status IN ('pending','approved')", (identity,)).fetchall():
                    connection.execute("UPDATE approvals SET status='expired' WHERE id=?", (approval["id"],))
                    self._event(connection, identity, "approval.expired", {"id": approval["id"], "reason": status})
                self._close_messages(connection, identity, status)
            self._event(connection, identity, kind, data)

        await self._run(save)

    async def request_cancel(self, identity: str) -> None:
        def save(connection: sqlite3.Connection) -> None:
            run = connection.execute("SELECT status FROM runs WHERE id=?", (identity,)).fetchone()
            if run is None or run[0] in TERMINAL or run[0] == "cancel_requested":
                return
            connection.execute("UPDATE runs SET status='cancel_requested', cancellation_json=? WHERE id=?", (json.dumps({"outcome": "pending"}), identity))
            for approval in connection.execute("SELECT id FROM approvals WHERE run_id=? AND status IN ('pending','approved')", (identity,)).fetchall():
                connection.execute("UPDATE approvals SET status='cancelled' WHERE id=?", (approval["id"],))
                self._event(connection, identity, "approval.cancelled", {"id": approval["id"]})
            self._event(connection, identity, "run.cancel_requested", {})
        await self._run(save)

    async def complete_cancel(self, identity: str, *, background_work_may_finish: bool = False) -> None:
        def save(connection: sqlite3.Connection) -> None:
            run = connection.execute("SELECT status FROM runs WHERE id=?", (identity,)).fetchone()
            if run is None or run[0] != "cancel_requested":
                return
            count = connection.execute("SELECT COUNT(*) FROM artifacts WHERE run_id=?", (identity,)).fetchone()[0]
            uncertain: dict[str, Record] = {}
            for row in connection.execute("SELECT kind, data_json FROM events WHERE run_id=? AND kind IN ('tool.dispatched','tool.completed','tool.failed') ORDER BY sequence", (identity,)):
                data = json.loads(row["data_json"])
                if row["kind"] == "tool.dispatched" and data.get("effect") not in {"read_only", "run_metadata"}:
                    uncertain[data["call_id"]] = {"call_id": data["call_id"], "tool": data["tool"]}
                elif row["kind"] in {"tool.completed", "tool.failed"}:
                    uncertain.pop(data["call_id"], None)
            result = {"outcome": "unknown_effects" if uncertain else "stopped", "completed_artifacts": count, "runner_stopped": True,
                      "background_work_may_finish": background_work_may_finish, "uncertain_attempts": list(uncertain.values())}
            for attempt in uncertain.values():
                self._event(connection, identity, "tool.unknown", {**attempt, "reason": "cancelled_before_result_acknowledgement"})
            connection.execute("UPDATE runs SET status='cancelled', cancellation_json=? WHERE id=?", (json.dumps(result), identity))
            self._close_messages(connection, identity, "cancelled")
            self._event(connection, identity, "run.cancelled", result)
        await self._run(save)

    @staticmethod
    def _messages(connection: sqlite3.Connection, identity: str) -> tuple[list[Record], set[str]]:
        """Messages admitted to a Run, and the IDs already delivered or reported undelivered."""
        admitted: list[Record] = []
        settled: set[str] = set()
        for row in connection.execute(
                "SELECT kind, data_json FROM events WHERE run_id=? AND kind IN "
                "('message.queued','message.delivered','message.undelivered') ORDER BY sequence", (identity,)):
            data = json.loads(row["data_json"])
            if row["kind"] == "message.queued":
                admitted.append(data)
            elif row["kind"] == "message.delivered":
                settled.add(data["message_id"])
            else:
                settled.update(data["message_ids"])
        return admitted, settled

    def _close_messages(self, connection: sqlite3.Connection, identity: str, reason: str) -> None:
        admitted, settled = self._messages(connection, identity)
        waiting = [message["message_id"] for message in admitted if message["message_id"] not in settled]
        if waiting:
            self._event(connection, identity, "message.undelivered", {"message_ids": waiting, "reason": reason})

    async def add_message(self, identity: str, message_id: str, text: str) -> Record:
        """Queue a user message for the Run's next decision; re-sending the same ID is idempotent."""
        def save(connection: sqlite3.Connection) -> Record:
            run = connection.execute("SELECT status FROM runs WHERE id=?", (identity,)).fetchone()
            if run is None:
                raise KeyError(identity)
            admitted, settled = self._messages(connection, identity)
            existing = next((message for message in admitted if message["message_id"] == message_id), None)
            if existing is not None:
                if existing["text"] != text:
                    raise ValueError("This message ID was already used for different text.")
                return {"message_id": message_id, "status": "settled" if message_id in settled else "queued"}
            if run["status"] not in ACCEPTS_MESSAGES:
                raise ValueError("This Run no longer accepts messages.")
            if len(admitted) >= MAX_RUN_MESSAGES:
                raise ValueError(f"A Run accepts at most {MAX_RUN_MESSAGES} added messages.")
            self._event(connection, identity, "message.queued", {"message_id": message_id, "text": text})
            return {"message_id": message_id, "status": "queued"}

        return await self._run(save)

    async def queued_messages(self, identity: str) -> list[Record]:
        def get(connection: sqlite3.Connection) -> list[Record]:
            admitted, settled = self._messages(connection, identity)
            return [message for message in admitted if message["message_id"] not in settled]

        return await self._run(get)

    async def events(self, identity: str, after: int = 0, *, limit: int | None = None) -> list[Record]:
        def get(connection: sqlite3.Connection) -> list[Record]:
            result = []
            for row in connection.execute(
                "SELECT * FROM events WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT ?", (identity, after, limit if limit is not None else -1)
            ):
                event = dict(row)
                event["data"] = json.loads(event.pop("data_json"))
                result.append(event)
            return result

        return await self._run(get)

    async def event_position(self, identity: str) -> Record | None:
        def get(connection: sqlite3.Connection) -> Record | None:
            row = connection.execute("SELECT status, (SELECT COALESCE(MAX(sequence), 0) FROM events WHERE run_id=runs.id) AS last_sequence FROM runs WHERE id=?", (identity,)).fetchone()
            return dict(row) if row is not None else None
        return await self._run(get)

    @staticmethod
    def _approval(row: sqlite3.Row) -> Record:
        value = dict(row)
        value["arguments"] = json.loads(value.pop("arguments_json"))
        value["scope"] = json.loads(value.pop("scope_json"))
        return value

    async def request_approval(self, identity: str, prepared: PreparedCall) -> Record:
        def save(connection: sqlite3.Connection) -> Record:
            run = connection.execute("SELECT * FROM runs WHERE id=?", (identity,)).fetchone()
            if run is None or run["status"] != "running":
                raise RunFailure("run_not_active", "Only an active Run can request approval.")
            limits = json.loads(run["limits_json"])
            created = datetime.now(timezone.utc)
            expires = min(created + timedelta(seconds=limits.get("approval_timeout_seconds", 120)),
                          datetime.fromisoformat(run["created_at"]) + timedelta(seconds=limits["timeout_seconds"]))
            approval_id = uuid4().hex
            connection.execute("INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)", (
                approval_id, identity, prepared.call.identity, prepared.call.name,
                json.dumps(prepared.arguments), json.dumps(prepared.scope), prepared.digest(identity), created.isoformat(), expires.isoformat(),
            ))
            connection.execute("UPDATE runs SET status='waiting_approval' WHERE id=?", (identity,))
            row = connection.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            assert row is not None
            approval = self._approval(row)
            self._event(connection, identity, "approval.requested", approval)
            return approval
        return await self._run(save)

    async def approval(self, identity: str, approval_id: str) -> Record | None:
        def get(connection: sqlite3.Connection) -> Record | None:
            row = connection.execute("SELECT * FROM approvals WHERE run_id=? AND id=?", (identity, approval_id)).fetchone()
            return self._approval(row) if row else None
        return await self._run(get)

    async def decide_approval(self, identity: str, approval_id: str, decision: str, digest: str) -> Record | None:
        def save(connection: sqlite3.Connection) -> Record | None:
            row = connection.execute("SELECT a.*, r.status AS run_status FROM approvals a JOIN runs r ON r.id=a.run_id WHERE a.run_id=? AND a.id=?", (identity, approval_id)).fetchone()
            if row is None:
                return None
            if row["status"] != "pending" or row["run_status"] != "waiting_approval" or datetime.now(timezone.utc) >= datetime.fromisoformat(row["expires_at"]):
                raise ValueError("This approval is no longer pending or has expired.")
            if digest != row["parameters_sha256"]:
                raise ValueError("The approval does not match the reviewed parameters.")
            status = "approved" if decision == "approve" else "denied"
            connection.execute("UPDATE approvals SET status=? WHERE id=?", (status, approval_id))
            connection.execute("UPDATE runs SET status='running' WHERE id=?", (identity,))
            self._event(connection, identity, f"approval.{status}", {"id": approval_id, "parameters_sha256": digest})
            updated = connection.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            assert updated is not None
            return self._approval(updated)
        return await self._run(save)

    async def dispatch(self, identity: str, prepared: PreparedCall, approval_id: str | None = None) -> PreparedCall:
        def save(connection: sqlite3.Connection) -> PreparedCall:
            run = connection.execute("SELECT * FROM runs WHERE id=?", (identity,)).fetchone()
            if run is None or run["status"] != "running":
                raise RunFailure("run_not_active", "The Run is not allowed to dispatch tools.")
            policy = json.loads(run["policy_json"])
            arguments, scope = prepared.arguments, prepared.scope
            if (prepared.scope.get("approval") == "required"
                    or (prepared.call.name == "artifact_write" and policy.get("require_publish_approval"))):
                row = connection.execute("SELECT * FROM approvals WHERE run_id=? AND id=?", (identity, approval_id)).fetchone()
                if row is None or row["status"] != "approved" or row["call_id"] != prepared.call.identity or row["tool"] != prepared.call.name or row["parameters_sha256"] != prepared.digest(identity):
                    raise RunFailure("approval_required", "A matching single-use approval is required.")
                if datetime.now(timezone.utc) >= datetime.fromisoformat(row["expires_at"]):
                    raise RunFailure("approval_expired", "The reviewed operation expired before dispatch.")
                arguments, scope = json.loads(row["arguments_json"]), json.loads(row["scope_json"])
                connection.execute("UPDATE approvals SET status='consumed' WHERE id=?", (approval_id,))
                self._event(connection, identity, "approval.consumed", {"id": approval_id, "call_id": prepared.call.identity})
            self._event(connection, identity, "tool.dispatched", {"call_id": prepared.call.identity, "tool": prepared.call.name,
                         "effect": scope["effect"], "arguments": arguments, "scope": scope,
                         "parameters_sha256": prepared.digest(identity)})
            return PreparedCall(prepared.call, deepcopy(arguments), deepcopy(scope))
        return await self._run(save)

    async def publish(self, identity: str, name: str, content: str, *, call_id: str) -> Record:
        artifact_id = _artifact_identity(identity, call_id)
        raw = content.encode("utf-8")
        directory = self.directory / "artifacts"

        def save(connection: sqlite3.Connection) -> Record:
            run = connection.execute("SELECT status, base_json FROM runs WHERE id=?", (identity,)).fetchone()
            if run is None or run[0] != "running":
                raise RunFailure("run_not_active", "Artifacts can only be published by an active Run.")
            existing = connection.execute("SELECT * FROM artifacts WHERE run_id=? AND call_id=?", (identity, call_id)).fetchone()
            if existing is not None:
                try:
                    matches = (existing["name"] == name and existing["sha256"] == hashlib.sha256(raw).hexdigest()
                               and (directory / existing["id"]).read_bytes() == raw)
                except OSError:
                    matches = False
                if not matches:
                    raise RunFailure("artifact_identity_conflict", "This operation already has different or unavailable artifact bytes.")
                return self._artifact(existing)
            directory.mkdir(exist_ok=True)
            path = directory / artifact_id
            created = False
            try:
                if path.exists():
                    if path.read_bytes() != raw:
                        raise RunFailure("artifact_identity_conflict", "The operation's existing artifact cannot be verified.")
                else:
                    with path.open("xb") as output:
                        created = True
                        output.write(raw)
                        output.flush()
                        os.fsync(output.fileno())
                base = json.loads(run["base_json"] or "null")
                connection.execute("INSERT INTO artifacts (id, run_id, name, sha256, bytes, parent_artifact_id, version, call_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
                    artifact_id, identity, name, hashlib.sha256(raw).hexdigest(), len(raw),
                    base["id"] if base else None, base["version"] + 1 if base else 1, call_id,
                ))
                row = connection.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
                assert row is not None
                artifact = self._artifact(row)
                self._event(connection, identity, "artifact.published", artifact)
                return artifact
            except Exception:
                if created:
                    path.unlink(missing_ok=True)
                raise

        return await self._run(save)

    async def session_artifact(self, session_id: str, artifact_id: str) -> Record | None:
        """An artifact published by any Run of the given Session."""
        def get(connection: sqlite3.Connection) -> Record | None:
            row = connection.execute(
                "SELECT a.* FROM artifacts a JOIN runs r ON r.id=a.run_id WHERE r.session_id=? AND a.id=?",
                (session_id, artifact_id)).fetchone()
            return self._artifact(row) if row else None

        return await self._run(get)

    async def artifact(self, run_id: str, artifact_id: str) -> Record | None:
        def get(connection: sqlite3.Connection) -> Record | None:
            row = connection.execute("SELECT * FROM artifacts WHERE run_id=? AND id=?", (run_id, artifact_id)).fetchone()
            return self._artifact(row) if row else None

        return await self._run(get)
