import pytest

from agent_harness import web
from scripted import SERVICE_TOKEN


@pytest.fixture(autouse=True)
def local_test_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """Starlette's TestClient sends `Host: testserver`; the shipped app accepts loopback names only.

    Services launched as child processes, and recorder scripts, share one known access token.
    """
    monkeypatch.setattr(web, "TRUSTED_HOSTS", (*web.TRUSTED_HOSTS, "testserver"))
    monkeypatch.setenv("AGENT_HARNESS_TOKEN", SERVICE_TOKEN)
