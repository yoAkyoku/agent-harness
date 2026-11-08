# Real 9B GUI workflow 01

Independent GUI evidence on candidate `1e05f2e`, using installed Qwen3.5 9B, JSON tools and thinking disabled. This is not a fixed six-case quality batch and does not change earlier rejection decisions. Configuration and source hashes are in `manifest.json`.

## Completed analysis and approval

The browser selected the original analysis-standard CSV, draft review, artifact receipts and explicit publication approval. Run `ba2622198f7d438d8888b871579b169e` completed with 7 model calls, 8 tool calls and 56 ordered events. Its 607-byte report contains independently checked totals 240 and 15, and rate 6.25%, with actual tool evidence. One approval was approved and consumed; one artifact was published.

Reload occurred while waiting for approval, not during active model execution. Reload returned to the start screen; selecting existing history restored the same Run and approval body. The exported list still contained one Run. The final timeline displayed each sequence once. This proves history-based restoration at that state, not automatic active-session restoration.

The browser clicked the download link. The public endpoint returned HTTP 200, attachment disposition and expected bytes/SHA. A physical browser-saved file and download completion were not observed. See `download-endpoint-audit.json`.

The body passed this narrow numeric/source check. Qualified hypothetical column examples and imprecise tool wording in the plan remain limitations; this does not establish complete plan quality or instruction adherence.

![Completed first Run and artifact preview](completed-preview.jpg)

## Failed revision

The browser selected “以這個版本修改” and requested a Markdown table of existing verified numbers, without added dates or claims. Run `69d12230b4a44d86ae1c78f2bc217cce` pinned the published artifact as its base.

Both draft attempts reused the previous Run's E1–E4 IDs and copied its evidence appendix. The new Run had performed no evidence-producing read or calculation, so its registry was empty. Both attempts correctly received `invalid_evidence`. After 3 model calls and 3 tool calls, the next request exceeded the conservative context envelope and ended with `context_limit_exceeded`. No second artifact or publication approval was created. The original artifact's bytes and SHA remained unchanged.

This is an end-to-end revision failure, not successful versioning, successful correction or full GUI acceptance. Both browser console exports contain no warning/error entries; that does not negate the Run failure.

## Interface diagnosis and next work

The pinned base passes same-session, completed-Run and byte-hash checks, then enters context as untrusted text including historical evidence IDs. New Runs intentionally start with an empty evidence registry. The base context exposes old labels without a fresh evidence-producing operation for the selected artifact itself. The model also ignored the existing route of reading selected original sources for new evidence.

A fix must preserve current-Run citation isolation and selected-source permissions. Evaluate an explicitly selected-base read operation returning fresh, honestly artifact-scoped evidence, or a narrower instruction to reread selected sources. Do not automatically admit ancestor IDs, imply that reading an artifact repeats its calculations, or grant access to unselected historical files. No product change has been made from this observation alone.

Use the agreed public HTTP/WebSocket and model-transport seams to check exact pinned version, fresh provenance, immutable original bytes, rejected ancestor IDs and restart behavior. Real-model revision then needs an independent recheck.

## Evidence boundaries

The three stage directories contain read-only public-API exports. Task submission, approval, revision and download-link actions were browser actions. Raw AX captures and screenshots record the UI. `workflow-summary.json` lists remaining GUI gates. Full six-case quality, current-candidate controlled-fault quality, successful revision and final spec/standards audit remain open.
