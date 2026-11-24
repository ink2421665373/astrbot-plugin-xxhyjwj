"""Parse report sections and render local ink-and-paper posters."""
from pathlib import Path
import re
from PIL import ImageFont

FONT_PATH = Path(__file__).parent / "assets" / "fonts" / "NotoSansCJKsc-Regular.otf"
WIDTH = 1080
MAX_HEIGHT = 1600
PAGE_LINES = 25


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines = []
    for original in text.splitlines() or [""]:
        current = ""
        for char in original.expandtabs(4):
            if current and font.getlength(current + char) > width:
                lines.append(current)
                current = ""
            current += char
        lines.append(current)
    return lines


def _blocks(report: str) -> list[tuple[str, object]]:
    """Group known sections, preserving unfamiliar upstream fields as text."""
    blocks, metrics = [], []
    columns = 4 if re.search(r"第\d+局[｜|]", report) else 3

    def flush():
        while metrics:
            blocks.append(("metrics", metrics[:columns]))
            del metrics[:columns]

    lines = report.splitlines()
    if lines and lines[0].startswith("昵称："):
        identity = [lines.pop(0)[3:]]
        if lines and lines[0].startswith("ID："):
            identity.append(lines.pop(0))
        blocks.append(("identity", identity))
    ai_section = False
    for line in lines:
        if line == "AI战绩分析":
            flush()
            ai_section = True
            blocks.append(("section", line))
            continue
        if ai_section:
            flush()
            if line.strip():
                blocks.append(("text", line))
            continue
        if not line.strip():
            flush()
            continue
        if line.startswith(("当前赛季概览", "指定赛季概览", "全部赛季概览", "近期对局汇总", "常用英雄", "有效对局详情", "有效对局列表")):
            flush()
            blocks.append(("section", line.rstrip("：")))
        elif re.match(r"第\d+局[｜|]", line):
            flush()
            blocks.append(("match", line))
        elif re.match(r"\d+、", line):
            flush()
            blocks.append(("hero", line))
        elif line.startswith(("武器（", "魂玉：", "标签：", "查询时间（北京时间）", "数据更新时间：", "近期样本时间范围（北京时间）", "对局赛季范围：")):
            flush()
            blocks.append(("text", line))
        else:
            parts = line.split("｜")
            if all("：" in part for part in parts):
                for part in parts:
                    label, value = part.split("：", 1)
                    if label in ("排位", "天人") and len(value.split()) == 3:
                        flush()
                        blocks.append(("metrics", [(f"{label} · {mode}", score)
                                                    for mode, score in zip(("单排", "双排", "三排"), value.split())]))
                    else:
                        metrics.append((label.strip(), value.strip() or "—"))
            else:
                flush()
                blocks.append(("text", line))
    flush()
    return blocks or [("text", "暂无可展示的数据")]


def render_report_images(report: str) -> list[bytes]:
    if __package__:
        from .poster_image import render_poster_pages
    else:
        from poster_image import render_poster_pages
    return render_poster_pages(report, _blocks(report))
