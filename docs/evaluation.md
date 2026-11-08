# Evaluation

This page records how the harness was evaluated with a real model, including the failures. Detailed per-batch records (in Traditional Chinese, with run IDs, fingerprints and per-case reviews) are under [`evaluation/records/`](evaluation/records/).

## What is accepted, and what is only observed

The harness is accepted on **behaviour**: it must detect output that tool evidence does not support, return a correctable error to the model, stop within budget, and keep a traceable artifact and event log. That is proven by public-interface tests, replays of recorded real-model runs, and a labelled controlled-fault case.

How well a particular model answers a fixed task set is recorded as a **quality observation**, failures included. It is deliberately not a pass gate: gating on a small fixed suite rewards adding rules for those cases, which is exactly what went wrong here once (below).

## Suites

- [`evaluation/slice-09`](../evaluation/slice-09/cases.json) — six fixed cases: three CSV analyses (standard, variant, a missing column) and three document indexes (standard, variant, a missing owner). Truth was written before any model run.
- [`evaluation/unseen-01`](../evaluation/unseen-01/cases.json) — two tasks the runtime was never developed against: a ticket analysis with different columns and an extra mean, and meeting notes with different labels, one missing decision and a distractor role.

All runs below used Qwen3.5 9B (Q4_K_M) on a local Ollama 0.40.10, JSON-tools protocol, thinking off, an 8,192-token context and the suite's fixed limits. The same model, inputs and limits were used in every batch, so differences come from the harness.

## What happened

| Batch | Harness change | Accepted | Notes |
|---|---|---|---|
| Legacy | ~450 lines of task-specific rules in the runtime | 6/6 | Rules matched the suite's wording (release gate, escalation window, owner) and even held translations of sentences from the evaluation files. |
| Observation 01 | Those rules removed; evidence-only verifiers | **3/6** | The honest baseline. Three failures were model semantic errors. |
| Observation 02 | The model sees the published body when writing its completion reply; reply-vs-artifact check | **4/6** | All six artifacts were byte-identical to batch 01; only replies changed. The missing-column case now reports its gaps correctly on the first try. |
| Observation 03 | Role-consistency check | 4/6 | The wrong owner was blocked before publication; the model resubmitted identical content and the run failed within budget. No other case changed. |
| Observation 04 | Role feedback names the line to change | 4/6 | The model acted on the feedback, but by relabelling the field `Owner (Incident escalation contact)` to slip past the check. |
| Observation 05 | Parenthetical labels count as the same field | 4/6 | The workaround was blocked as well; the run failed without publishing a wrong owner. |
| Unseen 01 | — | 1/2 | Meeting notes correct, distractor role not misused. The ticket analysis added a per-team table whose numbers came from other columns. |
| Controlled fault | Injected read failure | Pass | The model issued a new decision with a new call ID and recovered; the executor never retried on its own. |

Batches 02–05 and the unseen batch ran while the GPU was shared with another application, so their timings are not comparable with batch 01; artifacts and call counts are unaffected.

## Lessons

- **A 6/6 score can hide a harness that writes answers.** Removing case-specific rules cost three cases. Rebuilding with mechanisms that never read the task text recovered one of them and blocked another from publishing wrong content.
- **Root causes beat rules.** Identical completion replies across cases came from the finalization request carrying only the task text. Showing the model its own artifact fixed the reply on the first attempt — the new check never had to fire.
- **A model will satisfy the letter of a check.** After targeted feedback the model relabelled a field instead of correcting it. Closing that gap was a structural change (labels with role annotations are one field), not a rule for the case.
- **Know when to stop.** The 9B model cannot find the correct edit for the substituted owner within two corrections. The harness now refuses to publish it; going further would mean writing the answer for the model.

## Known gaps

- `documents-standard`: the model reads the task's parenthetical *(release gate or escalation window)* as an extra field and contradicts a supplied value. There is no task-text-free way to detect this yet.
- Numbers cited to evidence but taken from the wrong column (unseen ticket analysis) are not checked.
- The missing-field grader expects the source filename on the same line, so a correct artifact that uses section headings is graded as failing.
- The unseen ticket task's wording ("average handling hours per team") is ambiguous; the next suite should avoid it.
- Claude and OpenAI-compatible providers are covered by transport-level tests only; no paid real-model batch has been run with them yet.

Commit hashes in the records refer to the project's development history, which is not part of this repository.
