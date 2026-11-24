"""Bounded, in-memory API diagnostics built from an explicit data allowlist."""
from collections import deque
import copy
from datetime import datetime, timezone
import math
import re

LIMIT = 50
SENSITIVE = re.compile(r"cookie|token|authorization|password|secret|credential|heybox_id|device_id|hkey|nonce|凭证|密码", re.I)
NUMERIC = re.compile(r"[+\-\d.,\s/%万千亿kKmMiInNhHsS秒分小时次场米公里人个胜败:—–]+")


def safe_text(value, limit=80):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    value = str(value)
    if SENSITIVE.search(value) or len(value) > limit or any(ord(c) < 32 for c in value):
        return "[已隐藏]"
    return value


def metric_value(value):
    if value is None:
        return dict(value=None, state="null")
    if isinstance(value, str) and not value.strip():
        return dict(value=None, state="empty_value")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return dict(value=value, state="zero" if value == 0 else "value")
    if isinstance(value, str) and len(value) <= 80 and NUMERIC.fullmatch(value.strip()):
        value = value.strip()
        if value in ("—", "–", "-", "/"):
            return dict(value=value, state="missing")
        if not re.search(r"\d", value):
            return dict(value=None, state="not_numeric")
        zero = re.fullmatch(r"[+\-]?0+(?:\.0+)?\s*(?:%|万|千|亿|k|km|min|h|s|秒|分|小时|次|场|米|公里|人|个|胜|败)?", value, re.I)
        return dict(value=value, state="zero" if zero else "value")
    # Never return arbitrary strings, nested objects or error messages.
    return dict(value=None, state="not_numeric")


def field_state(body, key, expected):
    if key not in body:
        return "missing"
    if body[key] is None:
        return "null"
    if not isinstance(body[key], expected):
        return "wrong_type"
    return "present" if body[key] else "empty"


def metrics(rows, expected):
    if not isinstance(rows, list):
        rows = []
    result = []
    names = set()
    for item in rows[:200]:
        if not isinstance(item, dict):
            continue
        name = safe_text(item.get("desc"))
        if not name or name == "[已隐藏]":
            continue
        names.add(name)
        result.append(dict(name=name, **(metric_value(item["value"]) if "value" in item else dict(value=None, state="missing_value"))))
    for name in expected:
        if name not in names:
            result.append(dict(name=name, value=None, state="outside_preview" if len(rows) > 200 else "absent"))
    return result


class DiagnosticStore:
    def __init__(self):
        self.records = deque(maxlen=LIMIT)
        self.sequence = 0

    def append(self, kind, mode, season, body=None, *, elapsed_ms=0, error=""):
        self.sequence += 1
        home = kind == "home"
        original_body = body
        body = body if isinstance(body, dict) else {}
        record = dict(
            id=self.sequence, captured_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            endpoint="/game/yjwj/home/data" if home else "/game/yjwj/match/detail",
            kind="home" if home else "detail", mode=safe_text(mode, 96),
            season=safe_text(season, 96), elapsed_ms=max(0, round(elapsed_ms)),
            status="error" if error else "ok", error=error if error in (
                "HeyboxLoginRequired", "HeyboxRateLimited", "HeyboxNetworkError", "TimeoutError", "ValueError"
            ) else ("request_failed" if error else ""),
            response_state="object" if isinstance(original_body, dict) else "not_object",
        )
        if error:
            self.records.appendleft(record)
            return
        metric_key = "overview" if home else "data"
        expected = ("场均伤害", "场均振刀", "场均击杀") if home else ("总伤害", "振刀", "击杀")
        rows = body.get(metric_key)
        record.update(
            fields={metric_key: field_state(body, metric_key, list)},
            metrics=metrics(rows, expected), metric_count=len(rows) if isinstance(rows, list) else 0,
            metrics_truncated=isinstance(rows, list) and len(rows) > 200,
        )
        if home:
            seasons = body.get("seasons")
            record["fields"].update(seasons=field_state(body, "seasons", list), player_info=field_state(body, "player_info", dict))
            record["seasons"] = []
            record["season_count"] = len(seasons) if isinstance(seasons, list) else 0
            record["seasons_truncated"] = isinstance(seasons, list) and len(seasons) > 500
            for item in (seasons if isinstance(seasons, list) else [])[:500]:
                if not isinstance(item, dict):
                    continue
                row = dict(code=safe_text(item.get("key"), 96), name=safe_text(item.get("value")))
                for flag in ("selected", "is_current", "current"):
                    if flag in item:
                        row[flag] = str(item[flag]).lower() if isinstance(item[flag], (bool, int)) else str(item[flag]) if item[flag] in ("true", "false", "0", "1") else "[未识别]"
                record["seasons"].append(row)
        self.records.appendleft(record)

    def snapshot(self):
        return dict(records=copy.deepcopy(list(self.records)), limit=LIMIT, memory_only=True)

    def clear(self):
        self.records.clear()
