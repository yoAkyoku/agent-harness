"""Build the hard-01 workspace and cases.json (truth computed from the data, before any model run).

    uv run python evaluation/hard-01/make_sources.py

Deterministic: the same files and truth on every run. These tasks are harder than scenarios-01 on
purpose: two versions of a policy, similar roles, look-alike columns, two sheets, a field that is not
in the sources, a document larger than an 8K-token context, and a task that asks for delegation.
"""
from __future__ import annotations

import csv
from decimal import ROUND_HALF_UP, Decimal
import io
import json
from pathlib import Path
import random

from openpyxl import Workbook

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE / "workspace"


def write(name: str, data: bytes) -> None:
    path = WORKSPACE / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def percent(numerator: int, denominator: int) -> str:
    return str((Decimal(numerator) * 100 / Decimal(denominator)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def main() -> None:
    rng = random.Random(20261115)

    # Two versions of one policy: the question is about the one in force on a stated date.
    write("refund-policy-2025.md", (
        "# 退款政策（2025 年版）\n\n生效日期：2025-01-01\n失效日期：2026-06-30\n\n"
        "- 退款期限：收到商品後 30 天內\n- 退款核准人：財務主管 林怡君\n- 退款方式：原付款方式退回\n").encode("utf-8"))
    write("refund-policy-2026.md", (
        "# 退款政策（2026 年版）\n\n生效日期：2026-07-01\n\n"
        "- 退款期限：收到商品後 14 天內\n- 退款核准人：營運主管 張雅婷\n- 退款方式：原付款方式退回，或轉為購物金\n").encode("utf-8"))

    # Similar roles: the question asks for the deputy approver, not the approver or the handler.
    write("escalation.md", (
        "# 事件升級流程\n\n- 第一線處理人：客服組 陳柏宇\n- 升級核准人：值班經理 王志明\n"
        "- 代理核准人（核准人休假時）：資深工程師 黃詩涵\n- 通報對象：資安長 吳建宏\n- 升級時限：30 分鐘內\n").encode("utf-8"))

    # Look-alike columns: amount, refunded_amount and quantity.
    regions = ["north", "south", "east", "west"]
    rows = [(f"B{index:04d}", regions[index % 4], rng.randint(200, 3000), rng.randint(1, 9)) for index in range(1, 201)]
    refunds = [amount if rng.random() < 0.15 else 0 for _, _, amount, _ in rows]
    out = io.StringIO()
    table = csv.writer(out, lineterminator="\n")
    table.writerow(["order_id", "region", "amount", "refunded_amount", "quantity"])
    for (order, region, amount, quantity), refunded in zip(rows, refunds, strict=True):
        table.writerow([order, region, amount, refunded, quantity])
    write("orders.csv", out.getvalue().encode("utf-8"))
    amount_total, refund_total = sum(row[2] for row in rows), sum(refunds)
    refund_rate = percent(refund_total, amount_total)

    # Two sheets of one workbook.
    workbook = Workbook()
    h1 = workbook.active
    h1.title = "H1"
    h2 = workbook.create_sheet("H2")
    totals = {}
    for sheet in (h1, h2):
        sheet.append(["team", "tickets"])
        values = [rng.randint(40, 400) for _ in range(10)]
        for index, value in enumerate(values):
            sheet.append([f"team-{index + 1}", value])
        totals[sheet.title] = sum(values)
    buffer = io.BytesIO()
    workbook.save(buffer)
    write("tickets.xlsx", buffer.getvalue())
    half_ratio = percent(totals["H2"], totals["H1"])

    # A product sheet without the field the question asks for.
    write("product.md", (
        "# 產品規格：TX-200 路由器\n\n- 尺寸：210 × 140 × 35 mm\n- 重量：380 g\n- 無線規格：Wi-Fi 6\n"
        "- 連接埠：4 個 LAN、1 個 WAN\n- 電源：12V 1.5A\n- 建議售價：2490 元\n").encode("utf-8"))

    # A log larger than an 8K-token context; one line holds the answer.
    systems = ["訂單系統", "庫存系統", "會員系統", "報表系統", "通知服務", "搜尋服務"]
    lines = ["# 2026 年維運日誌", ""]
    for week in range(1, 121):
        system = systems[week % len(systems)]
        lines.append(f"第 {week} 週：{system}例行檢查完成，備份驗證通過，無異常；值班交接已記錄，待辦事項皆已結案。")
        if week == 67:
            lines.append("第 67 週補充：支付閘道憑證更新造成結帳中斷 47 分鐘，負責處理的是平台組 蔡宗翰，事後已補上到期提醒。")
    log = "\n".join(lines) + "\n"
    write("ops-log.md", log.encode("utf-8"))

    # Two short documents for a delegated task.
    write("vendor-a.md", (
        "# 供應商 A 報價\n\n- 單價：每台 1850 元\n- 最低訂購量：200 台\n- 交期：下單後 21 天\n- 保固：18 個月\n").encode("utf-8"))
    write("vendor-b.md", (
        "# 供應商 B 報價\n\n- 單價：每台 1790 元\n- 最低訂購量：500 台\n- 交期：下單後 35 天\n- 保固：12 個月\n").encode("utf-8"))

    limits = {"max_steps": 14, "max_tool_calls": 24, "max_corrections": 2, "timeout_seconds": 420}
    cases = [
        {"id": "policy-in-force", "flags": ["--answer"], "sources": ["refund-policy-2025.md", "refund-policy-2026.md"],
         "task": "在 2026 年 9 月 1 日，生效中的退款期限是幾天？由誰核准？請直接回答並附來源。",
         "truth": {"must_contain": ["14", "張雅婷"], "must_not_contain": ["林怡君"], "cites": "refund-policy-2026.md"}},
        {"id": "deputy-approver", "flags": ["--answer"], "sources": ["escalation.md"],
         "task": "升級核准人休假時，由誰代理核准？請直接回答並附來源。",
         "truth": {"must_contain": ["黃詩涵"], "must_not_contain": ["陳柏宇", "吳建宏"], "cites": "escalation.md"}},
        {"id": "refund-rate", "flags": ["--answer"], "sources": ["orders.csv"],
         "task": "orders.csv 中，refunded_amount 欄的總和占 amount 欄總和的百分之幾？請用工具計算，直接回答並附來源。",
         "truth": {"must_contain_number": refund_rate, "note": f"refunded {refund_total} / amount {amount_total}"}},
        {"id": "sheet-ratio", "flags": ["--answer"], "sources": ["tickets.xlsx"],
         "task": "tickets.xlsx 中，H2 工作表 tickets 欄的總和是 H1 工作表 tickets 欄總和的百分之幾？請用工具計算，直接回答並附來源。",
         "truth": {"must_contain_number": half_ratio, "note": f"H2 {totals['H2']} / H1 {totals['H1']}"}},
        {"id": "missing-field", "flags": ["--answer"], "sources": ["product.md"],
         "task": "TX-200 的保固期限是多久？建議售價是多少？請直接回答並附來源。",
         "truth": {"must_contain": ["2490"], "must_contain_any": ["未提供", "沒有提供", "未提及", "沒有提及", "未載明", "沒有說明", "未說明", "無法"],
                   "must_not_match": r"保固[^。\n]{0,12}\d+\s*(年|個月|月|天)"}},
        {"id": "long-log-compaction", "flags": ["--answer", "--compact"], "sources": ["ops-log.md"],
         "task": "維運日誌中，結帳中斷了幾分鐘？負責處理的是誰？請直接回答並附來源。",
         "truth": {"must_contain": ["47", "蔡宗翰"], "cites": "ops-log.md",
                   "note": f"ops-log.md is {len(log.encode('utf-8'))} bytes, larger than the 8,192-token context bound"}},
        {"id": "delegated-vendors", "flags": ["--answer", "--subagents"], "sources": ["vendor-a.md", "vendor-b.md"],
         "task": "請把兩份報價各交給一個子代理查詢單價與交期，再比較：哪一家單價較低？哪一家交期較短？請直接回答並附來源。",
         "truth": {"must_contain": ["1790", "1850", "21", "35"], "expects_tool": "subagent_run",
                   "note": "B is cheaper (1790 < 1850); A delivers sooner (21 < 35 days)"}},
        {"id": "report-artifact", "flags": [], "sources": ["orders.csv", "refund-policy-2026.md"],
         "task": "請寫一份 refund-summary.md：列出 orders.csv 的 amount 總和、refunded_amount 總和，以及 2026 年版政策的退款期限，每項附來源。",
         "truth": {"artifact_must_contain": [str(amount_total), str(refund_total), "14"], "artifacts": 1}},
    ]
    suite = {"version": 1, "model": "qwen3.5:9b-q4_K_M", "protocol": "json_tools, thinking off",
             "workbench_args": ["--allow-subagents"],
             "purpose": "Harder tasks than scenarios-01, one run each: versioned policies, similar roles, look-alike columns, "
                        "two sheets, a missing field, a log larger than the context window (compaction) and a delegated "
                        "comparison (subagents). Truth fixed before any run.",
             "limits": limits, "cases": cases}
    (HERE / "cases.json").write_text(json.dumps(suite, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("refund rate", refund_rate, "sheet ratio", half_ratio, "log bytes", len(log.encode("utf-8")))


if __name__ == "__main__":
    main()
