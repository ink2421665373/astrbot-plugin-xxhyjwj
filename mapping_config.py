"""Validated mapping settings shared by config schema, views and queries."""
import copy
import hashlib
import json
import re

MAPPING_KEYS = ("hero_mappings", "season_mappings")


def validate_mappings(payload):
    if not isinstance(payload, dict):
        raise ValueError("请求内容必须是对象")
    result = {}
    for field, id_key, name_key, template in (
        ("hero_mappings", "hero_id", "hero_name", "hero"),
        ("season_mappings", "season_id", "season_name", "season"),
    ):
        entries = payload.get(field)
        if not isinstance(entries, list) or len(entries) > 500:
            raise ValueError(f"{field} 必须是列表，且最多500项")
        normalized, seen = [], set()
        for index, entry in enumerate(entries, 1):
            if not isinstance(entry, dict):
                raise ValueError(f"{field} 第{index}项必须是对象")
            code, name = entry.get(id_key), entry.get(name_key)
            if not isinstance(code, str) or not isinstance(name, str):
                raise ValueError(f"第{index}项的编码和名称必须是文字")
            code, name = code.strip(), name.strip()
            if not code or len(code) > 96 or any(char.isspace() or ord(char) < 32 for char in code):
                raise ValueError(f"第{index}项编码不能为空，且不能包含空白或控制字符")
            if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
                raise ValueError(f"第{index}项名称不能为空，且最多80字")
            if field == "hero_mappings":
                if not re.fullmatch(r"[0-9]{1,32}", code):
                    raise ValueError(f"英雄第{index}项编码必须是数字")
                if len(code) < 2:
                    code = code.zfill(2)
            if code in seen:
                raise ValueError(f"{field} 存在重复编码：{code}")
            seen.add(code)
            row = {"__template_key": template, id_key: code, name_key: name}
            if field == "season_mappings":
                current = entry.get("is_current", False)
                if not isinstance(current, bool):
                    raise ValueError("查询赛季开关必须是布尔值")
                row["is_current"] = current
            normalized.append(row)
        result[field] = normalized
    if sum(row["is_current"] for row in result["season_mappings"]) > 1:
        raise ValueError("最多只能指定一个查询赛季")
    return result


def mapping_snapshot(config):
    # Never expose the rest of the plugin settings or authentication state.
    data = {key: copy.deepcopy(config.get(key, [])) for key in MAPPING_KEYS}
    revision = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return dict(data, revision=revision)


def merge_season_options(options, mappings):
    rows = [dict(item) for item in options if isinstance(item, dict) and item.get("key")] if isinstance(options, list) else []
    by_key = {str(item["key"]): item for item in rows}
    if not isinstance(mappings, list):
        return rows
    # Reuse strict validation for mappings edited through the ordinary WebUI.
    validated = validate_mappings({"hero_mappings": [], "season_mappings": mappings})["season_mappings"]
    for item in validated:
        code = item["season_id"]
        row = by_key.get(code)
        if row is None:
            row = {"key": code}
            rows.append(row)
            by_key[code] = row
        row.update(value=item["season_name"], _custom=True, _manual_current=item["is_current"])
    return rows


def collect_season_catalog(existing, options):
    """Collect the whole returned catalog; preserve all operator overrides."""
    if not isinstance(options, list) or not options:
        raise ValueError("接口未返回赛季列表，现有配置未改动")
    current = validate_mappings({"hero_mappings": [], "season_mappings": existing})["season_mappings"]
    known = {item["season_id"] for item in current}
    received, skipped, added = set(), 0, 0
    for item in options:
        if not isinstance(item, dict) or item.get("key") is None or item.get("value") is None:
            skipped += 1
            continue
        code, name = str(item["key"]).strip(), str(item["value"]).strip()
        try:
            row = validate_mappings({"hero_mappings": [], "season_mappings": [
                dict(season_id=code, season_name=name, is_current=False)
            ]})["season_mappings"][0]
        except ValueError:
            skipped += 1
            continue
        received.add(code)
        if code not in known:
            current.append(row)
            known.add(code)
            added += 1
    if not received:
        raise ValueError("接口赛季列表没有有效的编码/名称，现有配置未改动")
    # Never silently truncate the catalog, even when exceeding the form limit.
    validate_mappings({"hero_mappings": [], "season_mappings": current})
    return current, dict(received_count=len(options), catalog_count=len(received), added_count=added, skipped_count=skipped)
