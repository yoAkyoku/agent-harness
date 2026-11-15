# One Run, step by step

[繁體中文](walkthrough.zh-TW.md)

This follows a real Run from the saved events of the [hard-02 batch](evaluation/records/qwen3.5-9b-hard-02-2026-11-16/README.md) (`delegated-vendors-1.ndjson`, Run `8af289e1…`, Qwen3.5 9B on a local Ollama). Nothing here is staged: every number is in that file. It takes about five minutes to walk through, and the same Run can be reproduced with the commands at the end.

**The task.** Two supplier quotes are selected as sources. The task asks the model to hand each quote to a subagent, list both unit prices and lead times, and say which is cheaper and which is faster.

## 1. Admission: what the Run may touch is fixed before the model is called

`run.accepted` records the model, the limits and the policy (`subagents: true`). The two files are hashed at this point; a file that changes later is refused, not silently re-read. The workbench was started with `--allow-subagents`, and the Run asked for subagents: both are needed.

## 2. Step 1: only planning is offered

```
5  model.requested   step 1   offered_tools: [plan_update]            bound 3,978 / 8,192 tokens
9  plan.updated      steps: [檢查輸入。, 依證據作答。, 核對成果。]
```

Until a plan is saved, `plan_update` is the only tool in the request, and a call to anything else would be refused as not offered. Every request is measured first; the bound shown is the conservative upper estimate that must stay under the window.

## 3. Steps 2–3: the tool catalogue now includes delegation

```
11 model.requested   step 2   offered_tools: [plan_update, source_read, subagent_run, workspace_list]
18 tool.requested    subagent_run   {task: "請查詢 vendor-a.md 中的單價與交期。", sources: [vendor-a.md]}
19 tool.dispatched   subagent_run
```

`tool.dispatched` is written before anything runs. If the process died here, the Run would be reported as interrupted with this call's effect unknown, and continuing would be an explicit new Run.

## 4. Inside the subagent: a fresh, smaller context and fewer tools

```
20 subagent.started          sources: [vendor-a.md]
21 subagent.model_requested  step 1   offered_tools: [source_read]                bound 2,308
23 subagent.tool_completed   source_read vendor-a.md
24 subagent.model_requested  step 2   offered_tools: [source_read, task_finish]   bound 3,114
26 subagent.completed        steps 2   verified against: vendor-a.md
```

The child's first request is bounded at 2,308 tokens while the parent's is already past 5,500: the child sees only the delegated sentence, not the parent's conversation. It is offered `source_read` and nothing that publishes or delegates, and `task_finish` appears only once it has evidence to cite. Its two model calls and its tool call are added to the parent Run's usage.

## 5. The report crosses the boundary only after verification

The child answered `…單價為每台 1850 元 [來源 E1]…` (it copied the placeholder label from its instructions instead of the file name). That answer was checked against the child's own evidence with the same verifier as a direct answer. The parent then received one evidence record:

```
27 tool.completed   subagent_run   evidence_id: E1
     text:    根據 vendor-a.md，供應商 A 的單價為每台 1850 元 (vendor-a.md)，交期為下單後 21 天 (vendor-a.md)。
     sources: [{path: vendor-a.md, sha256: eb24c6fd…}]
```

The child's `E1` is gone from the text: the parent has its own `E1`, and the two must not be confused. The citation was rewritten with the path from the child's evidence record, not with the label the child wrote. The record keeps the file and hash the report was verified against. The second quote goes the same way (events 30–39) and becomes the parent's `E2`.

## 6. The answer is checked, then recorded with what it rests on

```
42 tool.requested    task_finish
45 answer.verified   cited: E1 → subagent report, verified against vendor-a.md (eb24c6fd…)
                            E2 → subagent report, verified against vendor-b.md (0be18763…)
46 run.completed
```

The answer cites `[subagent E1]` and `[subagent E2]` next to each figure. `answer.verified` records that both are model-written reports and names the source versions behind them, so a reader can go from a figure in the answer to a file hash in two steps. Nine model calls, nine tool calls, 7,749 input and 627 output tokens, 37 seconds.

## 7. What a rejection looks like

In the same batch (`refund-rate-1.ndjson`) the model's first answer cited the percentage but stated the two column totals without citing the calculations that produced them. The verifier refused it:

```
tool.rejected         task_finish   code: missing_calculation_citation
correction.available  correctable_fields: [summary]   remaining_corrections: 1
```

The error quotes the line, names the missing citations (`328517: [orders.csv E2]`, `45381: [orders.csv E3]`), the field the model may change and how many corrections are left. The next decision added them and was accepted. The rejected attempt stays in the events.

## 8. What stopping looks like

`report-artifact-1.ndjson` did not finish. After aggregating two columns, the model also read the whole 4 KB table; the request no longer fit:

```
context.pruned   source_excerpt_budget … acknowledged_evidence … tool_descriptions … artifact_name_descriptions
context.limited  upper_bound_tokens: 10,440   capacity_tokens: 8,192
run.failed       context_limit_exceeded
```

Each step that was tried is in the log, in order. The Run did not truncate the table or publish a partial report. The same task with `--compact` would have been allowed to summarise the table text instead.

## Reproduce it

```bash
uv run python evaluation/hard-02/make_sources.py
```

```bash
uv run python -m agent_harness --model qwen3.5:9b-q4_K_M --decision-protocol json_tools --allow-subagents
```

In a second terminal, with `AGENT_HARNESS_TOKEN` set to the token the workbench printed:

```bash
uv run python -m agent_harness.headless --workspace evaluation/hard-02/workspace --source vendor-a.md --source vendor-b.md --answer --subagents --task "請把兩份報價各交給一個子代理查詢單價與交期。回答時列出兩家各自的單價與交期，並指出哪一家單價較低、哪一家交期較短，附來源。"
```

The same Run as an OpenTelemetry trace (the Run is an `invoke_agent` span, each subagent a nested one under its `execute_tool subagent_run` span):

```bash
curl -s -H "Authorization: Bearer $AGENT_HARNESS_TOKEN" http://127.0.0.1:8787/api/runs/RUN_ID/trace
```

To watch it in the browser instead, open the address the workbench prints, choose the two files, tick “Allow delegating sub-questions to subagents” and “Answer directly”, and paste the task. The timeline shows the same events as they are saved.
