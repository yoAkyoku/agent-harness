"""Rules shared by everything that calls a user-configured HTTP endpoint (remote MCP servers, hooks).

Endpoints must use HTTPS, except plain HTTP to a loopback address. Credentials are never written
in a config file: the config names environment variables, read when a request is made, so the
secrets stay out of the API, events and the model's context.
"""
import os
import re
from urllib.parse import urlsplit

ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
RESERVED_HEADERS = {"host", "content-length", "content-type", "accept", "connection", "transfer-encoding",
                    "mcp-session-id", "mcp-protocol-version"}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def check_url(owner: str, url: object) -> str:
    parts = urlsplit(url) if isinstance(url, str) else None
    if parts is None or not parts.hostname or not (
            parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS)):
        raise ValueError(f"{owner}: url must use https (plain http only to a loopback address).")
    return str(url)


def check_header_env(owner: str, mapping: object) -> dict[str, str]:
    """Validate a {header name: environment variable name} map."""
    if not isinstance(mapping, dict):
        raise ValueError(f"{owner}: headerEnv maps header names to environment variable names.")
    for header, variable in mapping.items():
        if not isinstance(header, str) or not HEADER_NAME.fullmatch(header) or header.lower() in RESERVED_HEADERS:
            raise ValueError(f"{owner}: header {header!r} cannot be set from headerEnv.")
        if not isinstance(variable, str) or not ENV_NAME.fullmatch(variable):
            raise ValueError(f"{owner}: headerEnv values must name an environment variable.")
    return dict(mapping)


def read_headers(header_env: dict[str, str], *, bearer_env: str | None = None) -> dict[str, str]:
    """The request headers, read from the environment now; a missing variable is an error naming it."""
    names = ({"Authorization": bearer_env} if bearer_env else {}) | header_env
    headers: dict[str, str] = {}
    for header, variable in names.items():
        value = os.environ.get(variable)
        if not value:
            raise RuntimeError(f"environment variable {variable} is not set")
        headers[header] = f"Bearer {value}" if bearer_env and header == "Authorization" and variable == bearer_env else value
    return headers
