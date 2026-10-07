"""上游 JSON 的数据模型。

字段取自实际接口返回，命名与上游保持一致，缺失字段一律给默认值，
避免上游某个字段暂时为空时整个查询失败。
"""

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

PORTRAIT_ORIGIN = "https://profileimg.plaync.com"

# 上游时间戳是 UTC，而插件面向的用户与活动时刻表都按北京时间，统一换算过来
BEIJING = timezone(timedelta(hours=8))

# 上游会在搜索命中的片段外包一层高亮标签，直接输出会把标签发给用户
_TAG = re.compile(r"<[^>]*>")


def _g(raw: Any, key: str, default: Any = None) -> Any:
    if not isinstance(raw, dict):
        return default
    value = raw.get(key)
    return default if value is None else value


def _int(raw: Any, key: str, default: int = 0) -> int:
    try:
        return int(_g(raw, key, default))
    except (TypeError, ValueError):
        return default


def _str(raw: Any, key: str, default: str = "") -> str:
    value = _g(raw, key, default)
    return str(value) if value is not None else default


def clean_text(value: str) -> str:
    """去掉上游文本里的高亮标签并还原 HTML 实体，用于直接展示的字段。"""
    if not value or ("<" not in value and "&" not in value):
        return value
    return html.unescape(_TAG.sub("", value))


def _text(raw: Any, key: str, default: str = "") -> str:
    """取一个展示用文本字段。搜索结果的角色名会带 <strong> 高亮标签。"""
    return clean_text(_str(raw, key, default))


def _epoch(raw: Any, key: str) -> datetime | None:
    """取一个秒级时间戳，换算成北京时间交给展示层。

    按 UTC 解释原始值后转东八区，否则北京时间凌晨发布的公告会整体差一天。
    另外上游这个字段并不总是发布时间（维护公告会给出尚未到来的时刻），
    展示时只当时间戳用，不要写成「发布于」。
    """
    seconds = _int(raw, key, 0)
    return datetime.fromtimestamp(seconds, tz=BEIJING) if seconds > 0 else None


def _portrait(url: str) -> str:
    if not url:
        return ""
    return url if url.startswith("http") else PORTRAIT_ORIGIN + url


def _occupied(raw: Any, factory):
    """宠物、翅膀这类可选槽位：上游没装备时会给一个字段全为 null 的对象，
    不能据此造出一个空对象出来。"""
    if not isinstance(raw, dict):
        return None
    return factory(raw) if (_int(raw, "id") or _str(raw, "name")) else None


@dataclass
class Server:
    server_id: int = 0
    race_id: int = 0
    name: str = ""
    short_name: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> "Server":
        return cls(
            server_id=_int(raw, "serverId"),
            race_id=_int(raw, "raceId"),
            name=_str(raw, "serverName"),
            short_name=_str(raw, "serverShortName"),
        )


@dataclass
class GameClass:
    id: int = 0
    name: str = ""  # 英文标识，各区域一致
    text: str = ""  # 区域语言下的名称

    @classmethod
    def from_raw(cls, raw: Any) -> "GameClass":
        return cls(id=_int(raw, "id"), name=_str(raw, "name"), text=_str(raw, "text"))


@dataclass
class CharacterRef:
    server_id: int = 0
    character_id: str = ""


@dataclass
class CharacterSummary:
    ref: CharacterRef = field(default_factory=CharacterRef)
    name: str = ""
    level: int = 0
    race_id: int = 0
    pc_id: int = 0  # 职业编码，不是职业表的 id
    server_id: int = 0
    server_name: str = ""
    image_url: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> "CharacterSummary":
        server_id = _int(raw, "serverId")
        pc_id = _int(raw, "pcId")
        return cls(
            ref=CharacterRef(server_id=server_id, character_id=_str(raw, "characterId")),
            name=_text(raw, "name"),
            level=_int(raw, "level"),
            race_id=_int(raw, "race"),
            pc_id=pc_id,
            server_id=server_id,
            server_name=_str(raw, "serverName"),
            image_url=_portrait(_str(raw, "profileImageUrl")),
        )


@dataclass
class Page:
    page: int = 1
    size: int = 0
    total: int = 0
    last_page: int = 1
    items: list[CharacterSummary] = field(default_factory=list)

    @classmethod
    def from_raw(cls, raw: Any) -> "Page":
        pagination = _g(raw, "pagination", {})
        rows = _g(raw, "list", []) or []
        return cls(
            page=_int(pagination, "page", 1),
            size=_int(pagination, "size"),
            total=_int(pagination, "total"),
            last_page=_int(pagination, "endPage", 1),
            items=[CharacterSummary.from_raw(row) for row in rows],
        )


@dataclass
class Stat:
    type: str = ""
    name: str = ""
    value: int = 0
    effects: list[str] = field(default_factory=list)

    @classmethod
    def from_raw(cls, raw: Any) -> "Stat":
        return cls(
            type=_str(raw, "type"),
            name=_text(raw, "name"),
            value=_int(raw, "value"),
            effects=[_str(e, "desc") for e in (_g(raw, "statSecondList", []) or [])],
        )


@dataclass
class Title:
    id: int = 0
    name: str = ""
    grade: str = ""
    category: str = ""
    total: int = 0
    owned: int = 0
    owned_percent: int = 0
    stats: list[str] = field(default_factory=list)
    equip_stats: list[str] = field(default_factory=list)

    @classmethod
    def from_raw(cls, raw: Any) -> "Title":
        return cls(
            id=_int(raw, "id"),
            name=_text(raw, "name"),
            grade=_str(raw, "grade"),
            category=_str(raw, "equipCategory"),
            total=_int(raw, "totalCount"),
            owned=_int(raw, "ownedCount"),
            owned_percent=_int(raw, "ownedPercent"),
            stats=[_str(s, "desc") for s in (_g(raw, "statList", []) or [])],
            equip_stats=[_str(s, "desc") for s in (_g(raw, "equipStatList", []) or [])],
        )


@dataclass
class TitleSummary:
    total: int = 0
    owned: int = 0
    titles: list[Title] = field(default_factory=list)

    @classmethod
    def from_raw(cls, raw: Any) -> "TitleSummary":
        return cls(
            total=_int(raw, "totalCount"),
            owned=_int(raw, "ownedCount"),
            titles=[Title.from_raw(t) for t in (_g(raw, "titleList", []) or [])],
        )


@dataclass
class DaevanionSummary:
    id: int = 0
    name: str = ""
    icon: str = ""
    open: bool = False
    open_nodes: int = 0
    total_nodes: int = 0
    open_percent: int = 0

    @classmethod
    def from_raw(cls, raw: Any) -> "DaevanionSummary":
        return cls(
            id=_int(raw, "id"),
            name=_text(raw, "name"),
            icon=_str(raw, "icon"),
            open=bool(_int(raw, "open")),
            open_nodes=_int(raw, "openNodeCount"),
            total_nodes=_int(raw, "totalNodeCount"),
            open_percent=_int(raw, "openPercent"),
        )


@dataclass
class Profile:
    name: str = ""
    level: int = 0
    pc_id: int = 0  # 职业编码，不是职业表的 id
    class_name: str = ""
    gender_id: int = 0
    gender_name: str = ""
    race_id: int = 0
    race_name: str = ""
    server_id: int = 0
    server_name: str = ""
    # 上游字段名为 regionName，实测同一服务器上不同角色取值不同（有值也有空），
    # 是按角色的军团名，不是地区
    guild_name: str = ""
    combat_power: int = 0
    title_name: str = ""
    title_grade: str = ""
    image_url: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> "Profile":
        return cls(
            name=_text(raw, "characterName"),
            level=_int(raw, "characterLevel"),
            pc_id=_int(raw, "pcId"),
            class_name=_str(raw, "className"),
            gender_id=_int(raw, "gender"),
            gender_name=_str(raw, "genderName"),
            race_id=_int(raw, "raceId"),
            race_name=_str(raw, "raceName"),
            server_id=_int(raw, "serverId"),
            server_name=_str(raw, "serverName"),
            guild_name=_str(raw, "regionName"),
            combat_power=_int(raw, "combatPower"),
            title_name=_text(raw, "titleName"),
            title_grade=_str(raw, "titleGrade"),
            image_url=_portrait(_str(raw, "profileImage")),
        )


@dataclass
class Character:
    ref: CharacterRef = field(default_factory=CharacterRef)
    profile: Profile = field(default_factory=Profile)
    stats: list[Stat] = field(default_factory=list)
    titles: TitleSummary = field(default_factory=TitleSummary)
    daevanion: list[DaevanionSummary] = field(default_factory=list)

    @classmethod
    def from_raw(cls, raw: Any, ref: CharacterRef | None = None) -> "Character":
        profile = Profile.from_raw(_g(raw, "profile", {}))
        if ref is None:
            ref = CharacterRef(server_id=profile.server_id, character_id="")
        d_board = _g(raw, "daevanion", {})
        return cls(
            ref=ref,
            profile=profile,
            stats=[Stat.from_raw(s) for s in (_g(_g(raw, "stat", {}), "statList", []) or [])],
            titles=TitleSummary.from_raw(_g(raw, "title", {})),
            daevanion=[
                DaevanionSummary.from_raw(b) for b in (_g(d_board, "boardList", []) or [])
            ],
        )


@dataclass
class EquipSlot:
    slot_pos: int = 0
    slot_pos_name: str = ""
    item_id: int = 0
    name: str = ""
    grade: str = ""
    enchant_level: int = 0
    exceed_level: int = 0
    icon: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> "EquipSlot":
        return cls(
            slot_pos=_int(raw, "slotPos"),
            slot_pos_name=_str(raw, "slotPosName"),
            item_id=_int(raw, "id"),
            name=_text(raw, "name"),
            grade=_str(raw, "grade"),
            enchant_level=_int(raw, "enchantLevel"),
            exceed_level=_int(raw, "exceedLevel"),
            icon=_str(raw, "icon"),
        )


@dataclass
class Pet:
    id: int = 0
    name: str = ""
    level: int = 0
    icon: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> "Pet":
        return cls(
            id=_int(raw, "id"),
            name=_text(raw, "name"),
            level=_int(raw, "level"),
            icon=_str(raw, "icon"),
        )


@dataclass
class Wing:
    id: int = 0
    name: str = ""
    grade: str = ""
    enchant_level: int = 0
    icon: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> "Wing":
        return cls(
            id=_int(raw, "id"),
            name=_text(raw, "name"),
            grade=_str(raw, "grade"),
            enchant_level=_int(raw, "enchantLevel"),
            icon=_str(raw, "icon"),
        )


@dataclass
class Skill:
    id: int = 0
    name: str = ""
    level: int = 0
    category: str = ""
    acquired: bool = False
    equipped: bool = False
    need_level: int = 0
    icon: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> "Skill":
        return cls(
            id=_int(raw, "id"),
            name=_text(raw, "name"),
            level=_int(raw, "skillLevel"),
            category=_str(raw, "category"),
            acquired=bool(_int(raw, "acquired")),
            equipped=bool(_int(raw, "equip")),
            need_level=_int(raw, "needLevel"),
            icon=_str(raw, "icon"),
        )


@dataclass
class Equipment:
    slots: list[EquipSlot] = field(default_factory=list)
    skins: list[EquipSlot] = field(default_factory=list)
    pet: Pet | None = None
    wing: Wing | None = None
    wing_skin: Wing | None = None
    skills: list[Skill] = field(default_factory=list)

    @classmethod
    def from_raw(cls, raw: Any) -> "Equipment":
        gear = _g(raw, "equipment", {})
        petwing = _g(raw, "petwing", {})
        return cls(
            slots=[EquipSlot.from_raw(s) for s in (_g(gear, "equipmentList", []) or [])],
            skins=[EquipSlot.from_raw(s) for s in (_g(gear, "skinList", []) or [])],
            pet=_occupied(_g(petwing, "pet"), Pet.from_raw),
            wing=_occupied(_g(petwing, "wing"), Wing.from_raw),
            wing_skin=_occupied(_g(petwing, "wingSkin"), Wing.from_raw),
            skills=[
                Skill.from_raw(s)
                for s in (_g(_g(raw, "skill", {}), "skillList", []) or [])
            ],
        )


@dataclass
class ItemStat:
    id: str = ""
    name: str = ""
    value: str = ""
    min_value: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> "ItemStat":
        return cls(
            id=_str(raw, "id"),
            name=_text(raw, "name"),
            value=str(_g(raw, "value", "")),
            min_value=str(_g(raw, "minValue", "")),
        )


@dataclass
class Item:
    id: int = 0
    name: str = ""
    grade: str = ""
    grade_name: str = ""
    icon: str = ""
    level: int = 0
    equip_level: int = 0
    category_name: str = ""
    type: str = ""
    race_name: str = ""
    class_names: list[str] = field(default_factory=list)
    magic_stone_slots: int = 0
    god_stone_slots: int = 0
    max_enchant_level: int = 0
    main_stats: list[ItemStat] = field(default_factory=list)
    costumes: list[str] = field(default_factory=list)

    @classmethod
    def from_raw(cls, raw: Any) -> "Item":
        return cls(
            id=_int(raw, "id"),
            name=_text(raw, "name"),
            grade=_str(raw, "grade"),
            grade_name=_str(raw, "gradeName"),
            icon=_str(raw, "icon"),
            level=_int(raw, "level"),
            equip_level=_int(raw, "equipLevel"),
            category_name=_str(raw, "categoryName"),
            type=_str(raw, "type"),
            race_name=_str(raw, "raceName"),
            class_names=[str(c) for c in (_g(raw, "classNames", []) or [])],
            magic_stone_slots=_int(raw, "magicStoneSlotCount"),
            god_stone_slots=_int(raw, "godStoneSlotCount"),
            max_enchant_level=_int(raw, "maxEnchantLevel"),
            main_stats=[ItemStat.from_raw(s) for s in (_g(raw, "mainStats", []) or [])],
            costumes=[str(c) for c in (_g(raw, "costumes", []) or [])],
        )


@dataclass
class Author:
    name: str = ""
    ref: CharacterRef = field(default_factory=CharacterRef)
    official: bool = False


@dataclass
class Post:
    id: str = ""
    board: str = ""
    board_name: str = ""
    title: str = ""
    summary: str = ""
    views: int = 0
    comments: int = 0
    official: bool = False
    thumbnail_url: str = ""
    author: Author | None = None
    posted_at: datetime | None = None
    updated_at: datetime | None = None
    html: str = ""

    @classmethod
    def from_raw(cls, raw: Any, board: str = "") -> "Post":
        writer = _g(raw, "writer", {})
        game_user = _g(writer, "gameUser", {})
        category = _g(raw, "categoryBoard", {})
        author = None
        if _str(game_user, "characterId"):
            author = Author(
                name=_text(game_user, "characterName"),
                ref=CharacterRef(
                    server_id=_int(game_user, "gameServerId"),
                    character_id=_str(game_user, "characterId"),
                ),
            )
        return cls(
            id=_str(raw, "id"),
            board=board,
            board_name=_str(category, "boardName"),
            title=_text(raw, "title"),
            summary=_text(raw, "summary"),
            views=_int(_g(raw, "reactions", {}), "viewCount"),
            comments=_int(_g(raw, "reactions", {}), "commentCount"),
            official=bool(_g(_g(writer, "loginUser", {}), "admin", False)),
            thumbnail_url=_str(raw, "thumbnailUrl"),
            author=author,
            posted_at=_epoch(_g(raw, "timestamps", {}), "postedEpoch"),
            updated_at=_epoch(_g(raw, "timestamps", {}), "updatedEpoch"),
        )


@dataclass
class PostBody:
    """帖子正文，content 为上游返回的 HTML。"""

    id: str = ""
    title: str = ""
    html: str = ""
    written_at: datetime | None = None

    @classmethod
    def from_raw(cls, raw: Any) -> "PostBody":
        article = _g(raw, "article", {})
        meta = _g(article, "contentMeta", {})
        content = _g(article, "content", {})
        return cls(
            id=_str(meta, "id"),
            title=_text(meta, "title"),
            html=_str(content, "content"),
            written_at=_epoch(_g(meta, "timestamps", {}), "postedEpoch"),
        )
