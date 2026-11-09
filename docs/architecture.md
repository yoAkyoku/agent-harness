# Architecture

[繁體中文](architecture.zh-TW.md)

One runtime serves every task. Data analysis and document indexing differ only in the sources selected and in the model's own tool choices; nothing in the runtime selects a fixed runner or answer from the task text. The model adapter only translates protocols, the context builder selects bounded background, tools validate and execute capabilities, and the store persists state and events. The scripted fixture replaces only the model's transport.

```mermaid
flowchart TD
  UI[Workbench] -->|HTTP task / submission key| API[FastAPI interface]
  API -->|Run saved in a transaction| Store[(SQLite state and events)]
  API --> Runtime[Bounded asyncio runtime]
  Runtime --> Context[Context / relevant memory / paired history]
  Context --> Adapter[Model adapter]
  Adapter --> Model[Ollama / OpenAI-compatible / Claude]
  Model -->|text and complete tool requests| Runtime
  Runtime --> Guard[Schema / source scope / evidence / single-use approval]
  Guard -->|dispatch intent saved first| Store
  Guard --> Executor[Restricted files / tables / ratios / immutable artifacts]
  Executor -->|actual result saved first| Store
  Runtime --> Verifiers[Evidence-only verifiers]
  Store -->|sequence / cursor / ACK| WS[WebSocket observer]
  WS --> UI
```

## From a task to the next decision

`web.py` accepts a task with its sources and limits and saves the Run before the runtime starts. Each Session has at most one active Run; a semaphore bounds concurrency across Sessions, and blocking file and SQLite work runs in threads. A dropped WebSocket does not stop the work.

Until a plan is saved the model is offered only `plan_update`. Afterwards the catalogue depends only on Run state — the selected source types, whether there is a base artifact or a staged draft, whether something was published, whether a correction is pending — never on the task text. The catalogue is enforced: after the executor's own checks (schema, scope, evidence, review and completion gates, each with its specific error), a call to a tool the request did not offer is refused before dispatch with a correctable `tool_not_offered`. The JSON-tools protocol already rejects such names while decoding. Each `model.requested` event records the offered tool names and a SHA-256 of the exact request body.

Each step assembles the real model request and checks it against a conservative context bound before sending it. Complete history turns, source excerpts and relevant memory versions are selected by rule. Over budget, the runtime applies these moves in order, re-measuring after each one and recording it as a `context.pruned` event with its reason:

1. Drop the oldest complete history turns from earlier Runs, then the optional source excerpts.
2. Drop the model's narration next to successful tool groups (call arguments and results stay; the narration remains in events and the UI).
3. Remove tool errors that a later call already corrected or superseded.
4. Move tool groups the model has already read into an exact evidence snapshot that keeps every evidence ID and result field; identical shared source fields are stored once and restored field by field. The newest unread group, error groups and publication groups are protected.
5. Shorten the prompt-side catalogue: tool descriptions, then schema bounds (lengths, item counts, patterns), runtime guidance, inspection hints and artifact-name descriptions. The executor still enforces the full schemas. Tool names, parameter names and types, and system rules are never changed.
6. Re-encode without loss: drafts and repeated plans by reference (review and receipt modes), and, after publication, the labelled-line view already contained in a source's text and the whitespace in tool-result JSON.
7. If the request still does not fit, stop with `context_limit_exceeded`.

Steps 1, 2 and 5 lose information and are recorded as such; the context as a whole is not claimed to be lossless.

The adapter normalises text and tool requests and never executes a streamed call whose arguments are incomplete. `Tools.prepare` validates the schema, workspace scope and evidence references and produces the effective arguments. When approval is required, the exact pending operation and its digest are stored and the run waits; an approval applies once, to that Run, call and argument set. The dispatch intent is persisted before the tool runs. The real result goes back to the model, which chooses the next step. Correctable errors carry the remaining budget and `correctable_fields`; operations with unknown effects are not retried.

Artifacts are stored with an immutable identity, SHA-256 and exact bytes. A revision pins its completed base and adds a version instead of overwriting. Any two versions in a Session can be compared as a unified diff (by default a version against its parent); both are re-hashed before comparing, and only the first two diff lines are file headers, so a removed `---` rule still counts as a removed line.

## Verifiers

Before publication the runtime runs the verifiers in `verifiers.py`. **They receive only the candidate content and the server-held tool evidence, never the task text**, so they cannot encode answers for particular tasks. They reject only statements the tool results do not support:

- **Calculation citations.** A computed value cited to evidence that did not produce it is rejected. When evidence spans several sources, a computed value must cite its calculation on the same line; with a single source the server's evidence appendix already links every value.
- **Consistent source roles.** Each artifact value that matches a labelled source value takes that source label as its role. One output field holding values of two different roles (for example an escalation contact filled in as the owner) is rejected, with feedback that says which document to change under each reading of the field. Parenthetical role annotations in labels do not create a separate field; a value whose own text states its role is exempt. With only one document there is nothing to compare, so the check stays silent.

The completion reply is checked as well. After publication the finalization request includes the published body (without the server appendix, up to 6000 bytes; larger bodies are omitted with an event) so the model describes the artifact rather than restating the task. `completion_summary` sees only the reply and the published artifacts: a clause naming an item the artifact marks missing everywhere must say it is missing, and numbers in the reply must appear in the artifact (single-digit counts exempt). The same check applies however the model ends: once something is published, every real model is offered `task_finish` (remote providers included), and a plain-text ending after publication is checked too. An unsupported one is dropped from the next request and becomes a correctable step that offers only `task_finish`, with the rejection reason in the system message.

Every failure returns to the model as a correctable error; the model decides how to fix it.

## Providers

The runtime depends on a small protocol (`DecisionModel` in `model.py`): `request_body`, `context_budget`, `decide`, `settings` and `info`.

| Provider | Adapter | Notes |
|---|---|---|
| Fixture | `ModelAdapter` + `FixtureTransport` | Deterministic, clearly labelled; proves control flow only. |
| Ollama | `ModelAdapter` | Verifies the installed model digest and renderer; exact context accounting for Qwen3.5; optional JSON-tools protocol for small local models. |
| OpenAI-compatible | `ModelAdapter` | Any Chat Completions endpoint with native tool calling. HTTPS only (plain HTTP only for loopback servers); the key is read from a named environment variable. |
| Claude | `AnthropicAdapter` | Official `anthropic` SDK, Messages API. Translates the runtime's Chat Completions history into Messages turns (top-level system, `tool_use` / `tool_result`, alternating roles, `is_error` for failed tools) and back. |

Remote context budgets use a labelled UTF-8 byte upper bound, because those tokenizers are not verified locally.

Remote providers may list alternative models (`--alt-model`), and Claude exposes effort levels; `info.choices` publishes both. A Run's admission checks its `model` and `effort` against those choices and records them in the Run's model record. The loop then works with `with_settings(...)`, a variant of the startup adapter that shares its client and changes only the model name and effort. A resume accepts any configured model name from the checkpoint and keeps it. Local Ollama models do not switch per Run, because each one needs its own digest and tokenizer verification.

Current Claude models always think and bind each thinking block to the exact conversation prefix that produced it, while this harness deliberately rewrites history (pruning, executor-state snapshots, publication finalization). Replaying a block after such an edit is rejected, so the Claude adapter never replays thinking blocks: each turn is answered without the model's previous private reasoning. This is a documented trade-off, not an oversight.

Token usage comes from each reply: Chat Completions providers report `prompt_tokens` and `completion_tokens` (streams ask for them with `include_usage`), and the Claude adapter maps `input_tokens` and `output_tokens` to the same names. `model.responded` saves the reply's usage, and in the same transaction the store adds it to the Run's `token_usage` (input, output, and how many replies did or did not report usage), so the totals always match the saved events. Prices come only from `--price MODEL=INPUT,OUTPUT` for configured models; admission pins the chosen model's price into the Run's model record (a resume uses the current price, since its requests are new spending), and `estimated_cost` is computed from the totals, marked partial when some replies reported no usage.

## Adding a message to a running Run

`POST /api/runs/{id}/messages` with a client-chosen `message_id` queues a message while the Run can still decide (queued, running or waiting for approval). Re-sending the same ID and text returns the original admission; the same ID with other text is a conflict. At the start of each step the runtime delivers queued messages as user turns after the latest tool results, so the request already being generated is never changed. Steps that offer a single recovery tool (publication, completion or finalization) keep messages queued, because the model could not act on them. Every admission and delivery is an event (`message.queued`, `message.delivered`); `history_turn` replays delivered messages, so revisions and checkpoints see them. Messages still queued when the Run ends are reported in one `message.undelivered` event written before the terminal event.

## Local access

The app answers only to loopback host names (DNS-rebinding defence) and refuses foreign `Origin` headers. The command-line entry point also requires a per-launch access token: a browser presents `/?token=…` once and receives an HttpOnly, SameSite=Strict cookie named after the token, so workbenches on other ports never share it; scripts send `Authorization: Bearer <token>`. Every response refuses framing and sends no referrer.

## MCP tools and permission rules

`mcp_hub.py` starts the stdio servers named in the `--mcp-config` file when the app starts and closes them with it. A server that fails to start is reported in `/api/config` and does not stop the workbench. A tool is offered only if the user lists it in `allowedTools`; server annotations can only narrow that list (a tool that declares `read_only_hint: false` or `destructive_hint: true` is excluded), never widen it. Third-party descriptions are capped at 400 characters and introduced as untrusted, and input schemas must be valid JSON Schema objects under 4 KiB. A Run selects tools at admission like its sources; the selection is part of the Run's policy, so checkpoints and resumes keep it. A call is validated against the server's schema, dispatched as a `read_only` effect, limited to 30 s and 64 KiB of text, and its result becomes evidence (`mcp/<server>/<tool>`, SHA-256, text) that artifacts cite and verifiers check. Error results return to the model as correctable errors that a corrected retry of the same tool resolves. Servers get the MCP SDK's minimal inherited environment plus their configured `env`. Remote servers and OAuth connectors are not supported yet.

Permission rules are deny → ask → allow per tool: unlisted tools are never offered; `askTools` entries may have side effects, so their prepared scope carries `approval: required` and `effect: external_effect`. That scope is part of the digest a single-use approval binds to, and the store refuses to dispatch such a call without a matching, unexpired approval, so a denied, expired or cancelled approval means it never runs. An approved call whose outcome is lost in a crash is reported as an unknown effect and blocks automatic resume, like a publication; its errors are not retried automatically.

Plan approval (`require_plan_approval`) reuses the same mechanism. `Tools` remembers the last saved plan; with the policy on, a `plan_update` whose steps differ from it carries `approval: required` in its scope, so the store will not dispatch it without an approval of those exact steps. Until a plan is saved, only `plan_update` is offered, so nothing else can run. Denying a plan does not fail the Run: the runtime returns `plan_changes_requested` as a correction that a later `plan_update` resolves, outside the correction budget because it is the user's decision, and any note the user added is delivered at the next decision like other added messages. Resubmitting the approved plan needs no new approval, and a resume restores the approved plan from the checkpoint.

## Skills

`skills.py` reads `--skills-dir` once at startup. A skill is valid when its `SKILL.md` (at most 32 KiB, UTF-8) has frontmatter with a kebab-case `name` equal to its folder and a `description` of at most 1,024 characters; anything else is excluded with a reason. A Run selects skills at admission (stored in its policy, so resumes keep them). Its system message then lists one line per selected skill, and `skill_load` returns the instructions with the SHA-256 of the `SKILL.md` it read, so the context holds full instructions only for skills the model chose to load. Skill instructions guide how to work; they are not sources and cannot be cited. Scripts in skill folders are ignored because the harness has no shell.

## Project instructions

`instructions.py` reads the workspace's root `AGENTS.md` only for a Run that opts in. The file must be a regular file (a link is refused), UTF-8 and at most 8 KiB; an empty file counts as none. Admission pins the text, size and SHA-256 into the Run's policy, so the checkpoint carries them and a resume uses the same text even if the file has changed. The runtime appends the text to the system message under a header that names the file and its digest and places it below the harness rules: it may shape method, format and wording, but tools, sources, approvals and verifiers do not read it. `context.selected` records the path, size and digest.

## Headless clients

`agent_harness_demo.headless` is an ordinary observer: it creates the workspace, Session and Run through the HTTP API, then reads the durable stream with `flow=ack`, acknowledging each event and reconnecting from its last cursor (events at or below it are skipped). It prints events unchanged as NDJSON and maps the terminal status to an exit code. Because observers never own execution, a client that exits leaves the Run running. The reconnect path is not covered by a test yet.

## Execution and observation are separate

Store events are the authoritative sequence and are persisted before the UI is notified. Observers de-duplicate with a contiguous cursor and acknowledge to bound slow clients; after a terminal event they still read the persisted sequence to the end. Re-submitting the same key and content returns the original Run.

A cancel request, an executor that has stopped, and whether an effect is known are separate facts. Terminal states cannot be reopened by late results; a dispatched tool may be left `unknown`. Context and memory never grant permissions; tool scope and approvals are always enforced by the executor.

## Restart path

`StoreLease` takes an OS-level exclusive lock before SQLite is opened; a dead process releases the kernel lock, so a leftover lock file does not mean the service is alive. A new service finds Runs that lost their executor: dispatches without a result become `unknown`, and local artifacts are reconciled only when the call identity and exact bytes match. Checkpoints keep confirmed results, pending work, paired model messages and artifact references.

Resume takes only a digest and key. The server loads the checkpoint, validates the Session, model and source versions, artifacts and current resume blockers, then creates a new Run. Old approvals are invalid; confirmed operations are answered from saved results, and new effects that need approval are confirmed again. Old checkpoint payloads are never rewritten; admission diagnostics are recomputed after upgrades so incomplete legacy intent cannot bypass checks.

## Reading order

- `contracts.py` — limits, tool requests and prepared arguments, `PendingCorrection`, failure categories.
- `web.py` → `runtime.py` — public interface, lifecycle, bounded decision loop and stop conditions.
- `model.py`, `anthropic_adapter.py`, `streaming.py` — provider protocols, complete requests, stream normalisation.
- `tools.py`, `sources.py` — schemas, admitted sources, tools and evidence IDs.
- `verifiers.py` — task-independent checks before publication and completion.
- `context.py` — paired history, budgeting and lossless moves of confirmed groups.
- `storage.py`, `ownership.py`, `checkpoints.py` — transactions, immutable artifacts, OS ownership, `unknown` effects and resume.
- `event_stream.py`, `static/run-stream.js` — persisted sequence, ACK, catch-up and de-duplication.

## Testing seams

Tests drive the public HTTP/WebSocket interface and replace only the model's HTTP transport (an `httpx` transport for Chat Completions providers, an `httpx2` transport for the Anthropic SDK). Controlled faults use the same start-up switches as the validation scripts (`fault_once_source_read`, `hold_first_tool`), and the test client's host name is allowed in `tests/conftest.py` only. They use real temporary SQLite stores and files. Real subprocesses verify crash recovery; vendored source snapshots of earlier releases verify store and checkpoint upgrades; recorded real-model runs verify context handling. Browser walkthroughs and real-model quality runs are separate evidence (see [evaluation](evaluation.md)).
