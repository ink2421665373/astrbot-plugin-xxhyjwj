"""Validated mapping settings shared by config schema, views and queries."""
import copy
import hashlib
import json
import re

MAPPING_KEYS = ("hero_mappings", "season_mappings")

# 昵称 → 角色唯一标识（role_id）的对照表，供指令与 LLM 工具共用
NICKNAME_KEYS = ("nickname_mappings",)

# 小黑盒端游角色主键是**字母数字混合**的 22 位串（4 位随机前缀 + 18 位数字），
# 例如 2i67000021275100070163、i03c000022562500120163、km1r000078541000270163。
# 它绝不等于纯数字，所以这里只做「可见字母数字」的形状校验，避免把真实 ID 挡在门外。
ROLE_ID_PATTERN = re.compile(r"[0-9A-Za-z]{1,64}")


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


def _normalize_nickname(value: str) -> str:
    """对照时的比较键：去空白、统一小写、去掉常见零宽字符。"""
    cleaned = "".join(
        char for char in str(value or "") if char.isprintable() and ord(char) not in (0x200B, 0x200C, 0x200D, 0xFEFF)
    )
    return cleaned.strip().lower()


def nickname_key(nickname: str) -> str:
    """对外暴露的归一化函数，供查询链路做精确匹配。"""
    return _normalize_nickname(nickname)


def validate_nickname_mappings(entries) -> list[dict]:
    """校验昵称映射表：[{nickname, role_id, role_name?, server?}]。

    * ``nickname`` 必填，最多 80 字
    * ``role_id`` 必填，小黑盒 ``game_id``：**字母数字混合**（形如 ``2i67000021275100070163``），最多 64 位
    * 归一化后的昵称不可重复
    """
    if not isinstance(entries, list) or len(entries) > 500:
        raise ValueError("nickname_mappings 必须是列表，且最多500项")
    normalized, seen = [], set()
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"昵称第{index}项必须是对象")
        nickname = entry.get("nickname")
        role_id = entry.get("role_id")
        if not isinstance(nickname, str) or not isinstance(role_id, str):
            raise ValueError(f"昵称第{index}项的昵称和角色ID必须是文字")
        nickname = nickname.strip()
        role_id = role_id.strip()
        if not nickname or len(nickname) > 80 or any(ord(char) < 32 for char in nickname):
            raise ValueError(f"昵称第{index}项昵称不能为空，且最多80字")
        if not role_id or len(role_id) > 64 or not ROLE_ID_PATTERN.fullmatch(role_id):
            raise ValueError(
                f"昵称第{index}项角色ID格式不正确：应为小黑盒角色ID（字母和数字，"
                f"如 2i67000021275100070163），不要手填其它内容"
            )
        key = _normalize_nickname(nickname)
        if not key:
            raise ValueError(f"昵称第{index}项昵称为空白内容")
        if key in seen:
            raise ValueError(f"存在重复昵称：{nickname}")
        seen.add(key)
        row = {"nickname": nickname, "role_id": role_id}
        for field in ("role_name", "server"):
            text = entry.get(field)
            if isinstance(text, str) and text.strip() and len(text.strip()) <= 80:
                row[field] = text.strip()
        normalized.append(row)
    return normalized


def nickname_lookup(config, nickname: str) -> dict | None:
    """按归一化昵称查已保存的 role_id；没配就返回 None。"""
    key = _normalize_nickname(nickname)
    if not key:
        return None
    for row in config.get("nickname_mappings", []) or []:
        if not isinstance(row, dict):
            continue
        if _normalize_nickname(row.get("nickname") or "") == key:
            role_id = str(row.get("role_id") or "").strip()
            if role_id:
                return dict(row, role_id=role_id)
    return None


def mapping_snapshot(config):
    # Never expose the rest of the plugin settings or authentication state.
    data = {key: copy.deepcopy(config.get(key, [])) for key in MAPPING_KEYS}
    data.update({key: copy.deepcopy(config.get(key, [])) for key in NICKNAME_KEYS})
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
