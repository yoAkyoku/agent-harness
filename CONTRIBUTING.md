# Contributing

## Setup

```bash
uv sync --locked
uv run pytest -q
uv run mypy
node --check src/agent_harness_demo/static/app.js
```

## Principles

- **Test through the two seams only:** the public HTTP/WebSocket interface and the model's HTTP transport. Use real temporary SQLite stores and files behind them; do not call private methods or mock the runtime.
- **One failing behaviour at a time.** Write the test, watch it fail, implement the smallest complete behaviour.
- **Never make the harness task-aware.** Verifiers receive the artifact and tool evidence, never the task text. Do not add rules keyed to an evaluation case's wording or content; record model failures as observations instead.
- **Persist before you notify.** Events and state are saved before any notification. Model output is a request, not proof that a tool ran or an artifact exists.
- **Enforce before dispatch.** Schema, workspace scope and approvals are checked before a tool runs. Artifact versions are immutable. Effects with unknown outcomes are never retried automatically.
- **Keep evidence kinds apart.** Transport-level tests, real-model batches and browser walkthroughs prove different things; do not let one stand in for another.
- **Annotate types** for Python interfaces and keep blocking file or database work off the event loop.

## Running a quality batch

Start a workbench with a real model, then record one case at a time with `scripts/validate_demo.py` (see `--help`). Each case needs a manual review before the next; `--observe-all` keeps going after a rejected case. Record failures; never rerun a case to get a better score.
