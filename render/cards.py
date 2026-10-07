"""卡片上下文构建与纯文本回退。

卡片用 Jinja2 渲染 HTML，交给 AstrBot 截图；渲染不可用时回退到这里的纯文本版本。
道具与装备名按 id 从台服取中文，取不到就保留上游英文名。
"""

import re
from datetime import datetime, time, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..core import Aion2Client, Character, Equipment, Item, Profile, events
from ..i18n import (
    board_name,
    class_name,
    gender_name,
    grade_name,
    item_type,
    localized,
    pc_class_name,
    race_name,
    server_name,
    skill_category,
    slot_name,
    stat_name,
)

TEMPLATE_DIR = Path(__file__).with_name("templates")

env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

# 装备卡片上最多展示的技能数，避免技能多的角色把图拉得过长
MAX_SKILLS = 18

# 技能名来自上游原文（国际服英文、台服繁中），没有译名来源，
# 只能按是否含中日韩汉字判断中文名有没有
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


async def _glossary_names(client: Aion2Client, ids: list[int]) -> dict[int, str]:
    """取中文译名并转简体，台服返回的是繁体。"""
    names = await client.item_names_zh(ids)
    return {key: localized(value) for key, value in names.items()}


def _zh_stat_names(info: dict) -> dict[str, str]:
    """台服主属性译名，key 是属性 id（各区一致），值已转简体。"""
    raw = info.get("stats") if isinstance(info, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {str(key): localized(str(value)) for key, value in raw.items()}


def render(kind: str, context: dict) -> str:
    return env.get_template(f"{kind}.html").render(**context)


# --------------------------------------------------------------- 上下文


async def character_context(
    client: Aion2Client, ch: Character, *, width: int = 720, note: str = ""
) -> dict:
    p = ch.profile
    stats = [
        {
            "name": stat_name(s.type, s.name),
            "value": s.value,
            "effects": [localized(e) for e in s.effects],
        }
        for s in ch.stats
        if s.type != "ItemLevel"
    ]
    item_level = next((s for s in ch.stats if s.type == "ItemLevel"), None)
    if item_level is not None:
        stats.insert(0, {"name": "道具等级", "value": item_level.value, "effects": []})

    total = ch.titles.total or 1
    return {
        "width": width,
        "name": localized(p.name),
        "level": p.level,
        "class_name": pc_class_name(p.pc_id, localized(p.class_name)),
        "race": race_name(p.race_id or p.race_name),
        "gender": gender_name(p.gender_id or p.gender_name),
        "server": server_name(p.server_name),
        "guild": localized(p.guild_name),
        "combat_power": f"{p.combat_power:,}",
        "title": localized(p.title_name),
        "portrait": p.image_url,
        "stats": stats,
        "titles_total": ch.titles.total,
        "titles_owned": ch.titles.owned,
        "title_pct": round(ch.titles.owned / total * 100),
        "daevanion": [
            {
                "name": board_name(b.name),
                "percent": b.open_percent,
                "open_nodes": b.open_nodes,
                "total_nodes": b.total_nodes,
            }
            for b in ch.daevanion
        ],
        "region_label": client.region.label,
        "note": note,
        "fetched_at": _now(),
    }


async def equipment_context(
    client: Aion2Client,
    equip: Equipment,
    profile: Profile | None = None,
    *,
    width: int = 720,
    note: str = "",
) -> dict:
    ids = [s.item_id for s in equip.slots + equip.skins]
    ids += [x.id for x in (equip.pet, equip.wing, equip.wing_skin) if x is not None]
    names = await _glossary_names(client, ids)

    def slot_of(s) -> dict:
        zh = names.get(s.item_id)
        return {
            "slot": slot_name(s.slot_pos_name),
            "name": zh or localized(s.name),
            "zh": bool(zh),
            "grade": s.grade,
            "grade_label": grade_name(s.grade),
            "enchant": s.enchant_level,
            "icon": s.icon,
        }

    companions = []
    if equip.pet is not None:
        companions.append(
            {
                "slot": "宠物",
                "name": names.get(equip.pet.id) or localized(equip.pet.name),
                "grade": "",
                "grade_label": "",
                "icon": equip.pet.icon,
            }
        )
    for label, wing in (("翅膀", equip.wing), ("翅膀外观", equip.wing_skin)):
        if wing is not None:
            companions.append(
                {
                    "slot": label,
                    "name": names.get(wing.id) or localized(wing.name),
                    "grade": wing.grade,
                    "grade_label": grade_name(wing.grade),
                    "icon": wing.icon,
                }
            )

    skills = [s for s in equip.skills if s.acquired] or equip.skills

    def missing_note(ids: list[int]) -> str:
        """官方没有中文译名时如实标注，不臆造翻译。"""
        if ids and not all(names.get(i) for i in ids):
            return "官方未提供中文名"
        return ""

    return {
        "width": width,
        "name": localized(profile.name) if profile else "",
        "level": profile.level if profile else 0,
        "class_name": pc_class_name(profile.pc_id, localized(profile.class_name))
        if profile
        else "",
        "server": server_name(profile.server_name) if profile else "",
        "combat_power": f"{profile.combat_power:,}" if profile else "0",
        "portrait": profile.image_url if profile else "",
        "slots": [slot_of(s) for s in equip.slots],
        "skins": [slot_of(s) for s in equip.skins],
        "skins_note": missing_note([s.item_id for s in equip.skins]),
        "companions": companions,
        "companions_note": missing_note(
            [x.id for x in (equip.pet, equip.wing, equip.wing_skin) if x is not None]
        ),
        "skills": [
            {
                "name": localized(s.name),
                "level": s.level,
                "category": skill_category(s.category),
                "zh": bool(_CJK.search(s.name)),
            }
            for s in skills[:MAX_SKILLS]
        ],
        "skill_total": len(equip.skills),
        "region_label": client.region.label,
        "note": note,
        "fetched_at": _now(),
    }


async def item_context(
    client: Aion2Client, item: Item, *, width: int = 720, note: str = ""
) -> dict:
    info = await client.item_zh(item.id)
    zh_name = localized(str(info.get("name") or ""))
    zh_stats = _zh_stat_names(info)

    # 品类、种族、外观都优先用台服中文；台服没有该道具时回退国际服原文
    category = localized(str(info.get("category") or "")) or slot_name(item.category_name)
    race = localized(str(info.get("race") or "")) or race_name(item.race_name)
    costumes = [localized(str(c)) for c in (info.get("costumes") or [])] or [
        localized(c) for c in item.costumes
    ]

    basics = [{"name": "装备等级", "value": item.equip_level or item.level}]
    if item.magic_stone_slots:
        basics.append({"name": "魔石孔位", "value": item.magic_stone_slots})
    if item.god_stone_slots:
        basics.append({"name": "神石孔位", "value": item.god_stone_slots})
    if item.max_enchant_level:
        basics.append({"name": "强化上限", "value": f"+{item.max_enchant_level}"})
    if race:
        basics.append({"name": "可用种族", "value": race})

    main_stats = []
    for s in item.main_stats:
        try:
            low, high = int(float(s.min_value)), int(float(s.value))
        except (TypeError, ValueError):
            low = high = 0
        main_stats.append(
            {
                "name": zh_stats.get(s.id) or localized(s.name),
                "value": f"{low} ~ {high}" if high > low else str(high or s.value),
                "range": f"{low} ~ {high}" if high > low else "",
            }
        )

    return {
        "width": width,
        "item_id": item.id,
        "name": zh_name or localized(item.name),
        "grade": item.grade,
        "grade_label": grade_name(item.grade, item.grade_name),
        "category": category,
        "type_label": item_type(item.type),
        "icon": item.icon,
        "basics": basics,
        "main_stats": main_stats,
        "classes": [class_name(0, c) for c in item.class_names],
        "costumes": costumes,
        "note": note,
        "name_note": "" if zh_name else "该道具暂无官方中文名",
        "fetched_at": _now(),
    }


# ------------------------------------------------------------ 纯文本回退


def character_text(ch: Character) -> str:
    p = ch.profile
    lines = [
        f"【{localized(p.name)}】{server_name(p.server_name)}",
        f"Lv{p.level} {pc_class_name(p.pc_id, localized(p.class_name))} "
        f"{race_name(p.race_id or p.race_name)}·{gender_name(p.gender_id or p.gender_name)}",
        f"战斗力 {p.combat_power:,}",
    ]
    if p.title_name:
        lines.append(f"称号 {localized(p.title_name)}")
    stats = [s for s in ch.stats if s.type != "ItemLevel"]
    if stats:
        lines.append("")
        lines.append("属性")
        for s in stats[:12]:
            lines.append(f"  {stat_name(s.type, s.name)} {s.value}")
    if ch.titles.total:
        lines.append("")
        lines.append(f"称号收集 {ch.titles.owned}/{ch.titles.total}")
    if ch.daevanion:
        lines.append("")
        lines.append("守护者板")
        for b in ch.daevanion:
            lines.append(f"  {board_name(b.name)} {b.open_nodes}/{b.total_nodes} ({b.open_percent}%)")
    return "\n".join(lines)


async def equipment_text(client: Aion2Client, equip: Equipment, profile: Profile | None = None) -> str:
    ids = [s.item_id for s in equip.slots + equip.skins]
    ids += [x.id for x in (equip.pet, equip.wing, equip.wing_skin) if x is not None]
    names = await _glossary_names(client, ids)
    head = ""
    if profile is not None:
        head = (
            f"【{localized(profile.name)}】Lv{profile.level} "
            f"{pc_class_name(profile.pc_id, localized(profile.class_name))} "
            f"{server_name(profile.server_name)}　战斗力 {profile.combat_power:,}\n"
        )
    lines = [head + "装备面板"] if head else ["装备面板"]
    for s in equip.slots:
        name = names.get(s.item_id) or localized(s.name)
        enchant = f" +{s.enchant_level}" if s.enchant_level else ""
        lines.append(f"  {slot_name(s.slot_pos_name)}：{name}{enchant}")
    if equip.skins:
        lines.append("")
        lines.append("时装外观")
        for s in equip.skins:
            name = names.get(s.item_id) or localized(s.name)
            lines.append(f"  {slot_name(s.slot_pos_name)}：{name}")
    for label, wing in (("宠物", equip.pet), ("翅膀", equip.wing), ("翅膀外观", equip.wing_skin)):
        if wing is not None:
            name = names.get(wing.id) or localized(wing.name)
            lines.append(f"  {label}：{name}")
    if equip.skills:
        lines.append("")
        lines.append(f"技能（共 {len(equip.skills)} 个）")
        chunk = "、".join(
            f"{localized(s.name)}{s.level}" for s in equip.skills[:MAX_SKILLS]
        )
        lines.append("  " + chunk)
    return "\n".join(lines)


async def item_text(client: Aion2Client, item: Item) -> str:
    info = await client.item_zh(item.id)
    zh_name = localized(str(info.get("name") or ""))
    zh_stats = _zh_stat_names(info)
    category = localized(str(info.get("category") or "")) or slot_name(item.category_name)
    race = localized(str(info.get("race") or "")) or race_name(item.race_name)
    lines = [
        f"【{zh_name or localized(item.name)}】",
        f"品级 {grade_name(item.grade, item.grade_name)}｜"
        f"{category}｜{item_type(item.type)}｜ID {item.id}",
    ]
    if item.equip_level:
        lines.append(f"装备等级 {item.equip_level}")
    if item.magic_stone_slots or item.god_stone_slots:
        lines.append(f"魔石孔位 {item.magic_stone_slots}｜神石孔位 {item.god_stone_slots}")
    if race:
        lines.append(f"可用种族 {race}")
    if item.class_names:
        lines.append("可用职业 " + "、".join(class_name(0, c) for c in item.class_names))
    if item.main_stats:
        lines.append("")
        lines.append("主属性")
        for s in item.main_stats:
            try:
                low, high = int(float(s.min_value)), int(float(s.value))
            except (TypeError, ValueError):
                low = high = 0
            text = f"{low} ~ {high}" if high > low else str(high or s.value)
            lines.append(f"  {zh_stats.get(s.id) or localized(s.name)} {text}")
    return "\n".join(lines)


def _next_label(now: datetime, occ, tomorrow: bool) -> str:
    if occ is None or tomorrow:
        return ""
    return f"{occ.clock}（{events.countdown(now, occ.start)}）"


def events_context(
    *, now: datetime | None = None, tomorrow: bool = False, width: int = 720
) -> dict:
    """活动时刻表卡片。全部为本地推算，不需要客户端。"""
    moment = (now or datetime.now()).replace(second=0, microsecond=0)
    day = (moment + timedelta(days=1)).date() if tomorrow else moment.date()
    next_rift = _next_label(moment, events.next_occurrence(moment, events.KIND_RIFT), tomorrow)
    return {
        "width": width,
        "day_label": "明日" if tomorrow else "今日",
        "date_label": f"{day:%Y-%m-%d} {events.WEEKDAYS[day.weekday()]}",
        "now_label": f"现在 {moment:%H:%M}",
        # 明日卡片没有「下一次」，改为标出当天首场，免得右上角空着
        "head_label": "首场裂隙" if tomorrow else "下一次裂隙",
        "head_value": f"{events.RIFT_HOURS[0]:02d}:00"
        if tomorrow
        else (next_rift or "—"),
        "next_rift": next_rift,
        "next_game": _next_label(moment, events.next_occurrence(moment, events.KIND_MINIGAME), tomorrow),
        "rifts": [
            {
                "time": f"{hour:02d}:00",
                "state": ""
                if tomorrow
                else events.state_of(
                    datetime.combine(day, time(hour, 0)), moment, events.RIFT_DURATION
                ),
            }
            for hour in events.RIFT_HOURS
        ],
        "cells": events.timeline(day, moment),
        "sets": [
            {"label": f":{minute:02d}", "games": "、".join(games)}
            for minute, games in zip(events.MINIGAME_MINUTES, events.MINIGAME_SETS)
        ],
        "others": [
            {"name": "次元入侵", "value": f"每小时 :{events.INVASION_MINUTE:02d}"},
            {"name": "每日重置", "value": f"{events.RESET_HOUR:02d}:00"},
            {"name": "每周重置", "value": f"周三 {events.RESET_HOUR:02d}:00"},
        ],
        "state_labels": events.STATE_LABELS,
        "stamp": _now(),
    }


def events_text(now: datetime | None = None, *, tomorrow: bool = False) -> str:
    return events.text_table(now, tomorrow=tomorrow)


def search_text(page) -> str:
    if not page.items:
        return "没有找到匹配的角色。"
    lines = [f"共 {page.total} 条，第 {page.page}/{page.last_page} 页："]
    for i, c in enumerate(page.items, 1):
        lines.append(
            f"{i}. {localized(c.name)}  Lv{c.level} "
            f"{pc_class_name(c.pc_id)}·{race_name(c.race_id)} {server_name(c.server_name)}"
        )
    lines.append("")
    lines.append("用「角色 <角色名>」查看详情。")
    return "\n".join(lines)
