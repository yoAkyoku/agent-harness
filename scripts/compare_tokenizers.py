"""Compare the pure-Python Qwen3.5 counter with the pinned llama.cpp library, string by string (Windows only).

    uv run python scripts/compare_tokenizers.py LIBRARY MODEL [--out report.json] [--seed 0]

LIBRARY is Ollama's libllama.dll (sha256 96a590dd…) and MODEL the Qwen3.5 9B GGUF blob (sha256 02d45dc1…). The
corpus is built from this repository's own text (documentation in English and Chinese, code, JSON event records,
CSV) cut at random lengths, the same pieces wrapped in the chat template, and hand-written edge cases (marks,
emoji, unusual whitespace, contractions, special tokens). Every string counts: the report lists each mismatch.
"""
import argparse
import ctypes as C
import json
import os
from pathlib import Path
import random
import sys
import time
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_harness._tokenize import Params  # noqa: E402
from agent_harness.exact_context import LIBRARY_SHA  # noqa: E402
from agent_harness.qwen35_tokenizer import Qwen35Tokenizer, read_vocabulary  # noqa: E402

EDGE_CASES = [
    "", " ", "  ", "\n", "\r\n", " \n ", "\t\tx", "a  b", "a \n\n b", "x 　y", " a", "\x85\x85",
    "don't", "DON'T", "we're", "they'LL", "I'M", "'s", "''", "' s", "'Re", "rock'n'roll",
    "é", "́e", "́", "กัน", "नमस्ते", "مرحبا بالعالم", "한국어 텍스트", "日本語のテキスト",
    "😀", "👍🏽", "👨‍👩‍👧", "🇹🇼", "a😀b", "‍", "﻿x", "x​y",
    "12345", "3.14159", "１２３", "Ⅻ", "½", "٣٤", "2026-11-18T10:00:00Z",
    "<|im_start|>user\nhi<|im_end|>\n", "<|im_start|><|im_end|>", "<think>\n\n</think>\n\n", "<|endoftext|>",
    "<|im_start", "im_end|>", "<<|im_start|>>",
    "{\"a\": [1, 2, {\"b\": \"中文\"}]}", "def f(x):\n    return x  # comment\n", "| a | b |\n|---|---|\n| 1 | 2 |",
    "!!!\n\n", "...", "--- \r\n", "x\r\r\ny", "   ", "\x00\x01\x1f", "\x1c\x1d", "퟿",
]


def corpus(seed: int) -> list[str]:
    rng = random.Random(seed)
    texts: list[str] = []
    for path in sorted(ROOT.rglob("*")):
        if any(part in {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache"} for part in path.parts):
            continue
        if path.suffix not in {".md", ".py", ".json", ".ndjson", ".csv", ".js", ".css", ".html", ".txt", ".yml", ".toml"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        position = 0
        while position < len(text) and len(texts) < 6000:
            size = rng.choice((16, 64, 256, 1024, 4096))
            texts.append(text[position:position + size])
            position += size + rng.randrange(0, 2048)
    wrapped = [f"<|im_start|>system\n{a}<|im_end|>\n<|im_start|>user\n{b}<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
               for a, b in zip(texts[::7], texts[3::7])]
    # Random code points across the assigned planes, mixed with ASCII, to reach characters no document here uses.
    planes = [(0x20, 0x7E), (0xA0, 0x2FFF), (0x3000, 0x9FFF), (0xA000, 0xD7FF), (0xE000, 0xFFFD), (0x10000, 0x1FAFF), (0x20000, 0x2FA1F)]
    # Most draw only characters Python's tables know, which must count exactly; the rest may hit newer ones,
    # which must be bounded.
    fuzz = []
    for number in range(3000):
        chars: list[str] = []
        length = rng.randrange(1, 40)
        while len(chars) < length:
            low, high = rng.choice(planes)
            char = chr(rng.randrange(low, high + 1)) if rng.random() < .7 else rng.choice(" \n'aZ09.,\t")
            if number % 6 == 0 or unicodedata.category(char) != "Cn":
                chars.append(char)
        fuzz.append("".join(chars))
    return EDGE_CASES + texts + wrapped + fuzz


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("library", type=Path)
    parser.add_argument("model", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--seed", type=int, default=0)
    arguments = parser.parse_args()
    import hashlib
    if hashlib.sha256(arguments.library.read_bytes()).hexdigest() != LIBRARY_SHA or C.sizeof(Params) != 80:
        raise SystemExit("This is not the pinned library.")
    started = time.monotonic()
    portable = Qwen35Tokenizer(read_vocabulary(arguments.model))
    loaded = time.monotonic() - started
    directory = os.add_dll_directory(str(arguments.library.parent))
    lib = C.CDLL(str(arguments.library))
    lib.llama_model_default_params.restype = Params
    lib.llama_model_load_from_file.argtypes = [C.c_char_p, Params]
    lib.llama_model_load_from_file.restype = C.c_void_p
    lib.llama_model_get_vocab.argtypes = [C.c_void_p]
    lib.llama_model_get_vocab.restype = C.c_void_p
    lib.llama_tokenize.argtypes = [C.c_void_p, C.c_char_p, C.c_int32, C.POINTER(C.c_int32), C.c_int32, C.c_bool, C.c_bool]
    lib.llama_tokenize.restype = C.c_int32
    lib.llama_model_free.argtypes = [C.c_void_p]
    params = lib.llama_model_default_params()
    params.vocab_only = True
    model = lib.llama_model_load_from_file(os.fsencode(arguments.model), params)
    vocab = lib.llama_model_get_vocab(model)
    strings = corpus(arguments.seed)
    mismatches, bounded, total_bytes, total_tokens, portable_seconds = [], 0, 0, 0, 0.0
    for text in strings:
        data = text.encode("utf-8")
        buffer = (C.c_int32 * (len(data) + 64))()
        reference = lib.llama_tokenize(vocab, data, len(data), buffer, len(buffer), True, True)
        tick = time.monotonic()
        try:
            ours, is_bound = portable.count_detail(text)
        except Exception as error:  # a crash is a mismatch too
            ours, is_bound = f"error: {error!r}", False
        portable_seconds += time.monotonic() - tick
        total_bytes += len(data)
        total_tokens += max(reference, 0)
        bounded += is_bound
        # A bounded count must not fall short; every other count must be equal.
        if not (ours == reference or (is_bound and isinstance(ours, int) and ours >= reference)):
            mismatches.append({"text": text[:300], "bytes": len(data), "reference": reference, "portable": ours, "bounded": is_bound})
    lib.llama_model_free(model)
    directory.close()
    report = {"strings": len(strings), "mismatches": len(mismatches), "bounded_strings": bounded, "utf8_bytes": total_bytes, "reference_tokens": total_tokens,
              "bytes_per_token": round(total_bytes / max(total_tokens, 1), 3), "vocabulary_load_seconds": round(loaded, 1),
              "portable_count_seconds": round(portable_seconds, 2), "examples": mismatches[:40]}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if arguments.out:
        arguments.out.write_text(text, encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "examples"}))
    for example in mismatches[:10]:
        print(json.dumps(example, ensure_ascii=True)[:400])
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
