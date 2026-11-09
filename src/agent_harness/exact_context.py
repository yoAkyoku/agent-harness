"""Verified text-only Qwen35 rendering and isolated vocab-only input accounting."""
from __future__ import annotations

import hashlib
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from .contracts import RunFailure

LIBRARY_SHA = "96a590dd1a1a13c25d3d40dbfba7e5114fd691be4d318a1a7c0b7ee6a02636df"
MODEL_SHA = "02d45dc1cf451ba2475ac33b301c2dd8f985abe4c182ce04a1f2f5bf0260278d"
MODEL_DIGEST = "56671c2ab9385f9cfcb404638e32cd62d88e3501d44822208363c010179a3c90"
PROFILE = "ollama-0.40.10-qwen35-exact-vocab-only"
GO_SPACE = "\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"


def rendered_prompt(body: dict[str, Any]) -> str:
    if body.get("tools") != [] or body.get("reasoning_effort") != "none":
        raise ValueError("Exact accounting supports nonthinking JSON tools only.")
    messages = body["messages"]
    if not isinstance(messages, list) or not messages:
        raise ValueError("Missing messages.")
    parts: list[str] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or set(message) - {"role", "content"}:
            raise ValueError("Unsupported message fields.")
        role, content = message.get("role"), message.get("content")
        if role not in {"system", "user", "assistant"} or not isinstance(content, (str, type(None))):
            raise ValueError("Unsupported text message.")
        content = (content or "").strip(GO_SPACE)
        if role == "assistant" and "</think>" in content:
            content = content.split("</think>", 1)[1].lstrip("\n")
        last = index == len(messages) - 1
        prefill = last and role == "assistant"
        parts.append("<|im_start|>" + role + "\n" + content)
        if not prefill:
            parts.append("<|im_end|>\n")
        if last and not prefill:
            parts.append("<|im_start|>assistant\n<think>\n\n</think>\n\n")
    result = "".join(parts)
    if len(result.encode("utf-8")) > 1048576:
        raise ValueError("Tokenizer input too large.")
    return result


def fingerprint(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


class ExactCounter:
    def __init__(self, library: Path, model: Path) -> None:
        try:
            if os.name != "nt" or sys.maxsize <= 2**32:
                raise ValueError("Unsupported platform.")
            self.library, self.model = library.resolve(strict=True), model.resolve(strict=True)
            verified: list[tuple[int, int, int, int]] = []
            for path, expected in ((self.library, LIBRARY_SHA), (self.model, MODEL_SHA)):
                if str(path).startswith("\\\\") or not path.is_file():
                    raise ValueError("Tokenizer assets must be local files.")
                before = fingerprint(path)
                with path.open("rb") as stream:
                    actual = hashlib.file_digest(stream, "sha256").hexdigest()
                if actual != expected or fingerprint(path) != before:
                    raise ValueError("Tokenizer asset identity changed.")
                verified.append(before)
            self.identities = verified[0], verified[1]
        except (OSError, ValueError) as error:
            raise RunFailure("exact_context_unavailable", "The pinned local tokenizer assets could not be verified.") from error

    def _prompt(self, body: dict[str, Any]) -> str:
        if (fingerprint(self.library), fingerprint(self.model)) != self.identities:
            raise ValueError("Tokenizer assets changed.")
        return rendered_prompt(body)

    async def budget(self, body: dict[str, Any], capacity: int, reserve: int) -> dict[str, Any]:
        try:
            prompt = await asyncio.to_thread(self._prompt, body)
            creationflags = 0
            if sys.platform == "win32":
                creationflags = subprocess.CREATE_NO_WINDOW
            process = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).with_name("_tokenize.py")),
                str(self.library), str(self.model), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags)
            try:
                async with asyncio.timeout(15):
                    stdout, _ = await process.communicate(json.dumps({"prompt":prompt}).encode())
            finally:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                # Drain both pipes as well as reaping: wait() alone can block
                # after a cancelled communicate() leaves a full pipe paused.
                reaped = asyncio.create_task(process.communicate())
                # Preserve cleanup even if shutdown repeats a cancellation.
                while not reaped.done():
                    try:
                        await asyncio.shield(reaped)
                    except asyncio.CancelledError:
                        continue
            if process.returncode != 0 or len(stdout) > 2048:
                raise ValueError("Tokenizer process failed.")
            value = json.loads(stdout)
            count = value["input_tokens"]
            if (type(count) is not int or not 0 <= count <= len(prompt.encode()) + 64
                    or value.get("library_sha256") != LIBRARY_SHA
                    or await asyncio.to_thread(lambda: (fingerprint(self.library), fingerprint(self.model))) != self.identities):
                raise ValueError("Invalid tokenizer receipt.")
            return {"method":"qwen35_exact_vocab_only", "input_tokens":count,
                "rendered_prompt_sha256":hashlib.sha256(prompt.encode()).hexdigest(),
                "rendered_prompt_utf8_bytes":len(prompt.encode()),"output_token_reserve":reserve,
                "upper_bound_tokens":count + reserve,"capacity_tokens":capacity}
        except (OSError, ValueError, KeyError, TypeError, TimeoutError) as error:
            raise RunFailure("exact_context_unavailable", "Complete input token accounting failed; no generation was admitted.") from error
