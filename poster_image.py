"""Ink-and-paper report posters; all graphics are drawn locally."""
from io import BytesIO
from pathlib import Path
import math
import random
import re

from PIL import Image, ImageDraw, ImageFilter, ImageFont

WIDTH, MARGIN = 1080, 68
CW = WIDTH - MARGIN * 2
PAPER, INK, MUTED, RED, RULE = "#f2efe7", "#252722", "#77766d", "#a72e29", "#d3cbbd"
FONT_DIR = Path(__file__).parent / "assets" / "fonts"


def _font(size, brush=False):
    return ImageFont.truetype(str(FONT_DIR / ("MaShanZheng-Regular.ttf" if brush else "NotoSansCJKsc-Regular.otf")), size)


# 微信会把内嵌图压到 64KB 以内，笔画太细的字压完就糊成一团。
# 这里给正文描一圈同色边（伪加粗），并把纸张换成纯白，让压缩预算尽量花在字上。
_STROKE = 1
_PLAIN_PAPER = True


def configure(*, stroke: int = 1, plain_paper: bool = True) -> None:
    """按配置调整渲染风格：stroke=0 关掉描边，plain_paper=False 恢复水墨纸纹。"""
    global _STROKE, _PLAIN_PAPER
    _STROKE = max(0, min(int(stroke), 3))
    _PLAIN_PAPER = bool(plain_paper)


def _dt(draw, xy, text, font=None, fill=None, **kwargs):
    """带描边的文字绘制：小字号（<22px）不加描边，避免糊成一块。"""
    stroke = _STROKE if (font is not None and getattr(font, "size", 0) >= 22) else 0
    if stroke:
        kwargs.setdefault("stroke_width", stroke)
        kwargs.setdefault("stroke_fill", fill)
    return draw.text(xy, text, font=font, fill=fill, **kwargs)


def _wrap(text, font, width):
    output = []
    for line in str(text).splitlines() or [""]:
        current = ""
        for char in line.expandtabs(4):
            if current and font.getlength(current + char) > width:
                output.append(current)
                current = ""
            current += char
        output.append(current)
    return output


def _tile(height):
    return Image.new("RGB", (CW, height), "#ffffff" if _PLAIN_PAPER else PAPER)


def _text(text, size=24, color=INK, indent=0):
    font = _font(size)
    lines = _wrap(text, font, CW - indent - 12)
    tiles = []
    for start in range(0, len(lines), 20):
        part = lines[start:start + 20]
        tile = _tile(16 + len(part) * (size + 12))
        draw = ImageDraw.Draw(tile)
        for row, line in enumerate(part):
            _dt(draw, (indent, 4 + row * (size + 12)), line, font=font, fill=color)
        tiles.append(tile)
    return tiles


def _heading(text):
    # Long dynamic section names keep the same readable body font.
    font = _font(26)
    lines = _wrap(text, font, CW - 46)
    tile = _tile(26 + len(lines) * 36)
    draw = ImageDraw.Draw(tile)
    draw.rectangle((0, 18, 6, 44), fill=RED)
    for row, line in enumerate(lines):
        _dt(draw, (23, 7 + row * 36), line, font=font, fill=INK)
    draw.line((23, tile.height - 4, CW, tile.height - 4), fill=RULE)
    return tile


def _wash(width, height):
    """Soft, irregular ink ridges rather than geometric mountain icons."""
    if _PLAIN_PAPER:
        return Image.new("RGB", (width, height), "#ffffff")
    layer = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(layer)
    rng = random.Random(74)
    for base, shade, phase in ((height - 20, "#e7e4db", 0), (height + 5, "#deddd3", 2),
                                (height + 50, "#cfcfc4", 4)):
        ridge = []
        for x in range(0, width + 8, 8):
            y = base - 65 * math.sin(x / 150 + phase) - 30 * math.sin(x / 61 + phase) + rng.randint(-9, 9)
            ridge.append((x, max(0, min(height, int(y)))))
        draw.polygon(ridge + [(width, height), (0, height)], fill=shade)
    return layer.filter(ImageFilter.GaussianBlur(1.2))


def _metrics(pairs, columns=4, core=False):
    """Open typography with fine rules; never boxes around individual numbers."""
    tiles = []
    width = CW // columns
    for start in range(0, len(pairs), columns):
        cells = pairs[start:start + columns]
        layouts = []
        for label, value in cells:
            coverage = []
            sample = re.fullmatch(r"(.*?)（(\d+/\d+场有数据)）", value)
            if sample:
                value = sample.group(1)
                coverage = _wrap(sample.group(2), _font(18), width - 30)
            value_font = _font(60 if core else 29)
            if value_font.getlength(value) > width - 30:
                value_font = _font(29)
            labels = _wrap(label, _font(20), width - 30)
            values = _wrap(value, value_font, width - 30)
            layouts.append((label, value, labels, values, value_font, coverage))
        height = max(30 + len(c[2]) * 28 + len(c[3]) * (c[4].size + 8) + len(c[5]) * 25 for c in layouts)
        if height > 1000:
            tiles.extend(_text("\n".join(f"{k}：{v}" for k, v in cells)))
            continue
        tile = _tile(height)
        draw = ImageDraw.Draw(tile)
        for col, (label, value, labels, values, vf, coverage) in enumerate(layouts):
            x = col * width
            for row, text in enumerate(labels):
                _dt(draw, (x, 9 + row * 28), text, font=_font(20), fill=MUTED)
            color = RED if label in ("伤害", "分数", "当前模式分数") or "场均伤害" in label else INK
            if label == "积分变化" and value != "—":
                color = "#3e745a" if value.startswith("+") else RED
            for row, text in enumerate(values):
                _dt(draw, (x, 16 + len(labels) * 28 + row * (vf.size + 8)), text, font=vf, fill=color)
            for row, text in enumerate(coverage):
                _dt(draw, (x, 16 + len(labels) * 28 + len(values) * (vf.size + 8) + row * 25), text, font=_font(18), fill=MUTED)
            if col and core:
                draw.line((x - 20, 22, x - 20, height - 15), fill=RULE)
        draw.line((0, height - 2, CW, height - 2), fill=RULE)
        tiles.append(tile)
    return tiles


def _banner(title, subtitle, right_label="", right_value="", notes=None):
    """A calligraphic focal point with a large isolated score or match rank."""
    brush = _font(106, True)
    title_width = 600 if right_value else CW - 25
    if brush.getlength(title) > title_width:
        brush = _font(72, True)
    title_lines = _wrap(title, brush, title_width)
    if len(title_lines) > 4:
        return _text(title, 42) + _text(subtitle, 22) + _text(f"{right_label}：{right_value}", 30) + _text("\n".join(notes or []), 22)
    height = max(270, 150 + len(title_lines) * (brush.size + 8))
    tile = _tile(height)
    # Light wash is confined to the lower edge so it cannot obscure the title.
    wash = _wash(CW, 105)
    tile.paste(wash, (0, height - 105))
    draw = ImageDraw.Draw(tile)
    _dt(draw, (0, 8), subtitle, font=_font(22), fill=MUTED)
    for row, line in enumerate(title_lines):
        _dt(draw, (-3, 49 + row * (brush.size + 8)), line, font=brush, fill=INK)
    if right_value:
        x = 690
        for row, line in enumerate(_wrap(right_label, _font(18), CW - x)):
            _dt(draw, (x, 9 + row * 24), line, font=_font(18), fill=MUTED)
        score_font = _font(82 if len(right_value) <= 5 else 54)
        score_lines = _wrap(right_value, score_font, CW - x)
        if len(score_lines) <= 2:
            for row, line in enumerate(score_lines):
                _dt(draw, (x, 58 + row * 82), line, font=score_font, fill=RED)
        else:
            overflow_value = f"{right_label}：{right_value}"
            _dt(draw, (x, 67), "详见下方", font=_font(22), fill=RED)
    else:
        score_lines = []
    if notes:
        # Keep every note, wrapping longer unknown mode/map labels below the hero.
        note_text = "  ·  ".join(notes)
        note_lines = _wrap(note_text, _font(21), CW - 15)
        for row, line in enumerate(note_lines[:2]):
            _dt(draw, (0, height - 67 + row * 30), line, font=_font(21), fill=MUTED)
        overflow = note_lines[2:]
    else:
        overflow = []
    return ([tile] + (_text(overflow_value, 24, RED) if right_value and len(score_lines) > 2 else [])
            + (_text("\n".join(overflow), 21) if overflow else []))


def _hero(text):
    match = re.fullmatch(r"(\d+)、(.+?)\s+(\d+)场", text)
    if not match:
        return _text(text)
    rank, name, count = match.groups()
    font = _font(28)
    if font.getlength(name) > CW - 200:
        return _text(text)
    tile = _tile(67)
    draw = ImageDraw.Draw(tile)
    _dt(draw, (0, 12), rank.zfill(2), font=_font(21), fill=RED)
    _dt(draw, (60, 5), name, font=font, fill=INK)
    value = f"{count} 场"
    _dt(draw, (CW - font.getlength(value), 5), value, font=font, fill=MUTED)
    draw.line((60, 62, CW, 62), fill=RULE)
    return [tile]


def _match_tiles(blocks, mode):
    header = blocks[0][1]
    parts = str(header).split("｜")
    attributes = dict(part.split("：", 1) for part in parts[1:] if "：" in part)
    pairs = [pair for kind, data in blocks[1:] if kind == "metrics" for pair in data]
    values = dict(pairs)
    rank, people = values.get("排名", "—"), values.get("参赛人数", "—")
    notes = [f"时间：{attributes.get('时间', '—')}"]
    for label in ("地图", "评价"):
        if label in values:
            notes.append(f"{label}：{values[label]}")
    tiles = _banner(attributes.get("英雄", "未知英雄"), f"{parts[0]}  /  {mode or '单局战报'}",
                    f"排名 / {people} 人", f"#{rank}", notes)
    core_labels = ("击杀", "伤害", "恢复")
    tiles.extend(_metrics([(key, values.get(key, "—")) for key in core_labels], columns=3, core=True))
    excluded = set(core_labels) | {"排名", "参赛人数", "地图", "评价"}
    secondary = [(key, value) for key, value in pairs if key not in excluded]
    if secondary:
        tiles.append(_heading("战斗明细"))
        tiles.extend(_metrics(secondary))
    extra = [(kind, data) for kind, data in blocks[1:] if kind != "metrics"]
    if extra:
        tiles.append(_heading("本局记录"))
        for kind, data in extra:
            tiles.extend(_text(str(data), 23))
    return tiles


def render_poster_pages(report, blocks):
    identity = next((data for kind, data in blocks if kind == "identity"), [])
    name = identity[0] if identity else "永劫战绩"
    role_id = identity[1] if len(identity) > 1 else ""
    ai_index = next((i for i, (kind, data) in enumerate(blocks) if kind == "section" and data == "AI战绩分析"), None)
    ai_tail = blocks[ai_index:] if ai_index is not None else []
    if ai_index is not None:
        blocks = blocks[:ai_index]
    match_indices = [i for i, (kind, _) in enumerate(blocks) if kind == "match"]
    groups = []
    if match_indices:
        prefix = [(kind, data) for kind, data in blocks[:match_indices[0]] if kind != "identity"]
        time_notes = [(kind, data) for kind, data in prefix if kind == "text" and str(data).startswith(
            ("查询时间（北京时间）", "数据更新时间：", "近期样本时间范围（北京时间）"))]
        prefix = [item for item in prefix if item not in time_notes]
        mode_match = re.search(r"模式[：:]\s*([^\n｜]+)", report)
        mode = mode_match.group(1).strip() if mode_match else ""
        for index, start in enumerate(match_indices):
            end = match_indices[index + 1] if index + 1 < len(match_indices) else len(blocks)
            tiles = _match_tiles(blocks[start:end], mode)
            if index == 0 and time_notes:
                tiles = [tile for _, text in time_notes for tile in _text(str(text), 20, MUTED)] + tiles
            if index == 0 and prefix:
                # Query counts and warnings remain visible as a compact appendix.
                tiles.append(_heading("查询范围"))
                for kind, data in prefix:
                    if kind == "metrics":
                        tiles.extend(_metrics(data))
                    else:
                        tiles.extend(_text(str(data), 20, MUTED))
            groups.append(tiles)
    else:
        pairs = [pair for kind, data in blocks if kind == "metrics" for pair in data]
        values = dict(pairs)
        score = values.get("当前模式分数", "")
        mode = values.get("当前模式", "")
        only_ai = bool(ai_tail) and not any(kind == "section" for kind, _ in blocks)
        tiles = _banner("战绩分析" if only_ai else "战绩", "AI战绩评价" if only_ai else "个人战绩总览", "" if only_ai else f"{mode} · 分数" if mode else "分数", "" if only_ai else score)
        for kind, data in blocks:
            if kind == "identity":
                continue
            if kind == "metrics":
                remaining = [(key, value) for key, value in data if key not in ("当前模式", "当前模式分数")]
                if remaining:
                    tiles.extend(_metrics(remaining, columns=3))
            elif kind == "section":
                tiles.append(_heading(str(data)))
            elif kind == "hero":
                tiles.extend(_hero(str(data)))
            else:
                tiles.extend(_text(str(data)))
        groups.append(tiles)

    if ai_tail:
        ai_tiles = [_heading("AI战绩分析")]
        for _, text in ai_tail[1:]:
            ai_tiles.extend(_text(str(text), 23))
        groups.append(ai_tiles)
    identity_tiles = _text(name, 28) + (_text(role_id, 18, MUTED) if role_id else [])
    # Keep the complete report at its original font size in one long image.
    pages = [[tile for tiles in groups for tile in tiles]]
    output = []
    for index, page in enumerate(pages, 1):
        height = 150 + sum(tile.height + 6 for tile in identity_tiles) + sum(tile.height + 10 for tile in page)
        image = Image.new("RGB", (WIDTH, height), "#ffffff" if _PLAIN_PAPER else PAPER)
        draw = ImageDraw.Draw(image)
        _dt(draw, (MARGIN, 13), "永劫无间", font=_font(38, True), fill=INK)
        _dt(draw, (WIDTH - 260, 29), "战报  /  NARAKA", font=_font(18), fill=MUTED)
        draw.line((MARGIN, 67, WIDTH - MARGIN, 67), fill=INK, width=2)
        y = 81
        for tile in identity_tiles:
            image.paste(tile, (MARGIN, y))
            y += tile.height + 6
        y += 10
        for tile in page:
            image.paste(tile, (MARGIN, y))
            y += tile.height + 10
        draw.line((MARGIN, height - 40, WIDTH - MARGIN, height - 40), fill=RULE)
        footer = "小黑盒战绩  /  AI评价仅供参考  /  — 表示数据缺失" if ai_tail else "小黑盒数据  /  — 表示数据缺失或无可展示内容"
        _dt(draw, (MARGIN, height - 33), footer, font=_font(17), fill=MUTED)
        page_number = f"{index:02} / {len(pages):02}"
        _dt(draw, (WIDTH - MARGIN - _font(17).getlength(page_number), height - 33), page_number, font=_font(17), fill=RED)
        target = BytesIO()
        image.save(target, format="PNG")
        output.append(target.getvalue())
    return output
