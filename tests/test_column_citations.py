"""Column-aware citations: a value that cites a table sample sits under the column its label names.

The check is deliberately narrow. It applies only when the artifact's label for a value is exactly
one of the cited table's column names, and the value appears in the table sample only under a
different column. Values that also appear under the labelled column, and labels that are not column
names, are left alone. Like every verifier, it reads only the artifact and the tool evidence.
"""
import httpx
from pathlib import Path

from fastapi.testclient import TestClient

from agent_harness.model import ModelSettings
from agent_harness.verifiers import table_columns, verify_artifact
from agent_harness.web import create_app
from test_completion import CompletionPeer
from test_workbench import finish, tool_reply

TABLE = {"E1": {"path": "people.csv", "sha256": "0" * 64, "columns": ["Name", "Owner", "Contact"], "rows": 2,
                "sample": [["Billing", "Mei", "Bo"], ["Search", "Sora", "Ken"]], "evidence_id": "E1"}}


def _body(text: str) -> str:
    return text + "\n\n## Verified tool evidence\n- [E1] people.csv SHA-256 " + "0" * 64 + "\n"


def test_a_value_under_another_columns_label_is_refused() -> None:
    failure = table_columns(_body("| Name | Owner |\n|---|---|\n| Billing | Bo [people.csv E1] |\n"), TABLE)
    assert failure is not None and failure.code == "column_mismatch"
    assert "Bo" in failure.message and "Contact" in failure.message and "Owner" in failure.message
    assert failure.correctable_fields == ("content",)


def test_label_value_lines_are_checked_too() -> None:
    failure = table_columns(_body("Owner: Ken [people.csv E1]\n"), TABLE)
    assert failure is not None and "Ken" in failure.message and "Contact" in failure.message


def test_values_in_their_own_column_pass() -> None:
    assert table_columns(_body("| Name | Owner | Contact |\n|---|---|---|\n"
                               "| Billing [people.csv E1] | Mei [people.csv E1] | Bo [people.csv E1] |\n"), TABLE) is None
    assert table_columns(_body("Owner: Sora [people.csv E1]\n"), TABLE) is None


def test_labels_that_are_not_columns_and_uncited_values_are_left_alone() -> None:
    assert table_columns(_body("Escalation: Bo [people.csv E1]\n"), TABLE) is None  # not a column name
    assert table_columns(_body("Owner: Bo\n"), TABLE) is None  # cites no table evidence


def test_a_value_found_under_both_columns_is_not_flagged() -> None:
    shared = {"E1": {**TABLE["E1"], "sample": [["Billing", "Mei", "Bo"], ["Search", "Bo", "Ken"]]}}
    assert table_columns(_body("Owner: Bo [people.csv E1]\n"), shared) is None


def test_the_check_is_one_of_the_default_verifiers() -> None:
    failure = verify_artifact(_body("Owner: Bo [people.csv E1]\n"), TABLE)
    assert failure is not None and failure.code == "column_mismatch"


def test_a_run_corrects_a_value_placed_under_the_wrong_column(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "people.csv").write_bytes(b"Name,Owner,Contact\nBilling,Mei,Bo\nSearch,Sora,Ken\n")
    replies = [
        tool_reply("plan_update", {"steps": ["Inspect the table", "Publish the owners"]}, "plan"),
        tool_reply("table_inspect", {"path": "people.csv"}, "inspect"),
        tool_reply("artifact_write", {"name": "owners.md", "citations": ["E1"],
                                      "content": "| Name | Owner |\n|---|---|\n| Billing | Bo [people.csv E1] |\n"}, "write"),
        tool_reply("artifact_write", {"name": "owners.md", "citations": ["E1"],
                                      "content": "| Name | Owner |\n|---|---|\n| Billing | Mei [people.csv E1] |\n"}, "write-2"),
        tool_reply("task_finish", {"summary": "Saved owners.md: Mei owns Billing."}, "finish"),
    ]
    peer = CompletionPeer(replies)
    settings = ModelSettings.local("contract-model", stream=False, thinking=True)
    with TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(peer))) as client:
        workspace = client.post("/api/workspaces", json={"path": str(source)}).json()
        session = client.post("/api/sessions", json={"workspace_id": workspace["id"]}).json()
        run = client.post(f"/api/sessions/{session['id']}/runs", json={"task": "List the owners.", "sources": ["people.csv"]}).json()
        final, events = finish(client, run)

    assert final["status"] == "completed", final["error"]
    correction = next(event["data"] for event in events if event["kind"] == "correction.available")
    assert correction["error"]["code"] == "column_mismatch"
    assert len(final["artifacts"]) == 1
