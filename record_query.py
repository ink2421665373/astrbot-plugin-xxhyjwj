"""Select and summarize recent Xiaoheihe NARAKA match records.

The match-list endpoint may return a mixed pool even when given a mode. Check
each returned row; list damage is exact, while detail.data contains display values.
"""

from __future__ import annotations

import re
import math
from collections import Counter
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))


MODES = {
    "4": "天人单排",
    "13": "天人双排",
    "5": "天人三排",
    "5000000": "天选单排",
    "12": "天选双排",
    "5000001": "天选三排",
    "6": "快速单排",
    "7": "快速三排",
    "5000010": "无尽试炼",
}
RANKED_IDS = frozenset(("4", "13", "5", "5000000", "12", "5000001"))
TIANREN_IDS = frozenset(("4", "13", "5"))
TIANXUAN_RANK_NAMES = ("青铜", "白银", "黄金", "铂金", "陨星", "蚀月", "坠日",
                       "无间修罗", "无双修罗", "无相龙王", "无量梵天")


def normalize_rank_data(mode_id: str, body: dict | None) -> tuple[dict, str]:
    """Keep API ranks mode-specific; never infer a Tianren rank from ranked tiers.

    The PC game renamed Tianren ranks on 2025-07-24. Its 2026-10 season
    announcement still distinguishes 星月宗师 from the Tianxuan ranks above.
    https://www.yjwujian.cn/news/update/20250710/33456_1246358.html
    https://www.yjwujian.cn/news/update/20261010/33456_1316200.html
    Neither announcement supplies a complete current score-to-rank table, so
    do not substitute a mobile table or guess a rank from rating.
    """
    source = body if isinstance(body, dict) else {}
    if str(mode_id) not in TIANREN_IDS:
        return source, ""
    result = dict(source)
    rejected = []

    def validate(value):
        if value is None or isinstance(value, str) and not value.strip():
            return value
        if isinstance(value, str):
            if any(name in value for name in TIANXUAN_RANK_NAMES):
                rejected.append("mode")
                return None
            if not re.fullmatch(r"[\d.\s]+", value):
                return value
        rejected.append("code")
        return None

    info = source.get("player_info")
    if isinstance(info, dict):
        result["player_info"] = dict(info)
        if "level" in info:
            result["player_info"]["level"] = validate(info["level"])
    overview = source.get("overview")
    if isinstance(overview, list):
        result["overview"] = []
        for item in overview:
            if not isinstance(item, dict):
                result["overview"].append(item)
                continue
            item = dict(item)
            label = str(item.get("desc") or "")
            if "段位" in label and not any(word in label for word in ("积分", "分数", "变化", "排名")):
                item["value"] = validate(item.get("value"))
            result["overview"].append(item)
    note = ""
    if "mode" in rejected:
        note = "接口返回了天选之人的段位名称，无法据此确认天人之战段位；已保留分数和其他统计。"
    elif rejected:
        note = "接口未返回可确认的天人段位名称；已保留分数和其他统计，不按积分猜测段位。"
    return result, note

# 裸写模式：不带"天选/天人"前缀时，固定归到天选
BARE_MODES = {
    "单排": "5000000",
    "双排": "12",
    "三排": "5000001",
}

MODE_USAGE = "模式请填 单排/双排/三排、天选单排/双排/三排、天人单排/双排/三排，或快速单排/三排"
# Fallback catalog from the user's September 2026 Xiaoheihe home/data capture.
# A fresh API catalog overrides these names when available.
KNOWN_HERO_NAMES = {
    "1000021": "季莹莹", "1000031": "李寻欢", "1000017": "殷紫萍",
    "1000003": "宁红夜", "1000018": "沈妙", "1000032": "巫真",
    "1000009": "妖刀姬", "1000026": "张起灵", "1000027": "席拉",
    "1000022": "玉玲珑", "1000015": "顾清寒", "1000005": "特木尔",
    "1000001": "土御门胡桃", "1000010": "崔三娘", "1000004": "迦南",
    "1000029": "万钧", "1000030": "万钧", "1000020": "胡为", "1000013": "无尘",
    "1000023": "哈迪", "1000006": "季沧海", "1000024": "魏轻",
    "1000016": "武田信忠", "1000011": "岳山", "1000007": "天海",
    "1000025": "刘炼", "1000033": "甘璇", "1000028": "蓝梦",
}
KNOWN_HERO_SUFFIXES = {"70": "南宫锦", "73": "叶修"}


def apply_hero_mappings(hero_names: dict[str, str], records: list[dict], entries: object) -> dict[str, str]:
    """Apply editable short or full hero IDs over the API catalog."""
    result = dict(hero_names)
    if not isinstance(entries, list):
        return result
    overrides = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        hero_id = str(entry.get("hero_id") or "").strip()
        hero_name = str(entry.get("hero_name") or "").strip()
        if hero_id.isdigit() and hero_name:
            overrides[hero_id] = hero_name
    observed_ids = set(result)
    observed_ids.update(str(record.get("hero_id") or "") for record in records)
    for hero_id in observed_ids:
        if not hero_id.isdigit():
            continue
        exact = overrides.get(hero_id)
        suffix = overrides.get(hero_id[-2:].zfill(2))
        if exact or suffix:
            result[hero_id] = exact or suffix
    return result


def resolve_mode(text: str) -> str:
    """Return the list's battle_tid for an explicit mode, or raise.

    支持的写法（全部收敛到这里，其它地方不要再做补全）：
      * 数字 battle_tid：4 / 13 / 5 / 5000000 / 12 / 5000001 / 6 / 7 / 5000010
      * 四字组合：天人单排、天选三排、快速单排……
      * 长写法：天人之战单排、天选之人三排、快速匹配三排
      * 裸写：单排 / 双排 / 三排  → 固定归到「天选」
    """
    name = re.sub(r"[·・\s_—－-]", "", text)
    if not name:
        raise ValueError(MODE_USAGE)
    if name in MODES:
        return name
    if name in BARE_MODES:
        return BARE_MODES[name]
    for mode_id, label in MODES.items():
        if name in (label, label.replace("天人", "天人之战"), label.replace("天选", "天选之人"),
                    label.replace("快速", "快速匹配")):
            return mode_id
    raise ValueError(MODE_USAGE)


def recent_rows(body: dict, limit: int = 30) -> list[dict]:
    """Deduplicate and sort the returned mixed pool, capped by configuration."""
    rows = body.get("match_list")
    if not isinstance(rows, list):
        raise ValueError("对局列表格式已变化：缺少 match_list")
    seen = set()
    matches = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        match_id = str(row.get("match_id") or "")
        if match_id and match_id not in seen:
            seen.add(match_id)
            matches.append(row)
    matches.sort(key=lambda row: _number(row.get("time")) or 0, reverse=True)
    return matches[:limit]


def select_matches(rows: list[dict], requested_mode: str = "", limit: int = 10) -> tuple[str | None, list[dict]]:
    """Auto-select the newest ranked mode, excluding quick and other modes."""
    if requested_mode:
        mode_id = resolve_mode(requested_mode)
    else:
        mode_id = next((str(row.get("battle_tid")) for row in rows
                        if str(row.get("battle_tid")) in RANKED_IDS), None)
    if mode_id is None:
        return None, []
    return mode_id, [row for row in rows if str(row.get("battle_tid")) == mode_id][:limit]


def match_mode_counts(rows: list[dict]) -> dict[str, int]:
    """Expose the returned pool's actual mode distribution, including unknown IDs."""
    counts = Counter()
    for row in rows:
        code = str(row.get("battle_tid") or "")
        label = MODES.get(code) or (f"未识别模式（编码{code}）" if re.fullmatch(r"\d{1,15}", code) else "未标明模式")
        counts[label] += 1
    return dict(counts.most_common())


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(k|万|km|min|h)?\s*", value, re.I)
    if not match:
        return None
    number = float(match.group(1))
    suffix = (match.group(2) or "").lower()
    return number * {"k": 1000, "万": 10000, "km": 1, "min": 1, "h": 60, "": 1}[suffix]


def _metric(detail: dict, label: str) -> float | None:
    entries = detail.get("data")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("desc") == label:
            return _number(entry.get("value"))
    return None


def _timestamp_ms(value: object) -> float | None:
    """Accept plausible epoch seconds/milliseconds, never durations or bad dates."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and not re.fullmatch(r"\s*\d+(?:\.\d+)?\s*", value):
        return None
    try:
        number = _number(value)
    except (ValueError, OverflowError):
        return None
    if number is None or not math.isfinite(number) or number <= 0:
        return None
    milliseconds = number if number >= 1e12 else number * 1000
    try:
        stamp = datetime.fromtimestamp(milliseconds / 1000, BEIJING)
    except (ValueError, OverflowError, OSError):
        return None
    return milliseconds if 2000 <= stamp.year <= 2100 else None


def format_query_metadata(queried_at: float | None = None) -> list[str]:
    stamp = datetime.now(BEIJING) if queried_at is None else datetime.fromtimestamp(queried_at, BEIJING)
    return [f"查询时间（北京时间）：{stamp:%Y-%m-%d %H:%M:%S}",
            "数据更新时间：未知（不以查询时间代替）"]


def format_sample_period(records: list[dict], selected: int, samples: list[dict] | None = None) -> str:
    """Use the selected mode's sample, including rows whose detail request failed."""
    prefix = "近期样本时间范围（北京时间）："
    if not selected:
        return prefix + "无选中对局"
    times = [_timestamp_ms(item.get("time")) for item in (records if samples is None else samples)]
    available = [value for value in times if value is not None]
    coverage = f"{len(available)}/{selected}场有有效时间"
    if not available:
        return prefix + f"未知（{coverage}）"
    start, end = (datetime.fromtimestamp(value / 1000, BEIJING).strftime("%Y-%m-%d %H:%M")
                  for value in (min(available), max(available)))
    partial = "，范围仅覆盖有时间的对局" if len(available) < selected else ""
    return prefix + f"{start} 至 {end}（{coverage}{partial}）"


def _match_time(row: dict, detail: dict) -> float | None:
    """从详情数据里找对局时间戳。优先 detail.data 里 desc 含"时间"的项。"""
    entries = detail.get("data")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            desc = str(entry.get("desc") or "")
            if "时间" in desc:
                ts = _timestamp_ms(entry.get("value"))
                if ts is not None:
                    return ts
    return _timestamp_ms(detail.get("time")) or _timestamp_ms(row.get("time"))


def _whole(value: float | None, unit: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:.1f}{unit}"


def _integer(value: float | None) -> str:
    return str(round(value)) if value is not None else "—"


def _average(records: list[dict], key: str) -> float | None:
    values = [record[key] for record in records if record.get(key) is not None]
    return sum(values) / len(values) if values else None


def make_record(row: dict, detail: dict) -> dict:
    """Prefer exact list fields where available; retain rounded detail fields."""
    kills = _metric(detail, "击杀")
    damage = _number(row.get("damage"))
    rating_delta = _number(detail.get("rating_delta"))
    weapons = []
    for item in detail.get("weapon_list") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        part = str(item["name"])
        share = _number(item.get("per"))
        if share is not None:
            part += f"{share * 100:.0f}%"
        weapons.append(part)
    return {
        "time": _match_time(row, detail),
        "rank": _number(detail.get("rank")) or _number(row.get("rank")),
        "kills": kills if kills is not None else _number(row.get("kill_times")),
        "damage": damage if damage is not None else _metric(detail, "总伤害"),
        "recovery": _metric(detail, "总恢复"),
        "parry": _metric(detail, "振刀"),
        "grapple_used": _metric(detail, "使用钩索"),
        "grapple_hit": _metric(detail, "钩索命中"),
        "move_hits": _metric(detail, "招式命中"),
        "skills": _metric(detail, "释放技能"),
        "survival_min": _metric(detail, "存活时长"),
        "rescues": _metric(detail, "救援"),
        "distance_km": _metric(detail, "移动距离"),
        "revives": _metric(detail, "复活数"),
        "currency": _metric(detail, "消耗货币"),
        "rating_delta": rating_delta if rating_delta is not None else _number(row.get("rating_delta")),
        "rating": _number(detail.get("rating")) or _number(row.get("rating")),
        "total_users": _number(row.get("total_users_count")),
        "map": str(detail.get("map_name") or row.get("map_name") or ""),
        "grade": str(detail.get("grade") or row.get("grade") or ""),
        "hero_id": str(row.get("hero_id") or ""),
        "weapons": weapons,
        "souls": [str(item.get("name")) for item in detail.get("soul_item_list") or []
                  if isinstance(item, dict) and item.get("name")],
        "tags": [str(item.get("name")) for item in detail.get("tags") or []
                 if isinstance(item, dict) and item.get("name")],
    }


def short_role_id(role_id: object) -> str:
    """把角色主键压缩成便于阅读的形式。

    小黑盒端游角色主键形如 ``<4 位前缀><18 位数字>``（22 字符）：
    4 位随机前缀 + 15 位序号 + 3 位服务器号。展示时去掉前缀和前导零，
    得到 15 位序号；其它格式原样返回。
    """
    value = str(role_id or "").strip()
    if re.fullmatch(r"[0-9A-Za-z]{4}\d{18}", value):
        body = value[4:].lstrip("0")
        return body or value[4:]
    return value


def format_identity(player_name: str, role_id: object = "") -> list[str]:
    """统一的标题：昵称与 ID **各自独占一行**。"""
    lines = [f"昵称：{player_name}"]
    if role_id is not None and str(role_id).strip():
        lines.append(f"ID：{role_id}")
    return lines


def _score(scores: dict[str, str], mode_id: str) -> str:
    raw = str(scores.get(mode_id) or "").strip()
    return f"{int(raw):,}" if raw.isdigit() and int(raw) > 0 else "—"


def _hero_label(hero_id: object, hero_names: dict[str, str]) -> str:
    value = str(hero_id or "")
    name = hero_names.get(value)
    if name:
        return name
    if value.isdigit():
        suffix = value[-2:].zfill(2)
        return KNOWN_HERO_SUFFIXES.get(suffix, suffix)
    return "未知英雄"


def format_summary(player_name: str, total: int, mode_id: str, selected: int,
                   records: list[dict], scores: dict[str, str], hero_names: dict[str, str],
                   failed: int = 0, role_id: object = "", *,
                   season_body: dict | None = None, season_name: str = "",
                   season_error: str = "", season_notice: str = "", queried_at: float | None = None,
                   samples: list[dict] | None = None, pool_modes: dict[str, int] | None = None,
                   recent_season_name: str = "") -> str:
    label = MODES.get(mode_id, f"模式 {mode_id}")
    lines = [
        *format_identity(player_name, role_id),
        *format_query_metadata(queried_at),
        f"排位：{_score(scores, '5000000')} {_score(scores, '12')} {_score(scores, '5000001')}",
        f"天人：{_score(scores, '4')} {_score(scores, '13')} {_score(scores, '5')}",
        f"当前模式：{label}",
        f"当前模式分数：{_score(scores, mode_id)}",
    ]
    overview_title = ("全部赛季概览" if season_name == "全部赛季" else
                      "指定赛季概览" if season_name.endswith("（WebUI配置）") else "当前赛季概览")
    lines.append(f"{overview_title}｜{season_name or '赛季未知'}｜{label}")
    if season_notice:
        lines.append(season_notice)
    if season_error:
        lines.append(season_error)
    else:
        body, rank_note = normalize_rank_data(mode_id, season_body)
        info = body.get("player_info")
        season_lines = []
        if isinstance(info, dict):
            for key, title in (("rating", "分数"), ("level", "段位"), ("lv", "等级")):
                value = info.get(key)
                if key == "level" and value is None and rank_note:
                    season_lines.append("段位：待确认")
                if value is not None and str(value).strip():
                    season_lines.append(f"{title}：{value}")
        overview = body.get("overview")
        if isinstance(overview, list):
            for item in overview:
                if not isinstance(item, dict):
                    continue
                desc, value = item.get("desc"), item.get("value")
                if desc is not None and str(desc).strip() and value is not None and str(value).strip():
                    season_lines.append(f"{desc}：{value}")
        empty_message = ("接口未返回全部赛季统计" if season_name == "全部赛季" else
                         "接口未返回指定赛季统计" if season_name.endswith("（WebUI配置）") else "接口未返回当前赛季统计")
        lines.extend(season_lines or [empty_message])
        if rank_note:
            lines.append(rank_note)
    lines.extend([
        "近期对局汇总（以下统计仅基于本次筛选的对局）：",
        *([f"对局赛季范围：{recent_season_name}"] if recent_season_name else []),
        format_sample_period(records, selected, samples),
        f"查询最近场次：{total}",
        f"选中对局：{selected}",
        f"有效对局：{len(records)}",
    ])
    if not selected:
        lines.append(f"本次读取的最近{total}场对局中没有选中{label}对局，不代表该模式历史没有战绩。"
                     if total else "本次近期对局列表返回0场，不代表历史战绩为空；赛季概览与近期样本分别统计。")
        if pool_modes:
            lines.append("本次列表模式分布为 " + "、".join(f"{name}{count}场" for name, count in pool_modes.items()) + "。")
    if failed:
        lines.append(f"详情失败：{failed}（未计入场均）")
    counts = Counter(_hero_label(record.get("hero_id"), hero_names) for record in records)
    lines.append("常用英雄（最多五个）：")
    if counts:
        for index, (hero_name, count) in enumerate(counts.most_common(5), 1):
            lines.append(f"{index}、{hero_name}  {count}场")
    else:
        lines.append("暂无")
    for key, title in (("damage", "伤害"), ("parry", "振刀"), ("kills", "击败")):
        available = sum(record.get(key) is not None for record in records)
        lines.append(
            f"最近{selected}场场均{title}：{_whole(_average(records, key))}"
            f"（{available}/{selected}场有数据）"
        )
    return "\n".join(lines)


def format_details(player_name: str, total: int, mode_id: str,
                   selected: int, records: list[dict], hero_names: dict[str, str],
                   failed: int = 0, role_id: object = "", list_only: bool = False, *,
                   queried_at: float | None = None, samples: list[dict] | None = None,
                   recent_season_name: str = "", query_notice: str = "") -> str:
    label = MODES.get(mode_id, f"模式 {mode_id}")
    lines = [*format_identity(player_name, role_id),
             *format_query_metadata(queried_at),
             *([f"对局赛季范围：{recent_season_name}"] if recent_season_name else []),
             *([query_notice] if query_notice else []),
             format_sample_period(records, selected, samples),
             f"查询到对局 {total} 场｜模式：{label}",
             f"选中 {selected} 场｜有效对局 {len(records)} 场"]
    if failed:
        lines.append(f"详情未成功 {failed} 场")
    if not records:
        return "\n".join(lines + ["暂无可展示的单局详情。"])
    lines.append("有效对局列表（新→旧）：" if list_only else "有效对局详情（新→旧）：")
    if list_only:
        lines.append("当前未请求单局详情；恢复、振刀、武器、魂玉等详情指标不可用。")
    for index, record in enumerate(records, 1):
        hero = _hero_label(record.get("hero_id"), hero_names)
        ts = record.get("time")
        ts = _timestamp_ms(ts)
        time_str = datetime.fromtimestamp(ts / 1000, BEIJING).strftime("%m-%d %H:%M") if ts else "—"
        delta = record.get("rating_delta")
        delta_str = f"{delta:+.0f}" if delta is not None else "—"
        lines.append(
            f"第{index}局｜英雄：{hero}｜时间：{time_str}\n"
            f"地图：{record.get('map') or '—'}｜评价：{record.get('grade') or '—'}\n"
            f"排名：{_integer(record.get('rank'))}｜参赛人数：{_integer(record.get('total_users'))}\n"
            f"积分：{_integer(record.get('rating'))}｜积分变化：{delta_str}\n"
            f"击杀：{_integer(record.get('kills'))}｜伤害：{_integer(record.get('damage'))}\n"
            f"恢复：{_integer(record.get('recovery'))}｜振刀：{_integer(record.get('parry'))}｜"
            f"存活：{_whole(record.get('survival_min'), '分')}"
        )
        if not list_only:
            lines.extend((
                f"使用钩索：{_integer(record.get('grapple_used'))}｜钩索命中：{_integer(record.get('grapple_hit'))}",
                f"招式命中：{_integer(record.get('move_hits'))}｜释放技能：{_integer(record.get('skills'))}",
                f"救援：{_integer(record.get('rescues'))}｜复活数：{_integer(record.get('revives'))}",
                f"移动距离：{_whole(record.get('distance_km'), 'km')}｜消耗货币：{_integer(record.get('currency'))}",
                f"武器（伤害占比）：{'、'.join(record.get('weapons') or []) or '—'}",
                f"魂玉：{'、'.join(record.get('souls') or []) or '—'}",
                f"标签：{'、'.join(record.get('tags') or []) or '—'}",
            ))
        lines.append("")
    return "\n".join(lines).strip()


def split_detail_report(report: str, max_chars: int = 1200) -> list[str]:
    """Keep each QQ message reasonably short without splitting a match."""
    sections = report.split("\n\n")
    chunks = []
    current = ""
    for section in sections:
        if current and len(current) + len(section) + 2 > max_chars:
            chunks.append(current)
            current = section
        else:
            current = f"{current}\n\n{section}" if current else section
    if current:
        chunks.append(current)
    return chunks
