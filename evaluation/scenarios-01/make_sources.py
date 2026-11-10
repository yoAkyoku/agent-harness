"""Build the scenarios-01 workspace and cases.json (truth computed from the data, before any model run).

    uv run python evaluation/scenarios-01/make_sources.py

Deterministic: the same files and truth on every run.
"""
from __future__ import annotations

import csv
from decimal import Decimal
import io
import json
from pathlib import Path
import random
import statistics

from openpyxl import Workbook
from pptx import Presentation

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE / "workspace"


def write(name: str, data: bytes) -> None:
    path = WORKSPACE / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def main() -> None:
    rng = random.Random(20261110)

    policy = ("# 退款政策\n\n生效日期：2026-10-01\n\n- 退款期限：收到商品後 30 天內\n- 超過 5000 元的退款核准人：財務主管 林怡君\n"
              "- 一般退款處理人：客服組 陳柏宇\n- 退款方式：原付款方式退回\n")
    write("policy.md", policy.encode("utf-8"))

    workbook = Workbook()
    q1 = workbook.active
    q1.title = "Q1"
    q2 = workbook.create_sheet("Q2")
    totals = {}
    for sheet in (q1, q2):
        sheet.append(["region", "amount"])
        values = [rng.randint(100, 900) for _ in range(12)]
        for index, value in enumerate(values):
            sheet.append([["north", "south", "east", "west"][index % 4], value])
        totals[sheet.title] = sum(values)
    buffer = io.BytesIO()
    workbook.save(buffer)
    write("sales.xlsx", buffer.getvalue())

    deck = Presentation()
    slides = [("專案檢討", ["2026 Q3 系統移轉"]),
              ("主要風險", ["資料遷移延誤", "供應商合約到期", "測試人力不足"]),
              ("下一步", ["10 月底前完成遷移演練"])]
    for title, lines in slides:
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = title
        slide.placeholders[1].text = "\n".join(lines)
    buffer = io.BytesIO()
    deck.save(buffer)
    write("review.pptx", buffer.getvalue())

    amounts = [rng.randint(50, 2000) for _ in range(500)]
    out = io.StringIO()
    rows = csv.writer(out, lineterminator="\n")
    rows.writerow(["order_id", "amount"])
    for index, amount in enumerate(amounts, 1):
        rows.writerow([f"A{index:04d}", amount])
    write("orders.csv", out.getvalue().encode("utf-8"))
    mean = Decimal(sum(amounts)) / Decimal(len(amounts))
    median = statistics.median(amounts)

    write("web-notes.md", "# 待查網址\n\n- https://example.com/\n".encode("utf-8"))

    limits = {"max_steps": 12, "max_tool_calls": 20, "max_corrections": 2}
    cases = [
        {"id": "answer-policy", "scenario": "B answer mode", "flags": ["--answer"], "sources": ["policy.md"],
         "task": "退款期限是幾天？超過 5000 元的退款由誰核准？請直接回答並附來源。",
         "truth": {"must_contain": ["30", "林怡君"], "artifacts": 0}},
        {"id": "excel-sheet-sum", "scenario": "A Excel", "flags": ["--answer"], "sources": ["sales.xlsx"],
         "task": "sales.xlsx 的 Q2 工作表中，amount 欄的總和是多少？請直接回答並附來源。",
         "truth": {"must_contain": [str(totals["Q2"])], "not_contain": [str(totals["Q1"])], "artifacts": 0}},
        {"id": "pptx-risks", "scenario": "A PowerPoint", "flags": ["--answer"], "sources": ["review.pptx"],
         "task": "簡報列出的主要風險有哪些？請直接回答並附來源。",
         "truth": {"must_contain": ["資料遷移延誤", "供應商合約到期", "測試人力不足"], "artifacts": 0}},
        {"id": "large-csv-mean", "scenario": "C large table", "flags": ["--answer"], "sources": ["orders.csv"],
         "task": "orders.csv 共有 500 筆訂單，amount 欄的平均值是多少？請用工具計算全部資料，直接回答並附來源。",
         "truth": {"must_contain_number": str(mean), "artifacts": 0}},
        {"id": "web-example", "scenario": "D web_fetch", "flags": ["--answer", "--web"], "sources": ["web-notes.md"],
         "task": "請讀取 web-notes.md 中的網址，說明那個網頁表示這個網域是做什麼用的，直接回答並以網址引用。",
         "truth": {"must_contain_any": ["documentation", "文件", "範例", "example"], "cites": "https://example.com/",
                   "artifacts": 0}},
        {"id": "python-median", "scenario": "E python sandbox", "flags": ["--answer", "--python"], "sources": ["orders.csv"],
         "task": "請用 Python 計算 orders.csv 中 amount 欄的中位數，直接回答並附來源。",
         "truth": {"must_contain_number": str(median), "cites": "python_run", "artifacts": 0}},
        {"id": "unattended-approval", "scenario": "F unattended", "flags": ["--require-publish-approval", "--unattended"],
         "sources": ["policy.md"], "task": "請把退款政策整理成重點摘要，保存為 summary.md 並附來源。",
         "truth": {"exit_code": 5, "artifacts": 0}},
    ]
    suite = {"version": 1, "model": "qwen3.5:9b-q4_K_M", "protocol": "json_tools, thinking off",
             "purpose": "One real-model run per usage scenario added on 2026-11-10 (answer mode, Excel, PowerPoint, "
                        "large tables, web_fetch, the Python sandbox, unattended headless). Truth fixed before any run.",
             "limits": limits, "cases": cases}
    (HERE / "cases.json").write_text(json.dumps(suite, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Q1", totals["Q1"], "Q2", totals["Q2"], "mean", mean, "median", median)


if __name__ == "__main__":
    main()
