import inspect

from agent_harness_demo import verifiers
from agent_harness_demo.verifiers import calculation_citations, verify_artifact

EVIDENCE = {
    "E1": {"path": "metrics.csv", "columns": ["week", "orders"], "sample": []},
    "E2": {"operation": "sum", "value": "150", "inputs": ["E1"]},
}


def test_verifiers_never_receive_task_text() -> None:
    for verifier in verifiers.DEFAULT_VERIFIERS:
        assert list(inspect.signature(verifier).parameters) == ["content", "evidence"]


def test_computed_value_cited_to_the_wrong_evidence_is_rejected() -> None:
    failure = calculation_citations("訂單總數：150 [metrics.csv E1]", EVIDENCE)
    assert failure is not None and failure.code == "missing_calculation_citation"
    assert "[metrics.csv E2]" in failure.message
    assert calculation_citations("訂單總數：150 [metrics.csv E2]", EVIDENCE) is None


def test_uncited_value_relies_on_appendix_only_for_a_single_source() -> None:
    assert calculation_citations("訂單總數：150", EVIDENCE) is None
    two_sources = {**EVIDENCE, "E3": {"path": "notes.md", "text": "note"}}
    failure = calculation_citations("訂單總數：150", two_sources)
    assert failure is not None and "[metrics.csv E2]" in failure.message


def test_numbers_inside_the_server_appendix_are_ignored() -> None:
    content = "訂單總數：150 [metrics.csv E2]\n\n## Verified tool evidence\n- E2 = 150"
    assert verify_artifact(content, EVIDENCE) is None
