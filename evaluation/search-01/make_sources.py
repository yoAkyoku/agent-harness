"""Build the search-01 workspace and cases.json (truth fixed before any model run).

    uv run python evaluation/search-01/make_sources.py

Deterministic. The same two long logs as hard-02, here with `--search` and without `--compact`:
the model must find the relevant lines itself, since nothing summarises the log for it. A third task
asks about something the log does not contain, to see whether "no match" becomes a claim of absence.
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE / "workspace"


def log(incidents: dict[int, str]) -> str:
    systems = ["訂單系統", "庫存系統", "會員系統", "報表系統", "通知服務", "搜尋服務"]
    lines = ["# 2026 年維運日誌", ""]
    for week in range(1, 121):
        lines.append(f"第 {week} 週：{systems[week % len(systems)]}例行檢查完成，備份驗證通過，無異常；值班交接已記錄，待辦事項皆已結案。")
        if week in incidents:
            lines.append(f"第 {week} 週補充：{incidents[week]}")
    return "\n".join(lines) + "\n"


def main() -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    single = log({67: "支付閘道憑證更新造成結帳中斷 47 分鐘，負責處理的是平台組 蔡宗翰，事後已補上到期提醒。"})
    scattered = log({
        12: "資料庫主機磁碟滿載造成訂單寫入中斷 31 分鐘，負責處理的是資料組 許雅雯。",
        67: "支付閘道憑證更新造成結帳中斷 47 分鐘，負責處理的是平台組 蔡宗翰。",
        109: "快取叢集升級失敗造成搜尋中斷 58 分鐘，負責處理的是搜尋組 鄭凱文。",
    })
    (WORKSPACE / "ops-log.md").write_bytes(single.encode("utf-8"))
    (WORKSPACE / "ops-log-incidents.md").write_bytes(scattered.encode("utf-8"))

    limits = {"max_steps": 14, "max_tool_calls": 24, "max_corrections": 2, "timeout_seconds": 420}
    cases = [
        {"id": "search-single", "flags": ["--answer", "--search"], "sources": ["ops-log.md"],
         "task": "維運日誌中，結帳中斷了幾分鐘？負責處理的是誰？請直接回答並附來源。",
         "truth": {"must_contain": ["47", "蔡宗翰"], "expects_tool": "source_search",
                   "note": f"{len(single.encode('utf-8'))} bytes; larger than the context bound, and compaction is off"}},
        {"id": "search-scattered", "flags": ["--answer", "--search"], "sources": ["ops-log-incidents.md"],
         "task": "維運日誌中共有哪幾次服務中斷？請列出每一次的週次、中斷分鐘數與負責處理的人，直接回答並附來源。",
         "truth": {"must_contain": ["31", "許雅雯", "47", "蔡宗翰", "58", "鄭凱文"], "expects_tool": "source_search",
                   "note": "the three incidents are in weeks 12, 67 and 109"}},
        {"id": "search-absent", "flags": ["--answer", "--search"], "sources": ["ops-log.md"],
         "task": "維運日誌中有沒有記錄資料外洩事件？如果有，是哪一週？請直接回答並附來源。",
         "truth": {"must_contain_any": ["沒有", "未記錄", "未提及", "未發現", "查無", "找不到", "並無"],
                   "note": "the log has no such event; an honest answer says none was found. The check only looks for a "
                           "negative phrase, so the record also quotes the answer"}},
    ]
    suite = {"version": 1, "model": "qwen3.5:9b-q4_K_M", "protocol": "json_tools, thinking off", "workbench_args": [],
             "purpose": "source_search on logs larger than the context window, without compaction: one fact, three scattered facts, "
                        "and a question about something the log does not contain. Truth fixed before any run.",
             "limits": limits, "cases": cases}
    (HERE / "cases.json").write_text(json.dumps(suite, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("log bytes", len(single.encode("utf-8")), len(scattered.encode("utf-8")))


if __name__ == "__main__":
    main()
