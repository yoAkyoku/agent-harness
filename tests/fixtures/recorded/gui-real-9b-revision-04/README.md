# Real 9B GUI revision 04: base read observed, revision rejected

Candidate `57fb2aa` loads the pinned base through the existing selected-version read instead of unconditionally preloading its text. The original 607-byte artifact, exact revision task, selected CSV, draft-review/receipt/approval policy, installed Qwen3.5 9B JSON/nonthinking configuration and original limits remain fixed. This is an independent GUI check, not a six-case quality batch. Earlier failures are unchanged. The user explicitly retained only approved Qwen3.5 9B/4B; no Qwen2.5 profile or inference was introduced.

The existing browser tab was reconnected, reloaded, and its original completed Run selected. The operator clicked “以這個版本修改”, entered the identical task and submitted once. All Run creation/control actions were browser actions; public HTTP collected evidence read-only. No tool arguments or model responses were replaced.

Run `56ed23d8ea34460ba1f1a6c41697887a` actually chose base_artifact_read after planning. Its result E1 contains the complete original 607 bytes, exact artifact ID, origin Run, version1, lines1–16 and SHA `6c0ae6cec490cab714b1503f5e74202d5fb855cd868d28ac4bb9dfd2b059bd96`. The exported result text was byte-compared with the original download. This demonstrates real-model adoption of the selected-base read, not just a metadata or fixture claim.

The model then inspected metrics.csv and aggregated both columns instead of proceeding directly to a reformatted artifact. After 4 model calls and 5 tool attempts, request5 exceeded the unchanged conservative context envelope: rendered prompt byte bound7107 +64 special-token allowance +1024 output reserve =8195 >8192. These are conservative budget components, not a measured model token count. The Run failed after39 durable events and70.398581 seconds. There was no number_calculate, correction, draft, publication approval, v2 artifact or final receipt. The original bytes/SHA remain unchanged.

![Actual conservative context refusal](failed-visible.jpg)

On-demand reading works in this Run, but full revision delivery remains rejected. The previous recorded-prefix controls publish successfully without a base read; they cannot substitute for this new actual route, which retained both a full artifact read and additional CSV evidence. Do not claim that the context problem is generally solved, round away the excess, raise the cap or silently skip evidence.

The next useful diagnostic is a public replay of this new prefix, including the exact full base read and same-decision aggregate results. Inspect whether repeated source metadata in those unread results can be represented losslessly; also assess model guidance for formatting existing facts versus fresh calculations without hardcoding tasks or removing allowed capabilities. Neither change has been made from this observation alone.

`manifest.json` fixes the candidate/model/input scope. `manual-review.json` records exact byte checks, usage, tool sequence and bounds. The stage directories preserve complete public exports; AX/screenshots record the UI. Console warnings/errors were empty, and all five Store Runs were terminal after export. Full six-case quality, successful revision/full GUI, final-candidate controlled-fault quality and final audit remain open.
