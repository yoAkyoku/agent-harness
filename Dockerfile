# Agent Harness workbench. The default command runs the labelled scripted
# fixture, so the image works without any model or API key:
#
#   docker build -t agent-harness .
#   docker run --rm -p 127.0.0.1:8787:8787 agent-harness
#
# Claude (the key is passed through from your shell, never baked in):
#
#   docker run --rm -p 127.0.0.1:8787:8787 -e ANTHROPIC_API_KEY \
#     agent-harness --provider anthropic
#
# Mount your own files to analyse them: -v "$PWD/data:/workspace:ro"
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev

RUN useradd --create-home harness && mkdir -p /data /workspace && chown harness /data /workspace
USER harness
VOLUME ["/data"]
EXPOSE 8787
ENTRYPOINT ["/app/.venv/bin/python", "-m", "agent_harness_demo", "--host", "0.0.0.0", "--port", "8787", "--store", "/data"]
