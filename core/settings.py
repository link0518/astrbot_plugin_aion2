"""配置项的分组、范围与校验。

_conf_schema.json 只描述类型与说明，缺少数值范围、单位、分组和联动关系，
面板要用的这些信息补在这里，避免把展示细节写进前端。

校验是服务端职责：面板传来的值一律当作不可信输入，按这里的规则重新收一遍。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SCHEMA_FILE = "_conf_schema.json"

# 配置面板的分区，顺序即展示顺序
GROUPS: tuple[dict[str, str], ...] = (
    {
        "key": "query",
        "label": "查询区域",
        "hint": "决定向哪个分片取数。各分片的服务器与角色互相独立，切换后查询结果随之变化。",
    },
    {
        "key": "output",
        "label": "输出与展示",
        "hint": "卡片渲染失败时会自动回退为纯文本，不会因为渲染问题中断查询。",
    },
    {"key": "trigger", "label": "触发方式", "hint": ""},
    {"key": "i18n", "label": "中文化", "hint": ""},
    {
        "key": "push",
        "label": "活动提醒",
        "hint": "时刻表为本地推算，不依赖上游接口；官方临时调整不会同步。",
    },
    {
        "key": "kinah",
        "label": "基纳价格",
        "hint": "每小时抓取各区的基纳行情并渲染成一张图缓存下来，群里发「基纳」即可取图。数据来自交易所公开挂单，仅供参考。",
    },
    {
        "key": "network",
        "label": "网络与缓存",
        "hint": "上游会限流，请求频率与缓存时间是保护措施，不建议为了「实时」把它们调激进。",
    },
)

# 每个配置项的补充信息。key 必须出现在 _conf_schema.json 里，否则会被忽略。
FIELDS: tuple[dict[str, Any], ...] = (
    {"key": "region", "group": "query", "control": "select"},
    {"key": "output_mode", "group": "output", "control": "select"},
    {"key": "image_width", "group": "output", "min": 320, "max": 1600, "step": 20, "unit": "px"},
    {"key": "max_list_items", "group": "output", "min": 1, "max": 50, "unit": "条"},
    {"key": "free_trigger", "group": "trigger"},
    {"key": "chinese_names", "group": "i18n"},
    {"key": "t2s", "group": "i18n"},
    {"key": "event_push", "group": "push"},
    {"key": "event_lead", "group": "push", "min": 1, "max": 60, "unit": "分钟", "depends": ("event_push",)},
    {"key": "event_remind_rift", "group": "push", "depends": ("event_push",)},
    {"key": "event_remind_minigame", "group": "push", "depends": ("event_push",)},
    {"key": "event_rift_hours", "group": "push", "control": "minutes", "list_max": 23, "depends": ("event_push", "event_remind_rift")},
    {"key": "event_minigame_minutes", "group": "push", "control": "minutes", "list_max": 59, "depends": ("event_push", "event_remind_minigame")},
    {"key": "event_invasion_minute", "group": "push", "min": 0, "max": 59, "unit": "分"},
    {"key": "event_reset_hour", "group": "push", "min": 0, "max": 23, "unit": "时"},
    {"key": "quiet_start", "group": "push", "control": "clock", "depends": ("event_push",)},
    {"key": "quiet_end", "group": "push", "control": "clock", "depends": ("event_push",)},
    {"key": "kinah_enable", "group": "kinah"},
    {"key": "kinah_interval", "group": "kinah", "min": 600, "max": 86400, "unit": "秒", "depends": ("kinah_enable",)},
    {"key": "kinah_source_7881", "group": "kinah", "depends": ("kinah_enable",)},
    {"key": "kinah_source_pa", "group": "kinah", "depends": ("kinah_enable",)},
    {"key": "kinah_zones", "group": "kinah", "depends": ("kinah_enable",)},
    {"key": "rate_limit", "group": "network", "min": 0.5, "max": 20.0, "step": 0.5, "unit": "次/秒"},
    {"key": "request_timeout", "group": "network", "min": 3, "max": 120, "unit": "秒"},
    {"key": "cache_ttl", "group": "network", "min": 0, "max": 86400, "unit": "秒"},
    {"key": "glossary_ttl", "group": "network", "min": 3600, "max": 7776000, "unit": "秒"},
)

# 选项值到中文标签。schema 里只有值，面板要显示人话。
OPTION_LABELS: dict[str, dict[str, str]] = {
    "output_mode": {"image": "图片卡片", "text": "纯文本"},
}

CLOCK_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# 字符串类配置的长度上限，防止把超长文本写进配置
MAX_TEXT = 200

# 「整数列表」类配置接受的写法：逗号、空格或顿号分隔；上限控制列表长度
LIST_SPLIT_RE = re.compile(r"[,\s、，]+")
MAX_LIST = 64


def schema_path(root: Path) -> Path:
    """插件根目录下的配置 schema 路径。"""
    return Path(root) / SCHEMA_FILE


def load_schema(root: Path) -> dict[str, dict]:
    """读取 _conf_schema.json，读不到就返回空表（面板会退化为只读）。"""
    try:
        raw = json.loads(schema_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, dict)}


def _field_spec(key: str) -> dict[str, Any] | None:
    for item in FIELDS:
        if item["key"] == key:
            return item
    return None


def _options_of(key: str, spec: dict) -> list[dict[str, str]]:
    """把 schema 的 options 转成带标签的列表，区域名用插件自己的中文名。"""
    raw = spec.get("options")
    if not isinstance(raw, list):
        return []
    labels = OPTION_LABELS.get(key, {})
    if key == "region":
        from .routes import REGIONS

        labels = {k: v.label for k, v in REGIONS.items()}
    out = []
    for value in raw:
        text = str(value)
        out.append({"value": text, "label": labels.get(text, text)})
    return out


def describe(schema: dict[str, dict], values: dict, defaults: dict) -> list[dict]:
    """合并 schema、补充信息与当前值，生成面板要渲染的分组。"""
    by_group: dict[str, list[dict]] = {g["key"]: [] for g in GROUPS}
    for key, spec in schema.items():
        meta = _field_spec(key)
        if meta is None:
            continue  # schema 里有、这里没登记的配置项不进面板
        item = {
            "key": key,
            "label": spec.get("description") or key,
            "hint": spec.get("hint") or "",
            "type": spec.get("type") or "string",
            "control": meta.get("control") or ("select" if spec.get("options") else "text"),
            "options": _options_of(key, spec),
            "value": values.get(key, defaults.get(key)),
            "default": defaults.get(key),
            "unit": meta.get("unit", ""),
            "min": meta.get("min"),
            "max": meta.get("max"),
            "step": meta.get("step"),
            "listMax": meta.get("list_max"),
            "depends": list(meta.get("depends", ())),
        }
        by_group.setdefault(meta["group"], []).append(item)

    return [
        {
            "key": group["key"],
            "label": group["label"],
            "hint": group["hint"],
            "fields": by_group.get(group["key"], []),
        }
        for group in GROUPS
        if by_group.get(group["key"])
    ]


def defaults_of(schema: dict[str, dict]) -> dict:
    """schema 里登记的默认值。"""
    return {k: v.get("default") for k, v in schema.items()}


def editable_keys(schema: dict[str, dict]) -> list[str]:
    """允许面板写入的键。schema 与元数据同时登记才认。"""
    return [k for k in schema if _field_spec(k) is not None]


def validate(key: str, raw: Any, schema: dict[str, dict]) -> tuple[Any, str]:
    """校验并转换一个配置值，返回 (值, 错误)。错误非空表示不通过。"""
    if key not in schema:
        return None, f"未知配置项 {key}"
    meta = _field_spec(key)
    if meta is None:
        return None, f"配置项 {key} 未登记，拒绝写入"

    spec = schema[key]
    kind = spec.get("type") or "string"

    if kind == "bool":
        if not isinstance(raw, bool):
            return None, f"{key} 需要布尔值"
        return raw, ""

    if kind in ("int", "float"):
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            return None, f"{key} 需要数值"
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return None, f"{key} 需要数值"
        if number != number or number in (float("inf"), float("-inf")):
            return None, f"{key} 不是有效的数值"
        if kind == "int":
            if number != int(number):
                return None, f"{key} 需要整数"
            number = int(number)
        lower, upper = meta.get("min"), meta.get("max")
        if lower is not None and number < lower:
            return None, f"{key} 不能小于 {lower}"
        if upper is not None and number > upper:
            return None, f"{key} 不能大于 {upper}"
        return number, ""

    # 字符串类
    if not isinstance(raw, str):
        return None, f"{key} 需要文本"
    text = raw.strip()
    if len(text) > MAX_TEXT:
        return None, f"{key} 过长（上限 {MAX_TEXT} 字）"
    options = spec.get("options")
    if isinstance(options, list) and options:
        allowed = [str(o) for o in options]
        if text not in allowed:
            return None, f"{key} 只能是 {'、'.join(allowed)}"
        return text, ""
    if meta.get("control") == "clock":
        if text and not CLOCK_RE.match(text):
            return None, f"{key} 需要 HH:MM 格式，或留空表示不静默"
        return text, ""
    if meta.get("control") == "minutes":
        hours = meta.get("list_max", 23)
        if not text:
            return None, f"{key} 不能为空，需要一串用逗号分隔的整数"
        parts = [p for p in LIST_SPLIT_RE.split(text) if p]
        if len(parts) > MAX_LIST:
            return None, f"{key} 最多 {MAX_LIST} 个值"
        numbers = []
        for part in parts:
            if not re.fullmatch(r"\d{1,2}", part):
                return None, f"{key} 里的「{part}」不是 0–{hours} 的整数"
            value = int(part)
            if value > hours:
                return None, f"{key} 不能超过 {hours}"
            numbers.append(value)
        deduped = sorted(set(numbers))
        return ",".join(str(n) for n in deduped), ""
    return text, ""


def validate_payload(payload: dict, schema: dict[str, dict]) -> tuple[dict, list[str]]:
    """校验一批待写入的值，返回 (通过的值, 错误列表)。"""
    if not isinstance(payload, dict):
        return {}, ["请求体需要是对象"]
    clean: dict[str, Any] = {}
    errors: list[str] = []
    for key, raw in payload.items():
        value, error = validate(str(key), raw, schema)
        if error:
            errors.append(error)
        else:
            clean[str(key)] = value
    return clean, errors


def normalize(values: dict, schema: dict[str, dict]) -> dict:
    """把存量配置收一遍，用于发现越界的历史值（不写回，只报告）。"""
    issues = []
    for key in editable_keys(schema):
        if key not in values:
            continue
        _, error = validate(key, values[key], schema)
        if error:
            issues.append({"key": key, "value": values[key], "message": error})
    return {"issues": issues}
