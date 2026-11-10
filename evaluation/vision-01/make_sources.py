"""Build the vision-01 workspace (two rendered images) and cases.json, with truth fixed before any model run.

    uv run python evaluation/vision-01/make_sources.py

Deterministic: the images are drawn from fixed values with Pillow's built-in font.
"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE / "workspace"
SALES = {"north": 412, "south": 365, "east": 508, "west": 277}
TICKETS = [("Mon", 34), ("Tue", 51), ("Wed", 47), ("Thu", 62), ("Fri", 29)]


def table_image(path: Path) -> None:
    font = ImageFont.load_default(size=30)
    image = Image.new("RGB", (640, 360), "white")
    draw = ImageDraw.Draw(image)
    draw.text((30, 20), "Q3 sales by region (units)", fill="black", font=font)
    rows = [("Region", "Units"), *[(name, str(value)) for name, value in SALES.items()]]
    for index, (left, right) in enumerate(rows):
        top = 80 + index * 52
        draw.rectangle((30, top, 610, top + 52), outline="black", width=2)
        draw.line((320, top, 320, top + 52), fill="black", width=2)
        draw.text((45, top + 10), left, fill="black", font=font)
        draw.text((335, top + 10), right, fill="black", font=font)
    image.save(path, format="PNG")


def chart_image(path: Path) -> None:
    font = ImageFont.load_default(size=26)
    image = Image.new("RGB", (720, 440), "white")
    draw = ImageDraw.Draw(image)
    draw.text((30, 15), "Support tickets per day", fill="black", font=font)
    base, scale = 380, 4
    draw.line((60, base, 690, base), fill="black", width=2)
    for index, (day, value) in enumerate(TICKETS):
        left = 90 + index * 120
        draw.rectangle((left, base - value * scale, left + 70, base), fill=(70, 110, 180))
        draw.text((left + 10, base - value * scale - 34), str(value), fill="black", font=font)
        draw.text((left + 8, base + 8), day, fill="black", font=font)
    image.save(path, format="PNG")


def main() -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    table_image(WORKSPACE / "sales-table.png")
    chart_image(WORKSPACE / "tickets-chart.png")
    busiest = max(TICKETS, key=lambda item: item[1])
    limits = {"max_steps": 12, "max_tool_calls": 20, "max_corrections": 2}
    cases = [
        {"id": "table-image-value", "flags": ["--answer"], "sources": ["sales-table.png"],
         "task": "圖片中的表格，east 地區的 Units 是多少？請直接回答並附來源。",
         "truth": {"must_contain": [str(SALES["east"])], "artifacts": 0}},
        {"id": "chart-image-max", "flags": ["--answer"], "sources": ["tickets-chart.png"],
         "task": "這張長條圖中，哪一天的工單數最多？是多少？請直接回答並附來源。",
         "truth": {"must_contain": [busiest[0], str(busiest[1])], "artifacts": 0}},
    ]
    suite = {"version": 1, "model": "qwen3.5:9b-q4_K_M", "vision_model": "qwen3.5:9b-q4_K_M",
             "protocol": "json_tools, thinking off",
             "purpose": "One real-model run per image case: the vision model transcribes, the decision model answers "
                        "from the labelled transcription. Truth fixed before any run.",
             "limits": limits, "cases": cases}
    (HERE / "cases.json").write_text(json.dumps(suite, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("east", SALES["east"], "busiest", busiest)


if __name__ == "__main__":
    main()
