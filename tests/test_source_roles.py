"""One output field must not mix values the sources give under different roles."""
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from agent_harness_demo.model import ModelSettings
from agent_harness_demo.verifiers import consistent_source_roles
from agent_harness_demo.web import create_app
from test_completion import CompletionPeer
from test_completion_grounding import json_decision_reply
from test_workbench import create_task, finish

EVIDENCE = {
    "E1": {"path": "release.md", "labelled_lines": [
        {"line": 2, "label": "Owner", "value": "Sora"},
        {"line": 3, "label": "Release gate", "value": "passing regression tests."}]},
    "E2": {"path": "handoff.md", "labelled_lines": [
        {"line": 2, "label": "Incident escalation contact", "value": "Mika."}]},
}
SUBSTITUTED = (
    "# 文件索引\n\n## release.md [release.md E1]\n- **負責人**: Sora\n- **關鍵事項**: passing regression tests.\n\n"
    "## handoff.md [handoff.md E2]\n- **負責人**: Mika.\n- **關鍵事項**: Incident escalation contact.\n"
)
CORRECT = (
    "# 文件索引\n\n## release.md [release.md E1]\n- **負責人**: Sora\n- **關鍵事項**: passing regression tests.\n\n"
    "## handoff.md [handoff.md E2]\n- **負責人**: 未提供\n- **關鍵事項**: 事件升級聯絡人 Mika\n"
)


def test_contact_filled_in_as_owner_is_rejected() -> None:
    failure = consistent_source_roles(SUBSTITUTED, EVIDENCE)
    assert failure is not None and failure.code == "mixed_source_roles"
    assert failure.correctable_fields == ("content",)
    assert "負責人" in failure.message and "Sora" in failure.message and "Mika" in failure.message
    assert consistent_source_roles(CORRECT, EVIDENCE) is None


def test_feedback_says_which_document_to_change_under_each_reading() -> None:
    failure = consistent_source_roles(SUBSTITUTED, EVIDENCE)
    assert failure is not None
    message = failure.message
    # Each reading of the field is derived from the sources alone: the document
    # that lacks that role must be marked 未提供, and its value keeps its own role.
    assert "若「負責人」是 Owner：handoff.md 沒有 Owner，請改為未提供" in message
    assert "Mika. 是 handoff.md 的 Incident escalation contact" in message
    assert "若「負責人」是 Incident escalation contact：release.md 沒有 Incident escalation contact，請改為未提供" in message
    # The reading whose value appears first in the artifact is offered first.
    assert message.index("是 Owner：") < message.index("是 Incident escalation contact：")


def test_feedback_points_to_the_documents_own_value_when_it_has_one() -> None:
    evidence = {
        "E1": {"path": "a.md", "labelled_lines": [{"line": 2, "label": "Owner", "value": "Ann"}]},
        "E2": {"path": "b.md", "labelled_lines": [
            {"line": 2, "label": "Owner", "value": "Ben"}, {"line": 3, "label": "Contact", "value": "Cid"}]},
    }
    failure = consistent_source_roles("## a.md\n- **負責人**: Ann\n\n## b.md\n- **負責人**: Cid\n", evidence)
    assert failure is not None
    assert "若「負責人」是 Owner：b.md 的 Owner 是 Ben，請改為 Ben" in failure.message


def test_role_annotation_in_the_label_does_not_make_a_separate_field() -> None:
    # Observation batch 04: after feedback the model relabelled the lines instead
    # of marking the owner as missing. Readers still see Mika as 負責人.
    relabelled = (
        "## release.md [release.md E1]\n- **負責人 (Owner)**: Sora\n\n"
        "## handoff.md [handoff.md E2]\n- **負責人 (Incident escalation contact)**: Mika.\n"
    )
    failure = consistent_source_roles(relabelled, EVIDENCE)
    assert failure is not None and "「負責人」" in failure.message
    full_width = relabelled.replace("(Owner)", "（Owner）").replace("(Incident escalation contact)", "（Incident escalation contact）")
    assert consistent_source_roles(full_width, EVIDENCE) is not None


def test_bilingual_label_for_a_single_role_is_still_accepted() -> None:
    bilingual = (
        "## release.md [release.md E1]\n- **負責人 (Owner)**: Sora\n\n"
        "## handoff.md [handoff.md E2]\n- **負責人 (Owner)**: 未提供\n"
        "- **事件升級聯絡人 (Incident escalation contact)**: Mika.\n"
    )
    assert consistent_source_roles(bilingual, EVIDENCE) is None


def test_value_that_restates_its_source_role_is_not_mixed() -> None:
    combined = (
        "| 主題 | 負責人 | 關鍵事項 |\n|---|---|---|\n"
        "| Release checklist | Sora | passing regression tests. |\n"
        "| Support handoff | 未提供 | Incident escalation contact: Mika. |\n"
    )
    assert consistent_source_roles(combined, EVIDENCE) is None


def test_key_value_table_labels_each_row_by_its_first_cell() -> None:
    key_value = (
        "| 項目 | 內容 | 來源 |\n|---|---|---|\n"
        "| 負責人 | Sora | [release.md E1] |\n"
        "| 發布條件 | passing regression tests. | [release.md E1] |\n"
        "| 事件升級聯絡人 | Mika. | [handoff.md E2] |\n"
    )
    assert consistent_source_roles(key_value, EVIDENCE) is None


def test_entity_table_labels_cells_by_column_even_without_file_names() -> None:
    substituted_table = (
        "| 主題 | 負責人 | 關鍵事項 |\n|---|---|---|\n"
        "| Release checklist | Sora | passing regression tests. |\n"
        "| Support handoff | Mika. | 未提供 |\n"
    )
    failure = consistent_source_roles(substituted_table, EVIDENCE)
    assert failure is not None and "負責人" in failure.message


def test_single_document_cannot_reveal_a_substitution() -> None:
    # With no other owner in the artifact there is no evidence of which role
    # the field holds, so the check stays silent instead of guessing.
    alone = {"E2": EVIDENCE["E2"]}
    assert consistent_source_roles("- **負責人**: Mika.\n", alone) is None


def test_mixed_roles_are_returned_to_the_model_before_publication(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "release.md").write_text("# Release checklist\nOwner: Sora\nRelease gate: passing regression tests.\n", encoding="utf-8")
    (source / "handoff.md").write_text("# Support handoff\nIncident escalation contact: Mika.\n", encoding="utf-8")
    peer = CompletionPeer([
        json_decision_reply("plan_update", {"steps": ["讀取文件並整理索引。"]}),
        json_decision_reply("source_read", {"path": "release.md"}),
        json_decision_reply("source_read", {"path": "handoff.md"}),
        json_decision_reply("artifact_write", {"name": "index.md", "content": SUBSTITUTED, "citations": ["E1", "E2"]}),
        json_decision_reply("artifact_write", {"name": "index.md", "content": CORRECT, "citations": ["E1", "E2"]}),
        json_decision_reply("task_finish", {"summary": "已發布 index.md；handoff.md 的負責人未提供。"}),
    ])
    settings = ModelSettings.local("contract-model", stream=False, decision_protocol="json_tools")
    with TestClient(create_app(tmp_path / "store", model_settings=settings,
                               model_transport=httpx.MockTransport(peer))) as client:
        run, events = finish(client, create_task(client, source, sources=["release.md", "handoff.md"]))
        assert run["status"] == "completed", run["error"]
        assert len(run["artifacts"]) == 1
        published = client.get(run["artifacts"][0]["download_url"]).text
    assert published.startswith(CORRECT)
    rejected = next(event["data"] for event in events if event["kind"] == "correction.available")
    assert rejected["tool"] == "artifact_write"
    assert rejected["error"]["code"] == "mixed_source_roles"
    assert any(event["kind"] == "correction.observed" and event["data"]["tool"] == "artifact_write" for event in events)
