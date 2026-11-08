"""Implementation identity is observed only through the public service API."""

from pathlib import Path
import os
import re
import shutil
import subprocess
import sys

import pytest

from test_recovery import running_service


def test_service_identity_is_frozen_until_restart_and_tracks_package_changes(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    package = checkout / "src" / "agent_harness_demo"
    shutil.copytree(Path(__file__).resolve().parents[1] / "src" / "agent_harness_demo", package,
                    ignore=shutil.ignore_patterns("__pycache__"))
    store = tmp_path / "store"
    with running_service(store, package_root=checkout) as service:
        original = service.client.get("/api/config").json()["implementation"]
        assert original["algorithm"] == "package-files-sha256-v1"
        assert re.fullmatch(r"[a-f0-9]{64}", original["source_sha256"])
        source = package / "tools.py"
        source.write_bytes(source.read_bytes() + b"\n# Independent deployment change.\n")
        assert service.client.get("/api/config").json()["implementation"] == original
    with running_service(store, package_root=checkout) as service:
        changed = service.client.get("/api/config").json()["implementation"]
        assert changed["source_sha256"] != original["source_sha256"]
        cache = package / "__pycache__" / "irrelevant.cache"
        cache.parent.mkdir(exist_ok=True)
        cache.write_bytes(b"not implementation")
    with running_service(store, package_root=checkout) as service:
        assert service.client.get("/api/config").json()["implementation"] == changed


@pytest.mark.parametrize("recorder", ["validate_demo.py", "validate_correction.py"])
def test_recorders_reject_an_earlier_service_before_run_admission(tmp_path: Path, recorder: str) -> None:
    root = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "checkout"
    for directory in ("src", "scripts", "evaluation"):
        shutil.copytree(root / directory, checkout / directory, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(root / ".gitignore", checkout / ".gitignore")
    def git(*arguments: str) -> None:
        subprocess.run(["git", *arguments], cwd=checkout, check=True, capture_output=True)
    git("init", "-q")
    git("config", "core.autocrlf", "false")
    git("add", ".gitignore", "src", "scripts", "evaluation")
    git("-c", "user.name=Validation", "-c", "user.email=validation@example.invalid", "commit", "-qm", "Original package")
    with running_service(tmp_path / "store", package_root=checkout) as service:
        source = checkout / "src" / "agent_harness_demo" / "tools.py"
        source.write_bytes(source.read_bytes() + b"\n# A later committed checkout.\n")
        git("add", "src")
        git("-c", "user.name=Validation", "-c", "user.email=validation@example.invalid", "commit", "-qm", "Later package")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(checkout / "src")
        arguments = ["--base-url", str(service.client.base_url), "--case", "analysis-standard"] if recorder == "validate_demo.py" else ["--port", str(service.client.base_url.port)]
        result = subprocess.run([sys.executable, str(checkout / "scripts" / recorder), "--output", str(tmp_path / "evidence"), *arguments],
                                cwd=checkout, env=environment, capture_output=True, text=True, timeout=15)
        assert result.returncode != 0
        assert "Service implementation differs or is unidentified" in result.stderr
        assert service.client.get("/api/workspaces").json() == []
        assert service.client.get("/api/sessions").json() == []
