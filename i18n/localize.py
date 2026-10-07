"""中文化：术语词表查表 + 繁体转简体。

上游只有台服提供中文，且是繁体；国际服全是英文。
固定术语（职业、属性、部位、品级等）查词表，动态文本（道具名、称号、公告）
走繁转简。词表里没有的一律回退原文，不做机器直译。
"""

import json
from functools import lru_cache
from pathlib import Path

TERMS_PATH = Path(__file__).with_name("terms.json")

# 繁转简是可选依赖，缺失时保留原文并提示一次
_converter = None
_converter_ready = False

# 是否启用繁转简，由插件初始化时按配置设置
_SIMPLIFY = True


def set_simplify(enabled: bool) -> None:
    """开关繁转简。需在任何查询发生前调用，切换时会清空已有缓存。"""
    global _SIMPLIFY
    _SIMPLIFY = bool(enabled)
    to_simplified.cache_clear()


def _load_terms() -> dict:
    try:
        return json.loads(TERMS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


TERMS: dict = _load_terms()


@lru_cache(maxsize=4096)
def to_simplified(text: str) -> str:
    """把繁体文本转成简体；关掉转换或没有可用的转换库时原样返回。"""
    global _converter, _converter_ready
    if not text or not _SIMPLIFY:
        return text
    if not _converter_ready:
        _converter_ready = True
        try:
            from zhconv import convert

            _converter = lambda value: convert(value, "zh-cn")  # noqa: E731
        except ImportError:
            _converter = None
    return _converter(text) if _converter else text


def _table(name: str) -> dict:
    table = TERMS.get(name)
    return table if isinstance(table, dict) else {}


def class_name(class_id: int = 0, fallback: str = "") -> str:
    """职业名，用于职业列表接口（那里的 id 才是职业编号）。"""
    table = _table("classes")
    hit = table.get(str(class_id)) if class_id else None
    if hit:
        return hit
    by_name = _table("classes_by_name")
    return by_name.get(fallback, fallback)


def pc_class_name(pc_id: int = 0, fallback: str = "") -> str:
    """职业名，用于角色。角色给的是 pcId，它是 职业×性别×种族 的编码，
    与职业列表的 id 不是一套编号，拿 pcId 去查职业表会张冠李戴。"""
    hit = _table("pc_classes").get(str(pc_id)) if pc_id else None
    if hit:
        return hit
    return class_name(0, fallback)


def stat_name(stat_type: str, fallback: str = "") -> str:
    hit = _table("stats").get(stat_type)
    if hit:
        return hit
    return to_simplified(fallback) if fallback else stat_type


def slot_name(slot: str) -> str:
    return _table("slots").get(slot, slot)


def grade_name(grade: str, fallback: str = "") -> str:
    """品级名。词表优先，上游给的中文品级名次之（国际服返回的是英文）。"""
    hit = _table("grades").get(grade)
    if hit:
        return hit
    return to_simplified(fallback) if fallback else grade


def skill_category(category: str) -> str:
    return _table("skill_categories").get(category, category)


def race_name(value) -> str:
    return _table("races").get(str(value), str(value))


def gender_name(value) -> str:
    return _table("genders").get(str(value), str(value))


def server_name(name: str) -> str:
    return _table("servers").get(name, name)


def board_name(name: str) -> str:
    return _table("boards").get(name, name)


def region_name(key: str) -> str:
    return _table("regions").get(key, key)


def item_type(value: str) -> str:
    return _table("item_types").get(value, value)


def localized(text: str) -> str:
    """动态文本的统一入口：繁转简。"""
    return to_simplified(text or "")
