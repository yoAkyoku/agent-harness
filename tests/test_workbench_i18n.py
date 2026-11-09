"""Static checks that the workbench UI is fully covered by the English dictionary in i18n.js."""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "agent_harness" / "static"
SCRIPTS = ("app.js", "memory.js", "run-stream.js")
CJK = re.compile("[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")
PLACEHOLDER = re.compile(r"\{(\w+)\}")
# t("...") or t(`...`) as a call, not a method such as .format( or an identifier ending in t.
T_CALL = re.compile(r"""(?<![\w$.])t\(\s*("(?:[^"\\\n]|\\.)*"|`(?:[^`\\]|\\.)*`)""")
TRANSLATED_ATTRIBUTES = ("placeholder", "aria-label", "title")
VOID_ELEMENTS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


def read(name: str) -> str:
    return (STATIC / name).read_bytes().decode("utf-8")


def english() -> dict[str, str]:
    match = re.search(r"^const EN = (\{.*?^\});$", read("i18n.js"), re.DOTALL | re.MULTILINE)
    assert match, "i18n.js must define EN as a JSON-compatible object literal"
    return json.loads(match.group(1))


def decode_literal(literal: str) -> str:
    body = literal[1:-1]
    if literal[0] == "`":
        assert "${" not in body, f"t() keys must be static, found {literal}"
        body = body.replace('"', '\\"').replace("\n", "\\n")
    return json.loads(f'"{body}"')


def strip_comments(source: str) -> str:
    """Blank out // and /* */ comments while leaving string and template literal contents intact."""
    out, i, n = [], 0, len(source)
    braces: list[int] = []  # brace depth inside each open template ${...}
    mode = "code"
    while i < n:
        char, pair = source[i], source[i:i + 2]
        if mode == "code":
            if pair == "//":
                end = source.find("\n", i)
                i = n if end < 0 else end
                continue
            if pair == "/*":
                end = source.find("*/", i + 2)
                i = n if end < 0 else end + 2
                continue
            if char in "'\"":
                mode = char
            elif char == "`":
                mode = "`"
            elif char == "{" and braces:
                braces[-1] += 1
            elif char == "}" and braces:
                if braces[-1] == 0:
                    braces.pop()
                    mode = "`"
                else:
                    braces[-1] -= 1
            out.append(char)
            i += 1
            continue
        if char == "\\":
            out.append(source[i:i + 2])
            i += 2
            continue
        if mode == "`" and pair == "${":
            braces.append(0)
            mode = "code"
            out.append(pair)
            i += 2
            continue
        if char == mode:
            mode = "code"
        out.append(char)
        i += 1
    return "".join(out)


def script_keys() -> dict[str, set[str]]:
    keys: dict[str, set[str]] = {}
    for name in SCRIPTS:
        for literal in T_CALL.findall(strip_comments(read(name))):
            keys.setdefault(decode_literal(literal), set()).add(name)
    return keys


class _HtmlText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, bool]] = []
        self.texts: list[str] = []

    def _skipped(self) -> bool:
        return bool(self.stack) and self.stack[-1][1]

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        skipped = self._skipped() or values.get("translate") == "no"
        if not skipped:
            self.texts.extend(values[name].strip() for name in TRANSLATED_ATTRIBUTES if values.get(name))
        if tag not in VOID_ELEMENTS:
            self.stack.append((tag, skipped))

    def handle_endtag(self, tag):
        while self.stack:
            if self.stack.pop()[0] == tag:
                break

    def handle_data(self, data):
        if not self._skipped() and data.strip():
            self.texts.append(data.strip())


def html_keys() -> set[str]:
    parser = _HtmlText()
    parser.feed(read("index.html"))
    return {text for text in parser.texts if CJK.search(text)}


def test_every_script_key_has_english() -> None:
    en = english()
    missing = sorted(f"{key!r} ({', '.join(sorted(files))})" for key, files in script_keys().items() if key not in en)
    assert not missing, "t() keys without English:\n" + "\n".join(missing)


def test_every_chinese_html_text_has_english() -> None:
    en = english()
    missing = sorted(key for key in html_keys() if key not in en)
    assert not missing, "index.html text without English:\n" + "\n".join(missing)


def test_scripts_have_no_untranslated_chinese() -> None:
    problems = []
    for name in SCRIPTS:
        code = T_CALL.sub("t(", strip_comments(read(name)))
        problems.extend(f"{name}: {line.strip()[:120]}" for line in code.splitlines() if CJK.search(line))
    assert not problems, "Chinese outside t() keys:\n" + "\n".join(problems)


def test_english_values_are_english_and_keep_placeholders() -> None:
    problems = []
    for key, value in english().items():
        if CJK.search(value):
            problems.append(f"CJK in English for {key!r}: {value!r}")
        if set(PLACEHOLDER.findall(key)) != set(PLACEHOLDER.findall(value)):
            problems.append(f"placeholders differ for {key!r}: {value!r}")
    assert not problems, "\n".join(problems)


def test_dictionary_has_no_unused_entries() -> None:
    used = set(script_keys()) | html_keys()
    unused = sorted(key for key in english() if key not in used)
    assert not unused, "EN entries no longer used:\n" + "\n".join(unused)


def test_modules_share_one_i18n_instance() -> None:
    imports = {name: re.findall(r'from "(/assets/i18n\.js[^"]*)"', read(name)) for name in SCRIPTS}
    assert all(len(found) == 1 for found in imports.values()), imports
    assert len({found[0] for found in imports.values()}) == 1, imports
