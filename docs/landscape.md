# How this harness compares with production agent products

[繁體中文](landscape.zh-TW.md)

This note compares the harness with agent products whose operation flows are publicly documented: OpenAI Codex, Claude Code (and the Claude Agent SDK / Managed Agents), opencode, DeepSeek Harness (DSH), and agentic browsers. It was checked against first-party documentation and source on 2026-11-08; product details change quickly, so the [sources](#sources) list exactly what was read.

The point is not feature parity. Those products are coding agents or browsers with shells, file edits and network access. This harness is deliberately narrower: it answers questions **from selected local sources** and publishes **cited artifacts**. The comparison asks which of their control and safety mechanisms apply to that shape, which ones the harness already has, and which are worth adding.

## Operation flow

| Stage | Codex | Claude Code | opencode | DSH | Agentic browsers | This harness |
|---|---|---|---|---|---|---|
| Give a task | Thread → turn; IDE, CLI, app, SDK and an App Server protocol | Interactive session, headless `-p` with stream-JSON, Agent SDK | TUI, `opencode run --format json`, shared local server, ACP | Web, headless, SDK and ACP profiles over one plugin tree | Side panel or task tab, often with a "plan first" step | HTTP API, workbench or headless NDJSON client: task + selected sources; idempotent submission key |
| Show the plan | Plan items in the turn stream | Plan mode (read-only until approved) | `plan` agent (edits denied except plan files) | Plan mode is "soft guidance"; `exit_plan_mode` shows a plan | Plan approval defines the scope of later actions | A plan must be saved before any other tool is offered; the plan is public in the event stream. Optionally (`require_plan_approval`) nothing runs until the user approves the plan, and a revised plan needs approval again |
| Approve risky actions | Typed approval requests: accept, accept for session, decline, cancel | Permission modes; deny → ask → allow rules; hooks | `ask` replies once / always / reject | Fail-closed outcomes: allowed-once, rejected, cancelled, unavailable | Per-site / per-action-class grants; take over for sensitive steps | Single-use approval bound to a parameter digest, for publication (optional) and for every call of an `askTools` MCP tool; unlisted tools are denied |
| Steer or interrupt | Steer, queue, interrupt mid-turn | Interrupt; queue follow-ups | Enter steers, queue, Esc interrupts; idempotent prompt IDs | Inbox `steer` / `followup` / `cancel(keepInbox)` | Pause, take over, resume | Add a message while the Run works (idempotent by message ID; delivered at the next decision); cancel |
| Review and undo | Turn diffs; thread revert vs file revert | Checkpoints and rewind | Git snapshots per step, staged `/undo` | Review view; no file undo documented | Drafts instead of commits (e.g. compose but don't send) | Immutable, hashed artifacts with version diffs; optional draft → publish review mode; revisions are new Runs on a base artifact |
| Finish | Turn completed item | Result message with subtypes (e.g. max turns) | `run` waits for background jobs | `turn/end` with abort cause | Summary of actions taken | A `task_finish` summary (where offered) is checked against the published artifact; a Run cannot complete with an unresolved tool error or unpublished draft; terminal states cannot be reopened; reported token usage, and a cost estimate when prices are configured |
| Resume after a crash | Persisted threads | Resume sessions | SQLite sessions; live-only event bus | Append-only JSONL; open turn kept, repaired under write ownership | Usually none | Intent persisted before dispatch; explicit resume reuses confirmed results and never re-runs unknown effects |

## Mechanisms

| Mechanism | What the products do | This harness |
|---|---|---|
| Event log and streaming | Codex App Server streams thread/turn/item events; DSH separates live chunks from durable settlements and repairs gaps on reconnect; opencode's client bus is live-only | Events are written to SQLite before they are pushed; WebSocket resumes from a cursor with ACKs |
| Isolation | Codex and Claude Code sandbox shells and file writes; opencode and DSH say plainly that permissions are not isolation and recommend a VM or container | No shell. Sources are read-only; the only workspace write is saving an upload into `uploads/` (never overwriting), and a Run's only effect is publishing an artifact into the store, or an MCP tool call the user approved |
| Untrusted content | Browsers treat page content as tainted, use an isolated critic, keep secrets out of model context | Source text, memory and excerpts are labelled as untrusted data; API keys are read from the environment and never enter a request |
| Context management | Rolling summaries and compaction near the limit (opencode, DSH, Claude Code) | Every request is measured first; lossless moves, then labelled lossy omissions, then a stop — never silent truncation or summarisation |
| Instructions and memory | `AGENTS.md` / `CLAUDE.md` chains | Opt-in workspace `AGENTS.md`, pinned with its SHA-256 when the Run starts; user-saved, versioned memories with optimistic concurrency |
| Extensibility | Hooks, plugins, MCP, subagents | Fixed built-in tools, user-allowed local or remote MCP tools (read-only, or approval-each-time) and skills, all selected per Run; HTTP webhook hooks before (allow/deny, fail closed) and after tool calls; providers behind one `DecisionModel` protocol. No plugins or subagents |
| Verification | Mostly left to the user or an auto-reviewer model | Evidence-only verifiers that never see the task text |

## Where the harness is intentionally different

- **Capabilities instead of permissions.** The products grant a general agent broad tools and then gate them. Here the tool set itself is the boundary: read selected sources, compute over them, publish a cited artifact. That makes the approval surface small enough to bind every approval to an exact parameter digest.
- **Verification instead of trust.** An artifact must cite the evidence that produced each computed value, and the completion reply must agree with what was published. None of the compared products documents a check of its output against tool evidence.
- **Honest stopping.** Over budget, the harness stops rather than summarising history the user cannot inspect.

## Gaps found in this pass

The products' mechanisms were used as a checklist against the code. Each item records what was wrong and what changed; every fix has a regression test.

| # | Gap | Reference pattern | Change |
|---|---|---|---|
| 1 | The tool catalogue offered at a step was not enforced for native tool calls: a model could, for example, read a source while only `plan_update` was offered. (The `json_tools` protocol already refused unknown names while decoding.) | DSH resolves tools once per proposed step; Codex and Claude Code refuse tools outside the active permission set | After the existing guards (which keep their specific errors), a call outside the offered catalogue is refused before dispatch with a retryable `tool_not_offered` that names the allowed tools. It costs a correction but does not oblige the model to retry that tool. The scripted fixture now saves a plan first, as a real model must. |
| 2 | Publication finalization replaced the conversation with *system + task + published body*, but memory, excerpt and history bookkeeping still pointed at the dropped messages. A memory edited during publication crashed the Run (`execution_error`); a budget overflow could have deleted the task message. | DSH keeps an open turn intact and repairs only under write ownership | Finalization clears that bookkeeping and the finalization context stays fixed: memory refreshes do not apply to it. |
| 3 | The API trusted any request with a loopback `Host` header and no foreign `Origin`. Any local process or other OS user, or anything that reached a wrongly published container port, could register a directory and read files through a Run. | opencode warns when its server runs without a password; notebook servers use a per-launch token | The launched workbench requires a per-launch access token (generated, or `AGENT_HARNESS_TOKEN`). A browser exchanges `/?token=…` for an HttpOnly, SameSite=Strict cookie; scripts send `Authorization: Bearer`. The test-only `testserver` host is no longer accepted by the shipped app. |
| 4 | Pages could be framed, so an approval button could be clickjacked. | Standard clickjacking defence | Every response sends `X-Frame-Options: DENY`, `frame-ancestors 'none'`, `nosniff` and `Referrer-Policy: no-referrer` (the first URL carries the token). |
| 5 | `model.requested` recorded the context budget but not what was sent, so a request could not be tied to its catalogue after the fact. | DSH: "model-visible means logged" | Each `model.requested` event records the offered tool names and a SHA-256 of the exact request body. |
| 6 | The citation preview and publication used different rules to find the server-generated evidence appendix, so a model-written heading could hide body text from one check but not the other. | — | Both use the same detector. |
| 7 | Found while fixing #1: native models without the local thinking profile — including the Claude and OpenAI-compatible adapters — were never offered `task_finish` and ended with plain text, so the completion check never ran for remote providers. | Claude Code and Codex end a turn with a typed result; DSH records why a turn ended | Remote adapters are offered `task_finish` after publication. A plain-text ending after publication, when `task_finish` was offered, goes through the same `completion_summary` check; an unsupported reply is kept out of the next request and becomes a correctable step that offers only `task_finish`. |
| 8 | A running Run could only be cancelled; the user had no way to add or correct an instruction without starting over. | Codex steer/queue, opencode `delivery: steer \| queue`, DSH inbox with `steer`/`followup` | `POST /api/runs/{id}/messages` admits a message idempotently by `message_id`; the runtime delivers it as a user turn at the next step boundary and logs `message.queued` / `message.delivered`; messages that never reached the model are reported as `message.undelivered`. |
| 9 | Scripts and CI could only drive the harness through hand-written HTTP calls. | Claude Code `-p --output-format stream-json`, `opencode run --format json` | `python -m agent_harness.headless` submits a task, prints every saved event as NDJSON plus a `headless.result` line, acknowledges events, reconnects from its cursor, and exits with 0 completed / 1 failed / 2 refused / 3 cancelled / 4 interrupted. |
| 10 | The tool set was fixed; no external tools could be connected. | MCP servers in Claude, Codex, opencode and DSH | User-configured stdio MCP servers; only allowlisted tools that do not declare side effects are offered, a Run opts into specific tools, results become cited evidence, and servers never see workbench secrets. |
| 11 | Only publication could ask for approval; any external tool with side effects had to be refused. | Claude Code deny → ask → allow rules; Codex typed approvals; DSH fail-closed outcomes | `askTools` lets an MCP tool with side effects run only after a single-use approval bound to its exact arguments and scope; dispatch refuses it otherwise, and lost outcomes become unknown effects that block automatic resume. |
| 12 | There was no way to package reusable instructions for kinds of work. | Agent Skills in Claude and Codex; AGENTS.md chains in opencode and DSH | `--skills-dir` loads `SKILL.md` skills; a Run sees only selected names and descriptions, loads instructions on demand with `skill_load` (hashed), and `/skill-name` selects a skill from the task text. |
| 13 | Revisions kept every version, but there was no way to see what changed between them. | Codex turn diffs; Claude Code checkpoints | A diff endpoint and preview toggle compare any two versions in a Session (default: against the parent), re-hashing both first. |
| 14 | The model and effort were fixed when the workbench started. | Model and effort pickers in Claude, Codex and opencode | Remote providers can offer alternative models and Claude its effort levels; each Run chooses (workbench selectors, `model` / `effort` in the API), records the choice, and keeps it on resume. Local models stay fixed because each needs its own verification. |
| 15 | The plan was public but not something the user could approve before work began. | Claude Code plan mode, opencode `plan` agent, plan-first agentic browsers | Opt-in plan approval: the Run pauses at its plan, “request changes” sends it back with the user's note (outside the correction budget), a revised plan needs a new approval, and a resume keeps the approved plan. |
| 16 | A workspace could not carry standing instructions; each task had to repeat its house rules. | `AGENTS.md` in Codex, opencode and DSH; `CLAUDE.md` in Claude Code | A Run can apply the workspace's `AGENTS.md` (8 KiB, UTF-8, regular file). The text and SHA-256 are pinned at admission and kept on resume, and they sit below the harness rules, so they cannot add sources, tools or approvals. One root file only, no directory chain. |
| 17 | Runs counted model and tool calls but not tokens or cost. | Claude Code `/cost`; token counts in Codex; session cost in opencode | Each Run totals the tokens its provider reports, in the same transaction as each reply event. `--price MODEL=INPUT,OUTPUT` gives a cost estimate pinned at admission; replies without usage are counted and make the estimate partial. No built-in price table. |
| 18 | The workbench was only in Traditional Chinese. | English-first UIs in the reference products | An English / 繁中 switch (defaults to the browser language, choice saved locally). Every UI string goes through one dictionary keyed by the original Chinese text; a test fails if a new string is not wrapped or has no English. File contents and model output are never translated. |
| 19 | Only plain-text files could be sources. | File attachments in Claude and Codex; document reading in agentic browsers | PDF and Word (`.docx`) files in the workspace are sources: the model reads bounded extracted text, citations and change detection use the file's own hash, and unreadable documents are refused with a reason. Uploads from the workbench are saved into `uploads/` after the same checks, and never overwrite. No OCR. |
| 20 | MCP servers had to run locally as commands. | Remote MCP servers in Claude, Codex and opencode | Remote servers over Streamable HTTP (HTTPS, or HTTP to loopback), with credentials read from environment variables the config names, never written in it or shown. OAuth is not supported. |
| 21 | Nothing outside the harness could veto or observe a tool call. | Hooks in Claude Code, Codex and opencode | HTTP webhooks before tool calls (allow/deny; any deny wins; unreachable or invalid counts as deny) and after them (observe only), recorded as Run events. No shell commands; hooks cannot change arguments and verifiers ignore them. |
| 22 | Token usage and cost were per Run only. | Session cost in Claude Code and opencode | `GET /api/sessions/{id}/usage` sums a Session's Runs; the estimate is partial when a Run is unpriced or reported no usage. |

## Already in place

- **Fail-closed approvals.** An approval is single-use and bound to a parameter digest, and it ends as approved, denied, expired (timeout or restart) or cancelled (the Run was cancelled). That is the same closed set DSH documents, and it is stricter than "always allow" scopes: there is no session-wide grant.
- **Durable, replayable streaming.** opencode's client bus is live-only; here a client can reconnect from any cursor.
- **Crash semantics.** Intent is persisted before dispatch, so an interrupted effect is reported as uncertain rather than retried, like DSH's rule of keeping an interrupted turn instead of truncating it.

## Backlog, by value for this harness

1. **A real Claude batch.** The adapter is covered by mock-transport tests only; a recorded run needs an API key and consent to spend.
2. **Column-aware citations.** A value that cites a table sample should sit under the column its label names; today the verifiers check role consistency for labelled lines, not table columns.
3. **OAuth connectors.** Remote MCP servers take credentials from environment variables; signing in through OAuth needs a decision on which services and how tokens are stored.
4. **Shell-command hooks.** Hooks are HTTP webhooks only; running local commands would need a sandbox decision.
5. **OCR for scanned PDFs.** Documents without a text layer are refused today.

## Sources

OpenAI Codex
- App Server: https://developers.openai.com/codex/app-server and https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md
- Approvals and security: https://learn.chatgpt.com/docs/agent-approvals-security
- Sandboxing and auto-review: https://learn.chatgpt.com/docs/sandboxing, https://learn.chatgpt.com/docs/sandboxing/auto-review
- Hooks: https://learn.chatgpt.com/docs/hooks
- SDK: https://learn.chatgpt.com/docs/codex-sdk

Claude Code and the Claude platform
- How Claude Code works: https://code.claude.com/docs/en/how-claude-code-works.md
- Permission modes and rules: https://code.claude.com/docs/en/permission-modes.md, https://code.claude.com/docs/en/permissions.md
- Hooks: https://code.claude.com/docs/en/hooks.md
- Checkpointing: https://code.claude.com/docs/en/checkpointing.md
- Headless mode: https://code.claude.com/docs/en/headless.md
- Agent SDK loop: https://code.claude.com/docs/en/agent-sdk/agent-loop.md
- Managed Agents events and permission policies: https://platform.claude.com/docs/en/managed-agents/events-and-streaming.md, https://platform.claude.com/docs/en/managed-agents/permission-policies.md

opencode (v2.2.6, `github.com/anomalyco/opencode`)
- Security policy: https://github.com/anomalyco/opencode/blob/v2.2.6/SECURITY.md
- Permissions: https://opencode.ai/docs/permissions
- Docs source (snapshots, compaction, client events): https://github.com/anomalyco/opencode/tree/v2.2.6/services/site/src/docs/content

DeepSeek Harness (dsh-v0.2.2-rc.1)
- Safety: https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.2-rc.1/SAFETY.md
- Architecture and subsystems: https://github.com/deepseek-ai/deepseek-harness/tree/dsh-v0.2.2-rc.1/docs

Agentic browsers
- Claude in Chrome: https://support.claude.com/en/articles/12902428-use-claude-in-chrome-safely, https://support.claude.com/en/articles/12902446-claude-in-chrome-permissions-guide
- ChatGPT built-in browser (Atlas retirement): https://help.openai.com/en/articles/20001371-chatgpt-atlas-retirement, https://help.openai.com/en/articles/20001277-using-the-built-in-browser-in-the-chatgpt-desktop-app
- Perplexity Comet: https://www.perplexity.ai/hub/blog/mitigating-prompt-injection-in-comet, https://www.perplexity.ai/help-center/en/articles/13531023-managing-comet-assistant-permissions
- Chrome (Gemini): https://blog.google/security/architecting-security-for-agentic/
- Microsoft Edge Copilot Mode: https://support.microsoft.com/en-us/topic/copilot-actions-in-edge-5ed5e17e-42df-40a3-984a-20420eba86e2
- Opera Neon: https://www.operaneon.com/faq
- Brave's analysis of indirect prompt injection: https://brave.com/blog/comet-prompt-injection/
