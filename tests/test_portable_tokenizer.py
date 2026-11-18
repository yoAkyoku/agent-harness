"""The pure-Python Qwen3.5 token counter: its pre-tokenizer, its GGUF reader, and how the adapter uses a count.

Agreement with llama.cpp is measured by `scripts/compare_tokenizers.py` against the pinned library (Windows). Here,
the splitting rules are checked by hand, a tiny synthetic GGUF exercises the reader and BPE, and, when the real
model file is present (any platform), counts recorded from the pinned library are reproduced.
"""
import asyncio
import json
import os
from pathlib import Path
import struct

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_harness.contracts import Decision, RunFailure
from agent_harness.exact_context import PortableCounter
from agent_harness.model import ModelAdapter, ModelSettings
from agent_harness.qwen35_tokenizer import BYTE_ENCODER, Qwen35Tokenizer, pieces, read_vocabulary
from agent_harness.web import create_app
from test_completion import CompletionPeer
from test_json_decisions import json_reply
from test_session_versions import qwen_metadata
from test_workbench import create_task, finish

MODEL = Path(os.environ.get("HARNESS_TEST_TOKENIZER_MODEL", ""))
NAME = "qwen3.5:9b-q4_K_M"
DIGEST = "56671c2ab9385f9cfcb404638e32cd62d88e3501d44822208363c010179a3c90"
needs_model = pytest.mark.skipif(not MODEL.is_file(), reason="Set HARNESS_TEST_TOKENIZER_MODEL to the Qwen3.5 9B GGUF blob; nothing is downloaded.")


@pytest.mark.parametrize(("text", "expected"), [
    ("Hello world", ["Hello", " world"]),
    ("don't STOP'LL 'x", ["don", "'t", " STOP", "'LL", " '", "x"]),
    ("a  b", ["a", " ", " b"]),
    ("a \n\n b", ["a", " \n\n", " b"]),
    ("x   ", ["x", "   "]),
    ("abc123", ["abc", "1", "2", "3"]),
    ("été", ["été"]),  # marks stay with their letters
    ("́x", ["́x"]),
    ("中文，測試。\n下一行", ["中文", "，測試", "。\n", "下一行"]),  # one non-letter may lead a word
    ("  !!\n\nx", [" ", " !!\n\n", "x"]),
    ("\tdef", ["\tdef"]),
    ("\r\nx", ["\r\n", "x"]),
])
def test_pieces_follow_the_qwen35_pattern(text: str, expected: list[str]) -> None:
    assert list(pieces(text)) == expected
    assert "".join(pieces(text)) == text


def _gguf(path: Path, entries: dict[str, object]) -> Path:
    def string(value: str) -> bytes:
        data = value.encode("utf-8")
        return struct.pack("<Q", len(data)) + data

    def item(value: object) -> tuple[int, bytes]:
        if isinstance(value, bool):
            return 7, struct.pack("<?", value)
        if isinstance(value, int):
            return 5, struct.pack("<i", value)
        if isinstance(value, str):
            return 8, string(value)
        assert isinstance(value, list)
        kind = 8 if isinstance(value[0], str) else 5
        return 9, struct.pack("<IQ", kind, len(value)) + b"".join(item(element)[1] for element in value)

    body = b"".join(string(key) + struct.pack("<I", item(value)[0]) + item(value)[1] for key, value in entries.items())
    path.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, len(entries)) + body + b"\0" * 64)
    return path


def _tiny_vocabulary(tmp_path: Path) -> Path:
    tokens = list(BYTE_ENCODER.values()) + ["he", "hel", "hell", "hello", "Ġw", "Ġwo", "<|im_start|>", "<|im_end|>"]
    types = [1] * (len(tokens) - 2) + [3, 3]
    merges = ["h e", "he l", "hel l", "hell o", "Ġ w", "Ġw o"]
    return _gguf(tmp_path / "tiny.gguf", {"general.architecture": "qwen35", "tokenizer.ggml.model": "gpt2",
                                          "tokenizer.ggml.pre": "qwen35", "tokenizer.ggml.tokens": tokens,
                                          "tokenizer.ggml.token_type": types, "tokenizer.ggml.merges": merges,
                                          "tokenizer.ggml.add_bos_token": False})


def test_a_synthetic_vocabulary_is_read_and_merged_by_rank(tmp_path: Path) -> None:
    tokenizer = Qwen35Tokenizer(read_vocabulary(_tiny_vocabulary(tmp_path)))
    assert tokenizer.count("hello") == 1
    assert tokenizer.count(" world") == 4  # "Ġwo" then r, l, d as bytes
    assert tokenizer.count("<|im_start|>hello<|im_end|>") == 3  # special tokens are one token each
    assert tokenizer.count("中") == 3  # three UTF-8 bytes, no merges
    assert tokenizer.count_detail("hello") == (1, False)


def test_text_with_characters_unknown_to_python_is_bounded_by_its_bytes(tmp_path: Path) -> None:
    """U+2EE4C (CJK Extension I) is a letter to llama.cpp but unassigned in Python 3.12's tables."""
    tokenizer = Qwen35Tokenizer(read_vocabulary(_tiny_vocabulary(tmp_path)))
    text = "hello \U0002EE4C!"
    count, bounded = tokenizer.count_detail("<|im_start|>" + text)
    assert bounded and count == 1 + len(text.encode("utf-8"))


def test_a_vocabulary_of_another_tokenizer_is_refused(tmp_path: Path) -> None:
    entries = read_vocabulary(_tiny_vocabulary(tmp_path))
    with pytest.raises(ValueError):
        Qwen35Tokenizer({**entries, "tokenizer.ggml.pre": "qwen2"})
    not_gguf = tmp_path / "not.gguf"
    not_gguf.write_bytes(b"PK\x03\x04 not a model")
    with pytest.raises(ValueError):
        read_vocabulary(not_gguf)


def test_settings_accept_the_model_file_alone_but_not_the_library_alone(tmp_path: Path) -> None:
    model = tmp_path / "model.gguf"
    settings = ModelSettings.local(NAME, decision_protocol="json_tools", exact_tokenizer_model=model)
    assert settings.exact_tokenizer_library is None
    with pytest.raises(ValueError, match="matching model file"):
        ModelSettings.local(NAME, decision_protocol="json_tools", exact_tokenizer_library=tmp_path / "lib.dll")
    with pytest.raises(ValueError, match="nonthinking JSON tools"):
        ModelSettings.local(NAME, exact_tokenizer_model=model)


class _Counter:
    def __init__(self, count: int) -> None:
        self.count = count

    async def counted_tokens(self, body: dict) -> int:
        return self.count


def test_a_reply_reporting_more_prompt_tokens_than_counted_is_discarded() -> None:
    reply = json_reply("plan_update", {"steps": ["Read"]})
    payload = json.loads(reply.content)

    async def decide(reported: int) -> Decision:
        body = {**payload, "usage": {"prompt_tokens": reported, "completion_tokens": 5}}
        adapter = ModelAdapter(httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))),
                               ModelSettings.local(NAME, stream=False, decision_protocol="json_tools"), transport_fixture=True)
        adapter.exact_counter = _Counter(100)  # type: ignore[assignment]
        tools = [{"type": "function", "function": {"name": "plan_update", "description": "", "parameters": {
            "type": "object", "properties": {"steps": {"type": "array", "items": {"type": "string"}}}, "required": ["steps"]}}}]
        return await adapter.decide([{"role": "user", "content": "x"}], tools)

    assert asyncio.run(decide(100)).calls  # equal, or smaller after prefix caching, is fine
    assert asyncio.run(decide(60)).calls
    with pytest.raises(RunFailure) as failure:
        asyncio.run(decide(101))
    assert failure.value.code == "exact_context_mismatch"
    assert failure.value.diagnostics == {"counted_tokens": 100, "reported_prompt_tokens": 101}


# Recorded from the pinned libllama.dll (llama_tokenize with add_special and parse_special) on 2026-11-18.
GOLDEN = [
    ("Hello, world!", 4),
    ("整理這個改版的目標、已知風險，以及三週訂單數的變化，存成一份附引用的摘要。", 24),
    ("<|im_start|>system\nYou are careful.<|im_end|>\n<|im_start|>user\n讀取 brief.md 並回答。<|im_end|>\n"
     "<|im_start|>assistant\n<think>\n\n</think>\n\n", 29),
    ('{"name": "artifact_write", "arguments": {"content": "# 摘要 [brief.md E1]", "citations": ["E1"]}}', 31),
    ("week,orders,exceptions\n1,120,12\n2,150,9\n3,180,9\n", 31),
    ("They'll say it's fine.  \n\n\tdef f(x):\n        return x  # ok\r\n", 20),
    ("é नमस्ते 😀👍🏽 ½ １２３", 19),
]


@needs_model
def test_the_real_vocabulary_reproduces_counts_recorded_from_llama_cpp() -> None:
    tokenizer = Qwen35Tokenizer(read_vocabulary(MODEL))
    assert tokenizer.size == 248320
    assert [tokenizer.count(text) for text, _ in GOLDEN] == [count for _, count in GOLDEN]


@needs_model
def test_a_run_counts_every_request_exactly_without_the_library(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "facts.md").write_bytes("負責人：Mei\n".encode())
    candidate = {"name": "owner.md", "content": "負責人：Mei [facts.md E1]", "citations": ["E1"]}
    peer = CompletionPeer([json_reply("plan_update", {"steps": ["Read and save owner"]}),
                           json_reply("source_read", {"path": "facts.md", "labels": []}),
                           json_reply("artifact_draft", candidate), json_reply("artifact_write", candidate),
                           httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                               "role": "assistant", "content": '{"reply":"Saved."}'}}]})])

    async def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.10"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": NAME, "digest": DIGEST}]})
        if request.url.path == "/api/show":
            return httpx.Response(200, json=qwen_metadata())
        return await peer(request)

    settings = ModelSettings.local(NAME, stream=False, decision_protocol="json_tools", exact_tokenizer_model=MODEL)
    with TestClient(create_app(tmp_path / "store", model_settings=settings, model_transport=httpx.MockTransport(transport))) as client:
        assert client.get("/api/config").json()["model"]["context_profile"] == "ollama-0.40.10-qwen35-exact-portable"
        run, events = finish(client, create_task(client, source, task="讀取資料並保存負責人。", publication_workflow="draft_review"))
    assert run["status"] == "completed", run["error"]
    budgets = [event["data"]["context_budget"] for event in events if event["kind"] == "model.requested"]
    assert len(budgets) == 5
    assert all(budget["method"] == "qwen35_exact_portable" and not budget["byte_bounded_fragments"] for budget in budgets)
    # Far below the byte bound: the rendered prompt is several times larger in bytes than in tokens.
    assert all(budget["input_tokens"] * 2 < budget["rendered_prompt_utf8_bytes"] for budget in budgets)


def test_a_wrong_model_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RunFailure) as failure:
        PortableCounter(_tiny_vocabulary(tmp_path))
    assert failure.value.code == "exact_context_unavailable"
