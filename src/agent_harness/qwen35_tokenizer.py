"""A pure-Python token counter for the Qwen3.5 vocabulary, read from the model's own GGUF file.

It reproduces what llama.cpp does for `tokenizer.ggml.model = gpt2`, `tokenizer.ggml.pre = qwen35` with
`add_special` and `parse_special` on: special tokens are split out first, the rest is cut by the Qwen3.5
pre-tokenizer pattern, byte-encoded and merged by BPE rank. No model weights are loaded and nothing is downloaded;
only the GGUF header (vocabulary and merges) is read.

The pattern, as llama.cpp writes it for this pre-tokenizer:

    (?:'[sS]|'[tT]|'[rR][eE]|'[vV][eE]|'[mM]|'[lL][lL]|'[dD])|[^\\r\\n\\p{L}\\p{N}]?[\\p{L}\\p{M}]+|\\p{N}
    | ?[^\\s\\p{L}\\p{M}\\p{N}]+[\\r\\n]*|\\s*[\\r\\n]+|\\s+(?!\\S)|\\s+

Python's `re` has no `\\p{..}` classes, so `pieces` matches it by hand, alternative by alternative, with the same
backtracking outcome. Agreement with llama.cpp is not assumed: `scripts/compare_tokenizers.py` checks it string by
string against the pinned library, and the adapter checks every count against the prompt size Ollama reports.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import struct
from typing import BinaryIO, Iterator
import unicodedata

# Unicode White_Space, which is what llama.cpp's tables call whitespace (Python's str.isspace differs on U+001C-1F).
WHITESPACE = frozenset("\t\n\v\f\r \x85\xa0            "
                       "    　")
NORMAL, UNKNOWN, CONTROL, USER_DEFINED = 1, 2, 3, 4
_SCALARS = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
_LIMIT = 1 << 20  # largest count or length accepted from the file
_CONTRACTIONS = frozenset(a + b for pair in ("re", "ve", "ll") for a in (pair[0], pair[0].upper()) for b in (pair[1], pair[1].upper()))


def _byte_encoder() -> dict[int, str]:
    """GPT-2's reversible map from bytes to printable characters."""
    printable = list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1)) + list(range(ord("®"), ord("ÿ") + 1))
    codes = printable[:]
    extra = 0
    for byte in range(256):
        if byte not in printable:
            printable.append(byte)
            codes.append(256 + extra)
            extra += 1
    return {byte: chr(code) for byte, code in zip(printable, codes)}


BYTE_ENCODER = _byte_encoder()


class _Reader:
    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream

    def scalar(self, fmt: str) -> int | float | bool:
        size = struct.calcsize(fmt)
        data = self.stream.read(size)
        if len(data) != size:
            raise ValueError("Truncated GGUF header.")
        value: int | float | bool = struct.unpack(fmt, data)[0]
        return value

    def count(self) -> int:
        value = self.scalar("<Q")
        if not isinstance(value, int) or value > _LIMIT:
            raise ValueError("Implausible GGUF length.")
        return value

    def string(self) -> str:
        size = self.count()
        data = self.stream.read(size)
        if len(data) != size:
            raise ValueError("Truncated GGUF string.")
        return data.decode("utf-8")

    def value(self, kind: int) -> object:
        if kind in _SCALARS:
            return self.scalar(_SCALARS[kind])
        if kind == 8:
            return self.string()
        if kind == 9:
            element = self.scalar("<I")
            assert isinstance(element, int)
            return [self.value(element) for _ in range(self.count())]
        raise ValueError(f"Unknown GGUF value type {kind}.")


def read_vocabulary(path: Path) -> dict[str, object]:
    """The `tokenizer.ggml.*` entries of a GGUF file's header; tensors are never read."""
    with path.open("rb") as stream:
        reader = _Reader(stream)
        if stream.read(4) != b"GGUF" or reader.scalar("<I") != 3:
            raise ValueError("Not a GGUF version 3 file.")
        reader.scalar("<Q")  # tensor count
        entries = reader.count()
        found: dict[str, object] = {}
        for _ in range(entries):
            key = reader.string()
            kind = reader.scalar("<I")
            assert isinstance(kind, int)
            value = reader.value(kind)
            if key.startswith("tokenizer.ggml."):
                found[key] = value
    return found


def _category(char: str) -> str:
    return unicodedata.category(char)[0]


def pieces(text: str) -> Iterator[str]:
    """Split text like the Qwen3.5 pre-tokenizer pattern (see the module docstring)."""
    size, i = len(text), 0
    while i < size:
        char = text[i]
        kind = _category(char)
        after = text[i + 1] if i + 1 < size else ""
        # 1. English contractions, any case.
        if char == "'" and after:
            if after in "sStT":
                yield text[i:i + 2]
                i += 2
                continue
            if text[i + 1:i + 3] in _CONTRACTIONS:
                yield text[i:i + 3]
                i += 3
                continue
            if after in "mMdD":
                yield text[i:i + 2]
                i += 2
                continue
        # 2. [^\r\n\p{L}\p{N}]?[\p{L}\p{M}]+
        start = i
        if char not in "\r\n" and kind not in "LN" and after and _category(after) in "LM":
            j = i + 1
        elif kind in "LM":
            j = i
        else:
            j = -1
        if j >= 0:
            while j < size and _category(text[j]) in "LM":
                j += 1
            yield text[start:j]
            i = j
            continue
        # 3. \p{N}, one character at a time.
        if kind == "N":
            yield char
            i += 1
            continue
        # 4. ` ?[^\s\p{L}\p{M}\p{N}]+[\r\n]*`
        def other(c: str) -> bool:
            return c not in WHITESPACE and _category(c) not in "LMN"
        if (char == " " and after and other(after)) or other(char):
            j = i + 1 if char == " " else i
            while j < size and other(text[j]):
                j += 1
            while j < size and text[j] in "\r\n":
                j += 1
            yield text[i:j]
            i = j
            continue
        # Whitespace from here on: the run of it starting at i.
        end = i
        while end < size and text[end] in WHITESPACE:
            end += 1
        if end == i:  # not reachable for any character, kept as a guard
            yield char
            i += 1
            continue
        # 5. \s*[\r\n]+ : up to and including the run's last line break.
        last_break = max(text.rfind("\r", i, end), text.rfind("\n", i, end))
        if last_break >= 0:
            yield text[i:last_break + 1]
            i = last_break + 1
            continue
        # 6. \s+(?!\S) : the run, minus its last character when something follows it.
        if end == size:
            yield text[i:end]
            i = end
            continue
        if end - 1 > i:
            yield text[i:end - 1]
            i = end - 1
            continue
        # 7. \s+
        yield text[i:end]
        i = end


class Qwen35Tokenizer:
    def __init__(self, entries: dict[str, object]) -> None:
        tokens, types, merges = (entries.get(f"tokenizer.ggml.{name}") for name in ("tokens", "token_type", "merges"))
        if (entries.get("tokenizer.ggml.model") != "gpt2" or entries.get("tokenizer.ggml.pre") != "qwen35"
                or entries.get("tokenizer.ggml.add_bos_token") is not False
                or not isinstance(tokens, list) or not isinstance(types, list) or not isinstance(merges, list)
                or len(tokens) != len(types) or not all(isinstance(token, str) for token in tokens)):
            raise ValueError("Not a Qwen3.5 GPT-2 style vocabulary.")
        self.size = len(tokens)
        self.ids = {token: index for index, token in enumerate(tokens)}
        self.ranks: dict[str, int] = {}
        for rank, merge in enumerate(merges):
            if not isinstance(merge, str) or merge.count(" ") != 1:
                raise ValueError("Malformed merge.")
            self.ranks.setdefault(merge, rank)
        # llama.cpp splits out control, user-defined and unknown tokens first, longest text first.
        self.special = sorted((token for token, kind in zip(tokens, types) if kind in (UNKNOWN, CONTROL, USER_DEFINED) and token),
                              key=len, reverse=True)
        self._word = lru_cache(maxsize=65536)(self._count_word)

    def _fragments(self, text: str) -> list[tuple[bool, str]]:
        fragments: list[tuple[bool, str]] = [(False, text)]
        for special in self.special:
            if special not in text:
                continue
            split: list[tuple[bool, str]] = []
            for is_special, fragment in fragments:
                if is_special or special not in fragment:
                    split.append((is_special, fragment))
                    continue
                parts = fragment.split(special)
                for index, part in enumerate(parts):
                    if index:
                        split.append((True, special))
                    if part:
                        split.append((False, part))
            fragments = split
        return fragments

    def _count_word(self, word: str) -> int:
        symbols = [BYTE_ENCODER[byte] for byte in word.encode("utf-8")]
        while len(symbols) > 1:
            best, where = None, -1
            for index in range(len(symbols) - 1):
                rank = self.ranks.get(symbols[index] + " " + symbols[index + 1])
                if rank is not None and (best is None or rank < best):
                    best, where = rank, index
            if best is None:
                break
            symbols[where:where + 2] = [symbols[where] + symbols[where + 1]]
        # A merged symbol missing from the vocabulary falls back to its bytes.
        return sum(1 if symbol in self.ids else len(symbol) for symbol in symbols)

    def count(self, text: str) -> int:
        return self.count_detail(text)[0]

    def count_detail(self, text: str) -> tuple[int, bool]:
        """The token count, and whether any part of it is the byte bound instead of an exact count.

        Python's character tables (Unicode 15.0 in 3.12) predate llama.cpp's: a character unassigned here may be a
        letter there (CJK Extension I, for one), which changes how the text is cut. A fragment holding such a
        character is counted as its UTF-8 length, which no tokenization can exceed, so the count never falls short.
        """
        total, bounded = 0, False
        for is_special, fragment in self._fragments(text):
            if is_special:
                total += 1
            elif any(unicodedata.category(char) == "Cn" for char in fragment):
                total += len(fragment.encode("utf-8"))
                bounded = True
            else:
                total += sum(self._word(piece) for piece in pieces(fragment))
        return total, bounded
