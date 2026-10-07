"""AION2 查询插件：指令与大模型工具。

数据来自 AION2 官方站点。界面与输出中文化：
职业、属性、部位、品级等固定术语查词表，道具与装备名按 id 从台服反查中文，
公告、称号等动态文本做繁转简。

指令是平铺的（角色、装备、道具…），带不带斜杠都能触发。
活动时刻表（时空裂隙、小游戏）为本地推算，并在开场前推送给订阅过的会话。
"""

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

from . import render
from .core import (
    BOARDS,
    GLOSSARY_REGION,
    RANKING_LABELS,
    REGIONS,
    Aion2Client,
    Aion2Error,
    CharacterRef,
    NoSeason,
    Page,
    clean_text,
    events,
)
from .core.cache import GlossaryStore
from .i18n import class_name, localized, pc_class_name, race_name, server_name, set_simplify
from .webapi import ConsoleAPI

PLUGIN_NAME = "astrbot_plugin_aion2"

# 插件目录，配置面板要读同目录的 _conf_schema.json
PLUGIN_DIR = Path(__file__).resolve().parent

# 搜索时取多少条候选，够判断是否唯一匹配
SEARCH_PAGE_SIZE = 20

# 卡片截图参数，长图交给 full_page
IMAGE_OPTIONS = {"type": "png", "full_page": True, "timeout": 30}

# 候选列表的等待时间：这段时间内回序号才认，超时就当没这回事
CHOICE_TTL = 10.0

# 活动提醒的轮询间隔，要短于 events.pending_reminders 的命中窗口（60 秒）
EVENT_TICK = 20.0

# 无斜杠触发的句式。带参数的指令要求整条消息就是「角色 mizoo」这种写法，
# 不带参数的指令要求整条消息正好是那个词，免得群里闲聊提到「服务器」就被拦下来。
FREE_RULES = (
    ("角色", re.compile(r"^角色\s+(\S{1,24})(?:\s+(\d{1,6}))?$")),
    ("装备", re.compile(r"^装备\s+(\S{1,24})(?:\s+(\d{1,6}))?$")),
    ("道具", re.compile(r"^道具\s+(\d{1,12})$")),
    ("公告", re.compile(r"^公告(?:\s+(\S{1,16}))?$")),
    ("排行", re.compile(r"^排行(?:\s+(\S{1,16}))?$")),
    ("活动", re.compile(r"^活动(?:\s+(\S{1,8}))?$")),
    ("服务器", re.compile(r"^服务器$")),
    ("职业", re.compile(r"^职业$")),
    ("区域", re.compile(r"^区域$")),
    ("帮助", re.compile(r"^(?:帮助|help)$")),
)

# 活动提醒的订阅与已推记录，落在 data 目录，重启后订阅不丢、已推不重发
SUB_KEY = "sub:"
SENT_KEY = "sent:"

# 改动这些配置要重建客户端才生效，其余项每次读取时自然生效
CLIENT_KEYS = frozenset({"region", "rate_limit", "request_timeout", "cache_ttl", "chinese_names"})


@dataclass
class _Pending:
    """一次等待用户挑候选的上下文。

    同一会话里只保留最后一次，挑完或超时即失效。
    """

    kind: str  # 角色 / 装备
    keyword: str
    candidates: list  # CharacterSummary 列表，下标即用户回的序号
    expires_at: float


def _resolve_option(value: str, labels: dict[str, str]) -> str:
    """把板块、榜单这类选项解析成上游用的键。

    用户看到的是中文名，输入的也可能是中文名，两种都接受。
    """
    raw = (value or "").strip()
    key = raw.lower()
    if key in labels:
        return key
    for k, label in labels.items():
        if label == raw or label.lower() == key:
            return k
    return ""


def _option_hint(labels: dict[str, str]) -> str:
    return "、".join(f"{label}（{key}）" for key, label in labels.items())


def _wrap(cells: list[str], per_line: int = 4, indent: str = "  ") -> list[str]:
    """把一长串条目折成每行几个，避免日服 36 个服挤成 190 字符的一行。"""
    return [
        indent + "　".join(cells[i : i + per_line]) for i in range(0, len(cells), per_line)
    ]


def _data_dir() -> Path:
    """插件的持久化目录，放在 AstrBot 的 data 下以免插件更新时被覆盖。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        base = Path(get_astrbot_data_path())
    except Exception:  # noqa: BLE001 - 拿不到就退回相对路径
        base = Path("data")
    path = base / "plugin_data" / PLUGIN_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


class Aion2Plugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.plugin_dir = PLUGIN_DIR
        # 要在任何查询发生前设置，否则译名缓存里会混入未转换的结果
        set_simplify(config.get("t2s", True))
        self._client: Aion2Client | None = None
        self._glossary = GlossaryStore(
            _data_dir() / "glossary.json", int(config.get("glossary_ttl", 2592000))
        )
        # 会话 → 等待挑选的候选，超时即废
        self._pending: dict[str, _Pending] = {}
        # 活动提醒的订阅与已推记录：订阅长期有效，已推记录一小时过期
        self._subs = GlossaryStore(_data_dir() / "event_subs.json", 31536000)
        self._sent = GlossaryStore(_data_dir() / "event_sent.json", 3600)
        self._event_task: asyncio.Task | None = None
        # 配置面板的后端接口，注册到 WebUI 的插件详情页
        self._console = ConsoleAPI(self)
        self._console.register(PLUGIN_NAME)

    # ------------------------------------------------------------ 基础设施

    async def initialize(self):
        """按配置启动活动提醒的后台循环。"""
        self._ensure_event_loop()

    def _ensure_event_loop(self) -> None:
        """起后台循环。

        正常由 AstrBot 在插件加载后调用 initialize；万一某个版本没有这一步，
        收到第一条消息时也会补上，免得提醒功能静默失效。
        """
        if self._event_task is not None or not self.config.get("event_push", True):
            return
        try:
            self._event_task = asyncio.create_task(self._event_loop())
        except RuntimeError:  # 当前没有运行中的事件循环，等下一次
            self._event_task = None

    def _build_client(self) -> Aion2Client:
        region = str(self.config.get("region", "asia")).strip().lower()
        if region not in REGIONS:
            logger.warning(f"未知的查询区域 {region}，回退到 asia")
            region = "asia"
        return Aion2Client(
            region_key=region,
            rate_limit=float(self.config.get("rate_limit", 5.0)),
            timeout=int(self.config.get("request_timeout", 15)),
            cache_ttl=int(self.config.get("cache_ttl", 600)),
            glossary=self._glossary if self.config.get("chinese_names", True) else None,
            logger=logger,
        )

    @property
    def client(self) -> Aion2Client:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def peek_client(self) -> Aion2Client | None:
        """已存在的客户端，没有就返回 None。

        面板只是展示状态，不该因为打开一次页面就建出连接池。
        """
        return self._client

    async def _drop_client(self) -> None:
        """关掉现有客户端，下次查询按新配置重建。"""
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.close()
        except Exception as exc:  # noqa: BLE001 - 关闭失败不应阻塞配置生效
            logger.warning(f"关闭 AION2 客户端时出错：{exc}")

    async def terminate(self):
        await self._stop_event_loop()
        await self._drop_client()
        self._glossary.save()
        self._subs.save()
        self._sent.save()

    async def _stop_event_loop(self) -> None:
        task, self._event_task = self._event_task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - 停用阶段的异常只记日志
            logger.warning(f"停止活动提醒循环时出错：{exc}")

    # ------------------------------------------------------- 配置面板能力

    async def apply_config(self, changed: dict) -> None:
        """让新配置立即生效：能就地改的就地改，改不了的把客户端重建。"""
        if not changed:
            return
        if "t2s" in changed:
            set_simplify(self.config.get("t2s", True))
        if "glossary_ttl" in changed:
            self._glossary.ttl = max(60, int(self.config.get("glossary_ttl", 2592000)))
        if CLIENT_KEYS & set(changed):
            await self._drop_client()
        if self.config.get("event_push", True):
            self._ensure_event_loop()
        else:
            await self._stop_event_loop()

    def status(self) -> dict:
        """运行状态快照，供配置面板展示。"""
        now = datetime.now()
        region_key = str(self.config.get("region", "asia")).strip().lower()
        region = REGIONS.get(region_key)
        client = self.peek_client()
        cache = (
            client.cache_stats()
            if client is not None
            else {"entries": 0, "ttl": int(self.config.get("cache_ttl", 600))}
        )
        span = self._quiet_range()
        upcoming = []
        for kind in (events.KIND_RIFT, events.KIND_MINIGAME):
            occ = events.next_occurrence(now, kind)
            if occ is None:
                continue
            upcoming.append(
                {
                    "kind": kind,
                    "name": occ.name,
                    "clock": occ.clock,
                    "detail": occ.detail,
                    "countdown": events.countdown(now, occ.start),
                    "minutes": int((occ.start - now).total_seconds() // 60),
                }
            )
        task = self._event_task
        # 已推记录的键是「事件@日期时间」，按当天的日期片段统计今日已推
        today_tag = f"@{now:%Y-%m-%d}"
        return {
            "now": f"{now:%Y-%m-%d %H:%M:%S}",
            "region": {"key": region_key, "label": region.label if region else region_key},
            "push": {
                "enabled": bool(self.config.get("event_push", True)),
                "lead": self._lead(),
                "kinds": [events.KIND_NAMES[k] for k in self._push_kinds()],
                "quiet": f"{self._clock(span[0])}–{self._clock(span[1])}" if span else "",
                "quietNow": self._in_quiet(now),
                "running": task is not None and not task.done(),
                "subscribers": len(self.subscribers()),
                "sent": len(
                    [
                        k
                        for k in self._sent.keys()
                        if k.startswith(SENT_KEY) and today_tag in k
                    ]
                ),
            },
            "cache": {
                "entries": cache["entries"],
                "ttl": cache["ttl"],
                "glossary": len(self._glossary),
            },
            "next": upcoming,
        }

    async def selftest(self) -> dict:
        """按当前配置真实请求一次上游，用独立的客户端以免污染正常缓存。"""
        client = self._build_client()
        started = time.perf_counter()
        try:
            rows = await client.servers()
            return {
                "ok": True,
                "region": client.region.label,
                "servers": len(rows),
                "ms": int((time.perf_counter() - started) * 1000),
            }
        except Aion2Error as exc:
            return {"ok": False, "hint": exc.hint, "detail": exc.detail}
        except Exception as exc:  # noqa: BLE001 - 自检要把失败原因带回面板
            return {"ok": False, "hint": "自检失败", "detail": str(exc)}
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - 探测用的连接关不掉无所谓
                pass

    async def send_test(self, target: str) -> dict:
        """向指定会话发一条测试消息，验证提醒链路是否通。"""
        text = "AION2 查询插件：这是一条测试消息，收到说明提醒链路正常。"
        try:
            await self.context.send_message(target, MessageChain().message(text))
        except Exception as exc:  # noqa: BLE001 - 失败原因要回显给面板
            return {"ok": False, "detail": str(exc)}
        return {"ok": True}

    def subscription_list(self) -> list[dict]:
        """订阅列表与订阅时间，供面板展示。"""
        rows = []
        for target in self.subscribers():
            stamp = self._subs.get(SUB_KEY + target)
            since = ""
            if isinstance(stamp, (int, float)):
                since = datetime.fromtimestamp(float(stamp)).strftime("%m-%d %H:%M")
            rows.append({"target": target, "since": since})
        return sorted(rows, key=lambda row: row["target"])

    def unsubscribe(self, target: str) -> bool:
        """按会话标识退订，返回是否真的删掉了。"""
        if target not in self.subscribers():
            return False
        self._subs.remove(SUB_KEY + target)
        self._subs.save()
        return True

    def clear_glossary(self) -> int:
        """清空译名缓存，返回清掉的条数。"""
        count = len(self._glossary)
        self._glossary.clear()
        self._glossary.save()
        return count

    def _width(self) -> int:
        try:
            return max(320, min(int(self.config.get("image_width", 720)), 1600))
        except (TypeError, ValueError):
            return 720

    def _image_mode(self) -> bool:
        return str(self.config.get("output_mode", "image")) == "image"

    def _limit(self) -> int:
        try:
            return max(1, min(int(self.config.get("max_list_items", 10)), 50))
        except (TypeError, ValueError):
            return 10

    async def _emit(self, event: AstrMessageEvent, kind: str, ctx: dict, text):
        """按配置输出卡片，渲染不可用时回退纯文本。

        text 可以传字符串，也可以传返回字符串的可调用对象：图片模式下用不到
        纯文本，延迟到回退时才计算，省掉一次多余的取数。
        """
        if self._image_mode():
            try:
                html = render.render(kind, ctx)
                url = await self.html_render(html, options=dict(IMAGE_OPTIONS))
                return event.image_result(url)
            except Exception as exc:  # noqa: BLE001 - 渲染失败不影响查询本身
                logger.warning(f"卡片渲染失败，回退为文本输出：{exc}")
        if callable(text):
            text = text()
            if asyncio.iscoroutine(text):
                text = await text
        return event.plain_result(str(text))

    # ------------------------------------------------------------ 角色定位

    async def _search(self, keyword: str, server_id: int = 0) -> Page:
        return await self.client.search_characters(
            keyword, server_id=server_id, size=SEARCH_PAGE_SIZE
        )

    def _candidates(
        self,
        event: AstrMessageEvent,
        page: Page,
        keyword: str,
        kind: str,
        interactive: bool = True,
    ) -> str:
        """重名时列出候选。

        interactive 为真时挂起等待，用户回序号就能接着查；
        大模型工具那条链路不走序号（下一条消息由模型处理），只给服务器 ID 写法。
        """
        if not page.items:
            return (
                f"没有找到名为「{keyword}」的角色。"
                "请确认角色名与当前查询区域是否对应，名称需要与游戏内完全一致。"
            )
        shown = page.items[: self._limit()]
        if interactive:
            head = (
                f"「{keyword}」匹配到 {page.total} 个角色，回复序号继续查询"
                f"（{int(CHOICE_TTL)} 秒内有效）："
            )
        else:
            head = f"「{keyword}」匹配到 {page.total} 个角色，请补充服务器再查："
        lines = [head]
        for i, c in enumerate(shown, 1):
            prefix = f"{i}. " if interactive else ""
            lines.append(
                f"  {prefix}{localized(c.name)}　Lv{c.level} "
                f"{pc_class_name(c.pc_id)}·{race_name(c.race_id)}　"
                f"{server_name(c.server_name)}（{c.server_id}）"
            )
        if page.total > len(shown):
            lines.append(
                f"  仅列出前 {len(shown)} 个，可改用「{kind} {keyword} 服务器ID」精确查询。"
            )
        if interactive:
            self._remember(event, kind, keyword, shown)
        else:
            lines.append("")
            lines.append(f"例：{kind} {keyword} {shown[0].server_id}")
        return "\n".join(lines)

    def _session_key(self, event: AstrMessageEvent) -> str:
        """会话 + 发送者，保证同一群里各人各等各的。"""
        umo = getattr(event, "unified_msg_origin", "") or ""
        getter = getattr(event, "get_sender_id", None)
        sender = ""
        if callable(getter):
            try:
                sender = str(getter())
            except Exception:  # noqa: BLE001 - 取不到就当匿名，不影响主流程
                sender = ""
        return f"{umo}|{sender}"

    def _remember(self, event: AstrMessageEvent, kind: str, keyword: str, items) -> None:
        self._prune()
        self._pending[self._session_key(event)] = _Pending(
            kind=kind,
            keyword=keyword,
            candidates=list(items),
            expires_at=time.monotonic() + CHOICE_TTL,
        )

    def _prune(self) -> None:
        now = time.monotonic()
        for key in [k for k, p in self._pending.items() if p.expires_at <= now]:
            self._pending.pop(key, None)

    async def _pick(self, event: AstrMessageEvent, text: str):
        """把用户回的序号接回上一次查询。超时或没有上下文就不响应。"""
        key = self._session_key(event)
        pending = self._pending.get(key)
        if pending is None:
            return
        if pending.expires_at <= time.monotonic():
            self._pending.pop(key, None)
            return
        count = len(pending.candidates)
        try:
            index = int(text)
        except ValueError:
            return
        if not 1 <= index <= count:
            yield event.plain_result(f"序号超出范围，请输入 1-{count} 之间的数字。")
            return
        self._pending.pop(key, None)
        summary = pending.candidates[index - 1]
        renderer = self._render_character if pending.kind == "角色" else self._render_equipment
        try:
            async for result in renderer(event, summary):
                yield result
        except Aion2Error as exc:
            yield event.plain_result(f"查询失败：{exc.hint}（{exc.detail}）")

    async def _render_character(self, event: AstrMessageEvent, summary):
        ch = await self.client.character(self._ref(summary))
        ctx = await render.character_context(
            self.client, ch, width=self._width(), note=self._capability_note()
        )
        yield await self._emit(event, "character", ctx, lambda: render.character_text(ch))

    async def _render_equipment(self, event: AstrMessageEvent, summary):
        ref = self._ref(summary)
        ch = await self.client.character(ref)
        equip = await self.client.equipment(ref)
        ctx = await render.equipment_context(self.client, equip, ch.profile, width=self._width())
        yield await self._emit(
            event,
            "equipment",
            ctx,
            lambda: render.equipment_text(self.client, equip, ch.profile),
        )

    async def _locate(self, keyword: str, server_id: int = 0):
        """定位角色。唯一匹配返回摘要，否则返回 None 与候选页。

        搜索是模糊匹配并会在命中片段上包高亮标签，所以先按去标签后的
        名字做一次精确比对：精确命中只有一个时直接用它，不必让用户翻候选。
        """
        page = await self._search(keyword, server_id)
        wanted = keyword.strip().lower()
        exact = [c for c in page.items if c.name.strip().lower() == wanted]
        if len(exact) == 1:
            return exact[0], None
        if len(page.items) == 1:
            return page.items[0], None
        return None, page

    def _ref(self, summary) -> CharacterRef:
        return CharacterRef(server_id=summary.server_id, character_id=summary.ref.character_id)

    # ------------------------------------------------------------- 指令

    @filter.command("帮助", alias={"help"})
    async def help(self, event: AstrMessageEvent):
        """查看 AION2 查询插件的用法。"""
        region = self.client.region
        lines = [
            f"AION2 查询（当前区域：{region.label}）",
            "",
            "角色 <角色名> [服务器ID]　角色档案",
            "装备 <角色名> [服务器ID]　装备面板",
            "道具 <道具ID>　道具详情",
            "服务器　服务器列表",
            "职业　职业列表",
            "公告 [板块]　官方公告",
            "排行 [榜单]　排行榜",
            "活动 [明天]　活动时刻表",
            "活动 订阅　订阅开场提醒（裂隙、小游戏）",
            "活动 退订　取消订阅",
            "区域　查询区域说明",
            "帮助　本说明",
            "",
            f"板块：{'、'.join(BOARDS.values())}",
            f"榜单：{'、'.join(RANKING_LABELS.values())}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("区域")
    async def region_info(self, event: AstrMessageEvent):
        """查看当前查询区域与可切换的区域。"""
        current = self.client.region
        lines = [f"当前区域：{current.label}（{current.key}）", "", "可选区域（在插件配置里修改）："]
        for key, region in REGIONS.items():
            if key == GLOSSARY_REGION:
                continue  # 台服只作为译名来源，不用于查询
            mark = " ← 当前" if key == current.key else ""
            lines.append(f"  {key}　{region.label}{mark}")
        lines.append("")
        lines.append("区域只决定查哪个分片：各分片的服务器与角色互相独立，")
        lines.append("同一个名字可能在不同分片各有一个角色。")
        lines.append("上游取数一律用英文（台服为繁中），中文由插件本地映射，与区域无关。")
        yield event.plain_result("\n".join(lines))

    @filter.command("服务器")
    async def servers(self, event: AstrMessageEvent):
        """列出当前区域的所有服务器。"""
        try:
            rows = await self.client.servers()
        except Aion2Error as exc:
            yield event.plain_result(f"查询失败：{exc.hint}")
            return
        if not rows:
            yield event.plain_result("该区域没有返回服务器数据。")
            return
        lines = [f"{self.client.region.label}服务器（{len(rows)} 个）"]
        for race_id, label in ((1, "天族"), (2, "魔族")):
            group = [s for s in rows if s.race_id == race_id]
            if not group:
                continue
            lines.append("")
            lines.append(f"{label}（{len(group)} 个）")
            cells = [f"{server_name(s.name)}({s.server_id})" for s in group]
            lines.extend(_wrap(cells))
        # 欧服比台服多 8 个服，这 8 个没有官方中文源，不能默默显示英文
        missing = [s for s in rows if server_name(s.name) == s.name]
        if missing:
            lines.append("")
            lines.append(
                f"注：其中 {len(missing)} 个服务器官方没有中文名，显示英文原名"
                f"（{'、'.join(s.name for s in missing[:6])}"
                f"{'…' if len(missing) > 6 else ''}）。"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("职业")
    async def classes(self, event: AstrMessageEvent):
        """列出当前区域的职业。"""
        try:
            rows = await self.client.classes()
        except Aion2Error as exc:
            yield event.plain_result(f"查询失败：{exc.hint}")
            return
        lines = [f"{self.client.region.label}职业（{len(rows)} 个）"]
        for c in rows:
            lines.append(f"  {class_name(c.id, localized(c.text))}（{c.name}）ID {c.id}")
        yield event.plain_result("\n".join(lines))

    @filter.command("角色", alias={"查角色"})
    async def character(self, event: AstrMessageEvent, name: str, server_id: int = 0):
        """查询角色档案：等级、职业、战斗力、属性、称号与守护者板。"""
        try:
            summary, page = await self._locate(name, server_id)
            if summary is None:
                yield event.plain_result(self._candidates(event, page, name, "角色"))
                return
            async for result in self._render_character(event, summary):
                yield result
        except Aion2Error as exc:
            yield event.plain_result(f"查询失败：{exc.hint}（{exc.detail}）")

    @filter.command("装备", alias={"查装备"})
    async def equipment(self, event: AstrMessageEvent, name: str, server_id: int = 0):
        """查询角色装备面板：装备、时装、宠物翅膀与技能。"""
        try:
            summary, page = await self._locate(name, server_id)
            if summary is None:
                yield event.plain_result(self._candidates(event, page, name, "装备"))
                return
            async for result in self._render_equipment(event, summary):
                yield result
        except Aion2Error as exc:
            yield event.plain_result(f"查询失败：{exc.hint}（{exc.detail}）")

    @filter.command("道具", alias={"查道具"})
    async def item(self, event: AstrMessageEvent, item_id: int):
        """按道具 ID 查询道具详情。"""
        try:
            detail = await self.client.item(item_id)
            ctx = await render.item_context(self.client, detail, width=self._width())
            yield await self._emit(
                event, "item", ctx, lambda: render.item_text(self.client, detail)
            )
        except Aion2Error as exc:
            yield event.plain_result(f"查询失败：{exc.hint}（{exc.detail}）")

    @filter.command("公告")
    async def notices(self, event: AstrMessageEvent, board: str = "notice"):
        """查看官方公告。板块可选：公告、更新公告、开发日志等，中英文名都可用。"""
        key = _resolve_option(board, BOARDS)
        if not key:
            yield event.plain_result(
                f"未知板块 {board}。可选：{_option_hint(BOARDS)}"
            )
            return
        try:
            rows = await self.client.posts(key, self._limit())
        except Aion2Error as exc:
            yield event.plain_result(f"查询失败：{exc.hint}（{exc.detail}）")
            return
        if not rows:
            yield event.plain_result(
                f"{BOARDS[key]}暂时没有内容。玩家板块在国际服抢先体验期是空的。"
            )
            return
        lines = [f"{BOARDS[key]}（最新 {len(rows)} 条）"]
        for p in rows:
            when = p.posted_at.strftime("%Y-%m-%d") if p.posted_at else ""
            lines.append(f"  [{when}] {localized(p.title)}")
            if p.summary:
                lines.append(f"      {localized(p.summary)[:60]}")
        yield event.plain_result("\n".join(lines))

    @filter.command("排行")
    async def ranking(self, event: AstrMessageEvent, kind: str = "abyss"):
        """查看排行榜。榜单可选：深渊、噩梦、超越等，中英文名都可用。"""
        key = _resolve_option(kind, RANKING_LABELS)
        if not key:
            yield event.plain_result(
                f"未知榜单 {kind}。可选：{_option_hint(RANKING_LABELS)}"
            )
            return
        try:
            rows = await self.client.servers()
        except Aion2Error as exc:
            yield event.plain_result(f"查询失败：{exc.hint}")
            return
        if not rows:
            yield event.plain_result("该区域没有返回服务器数据。")
            return
        try:
            entries = await self.client.ranking(key, rows[0].server_id)
        except NoSeason:
            yield event.plain_result(
                "官方已关闭该排行榜的公开数据，暂时查不到名次。"
            )
            return
        except Aion2Error as exc:
            yield event.plain_result(f"查询失败：{exc.hint}（{exc.detail}）")
            return
        lines = [f"{server_name(rows[0].name)} {RANKING_LABELS[key]}榜"]
        for e in entries[: self._limit()]:
            name = clean_text(str(e.get("characterName", "")))
            lines.append(f"  {e.get('rank')}. {localized(name)}")
        lines.append("")
        lines.append("榜单按服务器统计，这里展示的是该区域的第一个服务器。")
        yield event.plain_result("\n".join(lines))

    # --------------------------------------------------- 活动时刻表与提醒

    def _lead(self) -> int:
        """提前多少分钟推送。"""
        try:
            return max(1, min(int(self.config.get("event_lead", 5)), 60))
        except (TypeError, ValueError):
            return 5

    def _push_kinds(self) -> tuple[str, ...]:
        kinds = []
        if self.config.get("event_remind_rift", True):
            kinds.append(events.KIND_RIFT)
        if self.config.get("event_remind_minigame", True):
            kinds.append(events.KIND_MINIGAME)
        return tuple(kinds)

    def _quiet_range(self) -> tuple[int, int] | None:
        """静默时段的起止（分钟数）。留空或起止相同表示不静默。"""

        def to_minutes(value) -> int | None:
            text = str(value or "").strip()
            if not text:
                return None
            try:
                hour, minute = (int(part) for part in text.split(":")[:2])
            except ValueError:
                return None
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                return None
            return hour * 60 + minute

        start = to_minutes(self.config.get("quiet_start", "00:00"))
        end = to_minutes(self.config.get("quiet_end", "08:00"))
        if start is None or end is None or start == end:
            return None
        return start, end

    def _in_quiet(self, now: datetime) -> bool:
        span = self._quiet_range()
        if span is None:
            return False
        start, end = span
        current = now.hour * 60 + now.minute
        if start < end:
            return start <= current < end
        return current >= start or current < end  # 跨零点

    def subscribers(self) -> list[str]:
        """已订阅活动提醒的会话标识。"""
        return [key[len(SUB_KEY) :] for key in self._subs.keys() if key.startswith(SUB_KEY)]

    def _clock(self, minutes: int) -> str:
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    def _subscribe_note(self) -> str:
        span = self._quiet_range()
        labels = [events.KIND_NAMES[k] for k in self._push_kinds()]
        lines = [
            "已订阅活动提醒",
            f"提前 {self._lead()} 分钟推送：{'、'.join(labels) or '未选择任何事件'}",
            f"静默时段：{self._clock(span[0])}–{self._clock(span[1])}" if span else "静默时段：无",
            "发送「活动 退订」可取消。",
        ]
        return "\n".join(lines)

    @filter.command("活动")
    async def event_schedule(self, event: AstrMessageEvent, option: str = ""):
        """查看今日活动时刻表（时空裂隙、小游戏），或订阅开场提醒。"""
        key = (option or "").strip()
        umo = getattr(event, "unified_msg_origin", "") or ""
        if key in ("订阅", "subscribe"):
            if not umo:
                yield event.plain_result("拿不到当前会话标识，订阅失败。")
                return
            self._subs.set(SUB_KEY + umo, int(time.time()))
            self._subs.save()
            yield event.plain_result(self._subscribe_note())
            return
        if key in ("退订", "unsubscribe"):
            self._subs.remove(SUB_KEY + umo)
            self._subs.save()
            yield event.plain_result("已退订活动提醒。发送「活动 订阅」可重新订阅。")
            return
        if key in ("状态", "status"):
            lines = [
                f"本会话{'已订阅' if self._subs.get(SUB_KEY + umo) else '未订阅'}活动提醒",
                f"当前订阅会话数：{len(self.subscribers())}",
                f"推送开关：{'开' if self.config.get('event_push', True) else '关'}"
                f"｜提前 {self._lead()} 分钟",
            ]
            yield event.plain_result("\n".join(lines))
            return
        if key and key not in ("明天", "tomorrow", "明日"):
            yield event.plain_result(f"未知参数 {key}。可用：明天、订阅、退订、状态")
            return
        tomorrow = key in ("明天", "tomorrow", "明日")
        now = datetime.now()
        ctx = render.events_context(now=now, tomorrow=tomorrow, width=self._width())
        text = render.events_text(now=now, tomorrow=tomorrow)
        yield await self._emit(event, "events", ctx, text)

    async def _event_loop(self):
        """活动提醒的后台循环，由 _stop_event_loop 取消。"""
        while True:
            try:
                await self.push_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单次失败不能拖垮循环
                logger.warning(f"活动提醒推送出错：{exc}")
            await asyncio.sleep(EVENT_TICK)

    async def push_due(self, now: datetime | None = None) -> int:
        """把到点的提醒推给订阅过的会话，返回发出的条数。"""
        moment = now or datetime.now()
        if not self.config.get("event_push", True) or self._in_quiet(moment):
            return 0
        due = events.pending_reminders(moment, lead=self._lead(), kinds=self._push_kinds())
        targets = self.subscribers()
        if not due or not targets:
            return 0
        sent = 0
        for occ in due:
            key = SENT_KEY + occ.key
            if self._sent.get(key) is not None:
                continue
            text = events.reminder_text(moment, occ, lead=self._lead())
            for umo in targets:
                try:
                    await self.context.send_message(umo, MessageChain().message(text))
                    sent += 1
                except Exception as exc:  # noqa: BLE001 - 一个会话失败不影响其他会话
                    logger.warning(f"向 {umo} 推送活动提醒失败：{exc}")
            self._sent.set(key, int(time.time()))
        self._sent.save()
        return sent

    # ------------------------------------------------------- 无斜杠触发

    async def _free_handler(self, name: str, event: AstrMessageEvent, groups):
        """把无斜杠的消息接到对应指令上，缺省参数用指令自身的默认值。"""
        if name in ("角色", "装备"):
            server_id = int(groups[1]) if groups[1] else 0
            handler = self.character if name == "角色" else self.equipment
            async for result in handler(event, groups[0], server_id):
                yield result
        elif name == "道具":
            async for result in self.item(event, int(groups[0])):
                yield result
        elif name == "公告":
            async for result in self.notices(event, groups[0] or "notice"):
                yield result
        elif name == "排行":
            async for result in self.ranking(event, groups[0] or "abyss"):
                yield result
        elif name == "活动":
            async for result in self.event_schedule(event, groups[0] or ""):
                yield result
        else:
            handler = {
                "服务器": self.servers,
                "职业": self.classes,
                "区域": self.region_info,
                "帮助": self.help,
            }[name]
            async for result in handler(event):
                yield result

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_free_text(self, event: AstrMessageEvent):
        """不带斜杠的写法：直接发「角色 mizoo」「服务器」，重名时回序号。

        只认整条消息正好是这套句式，免得群里闲聊提到「角色」二字就被拦下来。
        带斜杠的消息以 / 开头，不会匹配到这里的正则，因此不会重复响应。
        """
        self._ensure_event_loop()
        if not self.config.get("free_trigger", True):
            return
        text = (event.message_str or "").strip()
        if not text or len(text) > 40:
            return
        if text.isdigit():
            # 只有刚列过候选的会话才会认，否则纯数字当没说
            async for result in self._pick(event, text):
                yield result
            return
        for name, pattern in FREE_RULES:
            match = pattern.match(text)
            if not match:
                continue
            try:
                async for result in self._free_handler(name, event, match.groups()):
                    yield result
            except (TypeError, ValueError):
                yield event.plain_result(f"{name}的参数不对，例如：{name} mizoo")
            return

    # -------------------------------------------------------- 大模型工具

    @filter.llm_tool(name="aion2_search_character")
    async def tool_search_character(self, event: AstrMessageEvent, keyword: str):
        """在 AION2 中按名字搜索角色，返回候选列表。

        Args:
            keyword(string): 角色名的一部分
        """
        try:
            page = await self._search(keyword)
        except Aion2Error as exc:
            yield event.plain_result(f"查询失败：{exc.hint}")
            return
        yield event.plain_result(render.search_text(page))

    @filter.llm_tool(name="aion2_character")
    async def tool_character(self, event: AstrMessageEvent, keyword: str, server_id: int = 0):
        """查询 AION2 角色的档案：等级、职业、战斗力、属性、称号与守护者板进度。

        Args:
            keyword(string): 角色名
            server_id(number): 服务器 ID，不确定时留空
        """
        try:
            summary, page = await self._locate(keyword, server_id)
            if summary is None:
                yield event.plain_result(
                    self._candidates(event, page, keyword, "角色", interactive=False)
                )
                return
            async for result in self._render_character(event, summary):
                yield result
        except Aion2Error as exc:
            yield event.plain_result(f"查询失败：{exc.hint}（{exc.detail}）")

    @filter.llm_tool(name="aion2_equipment")
    async def tool_equipment(self, event: AstrMessageEvent, keyword: str, server_id: int = 0):
        """查询 AION2 角色的装备面板：各部位装备、时装、宠物、翅膀与技能。

        Args:
            keyword(string): 角色名
            server_id(number): 服务器 ID，不确定时留空
        """
        try:
            summary, page = await self._locate(keyword, server_id)
            if summary is None:
                yield event.plain_result(
                    self._candidates(event, page, keyword, "装备", interactive=False)
                )
                return
            async for result in self._render_equipment(event, summary):
                yield result
        except Aion2Error as exc:
            yield event.plain_result(f"查询失败：{exc.hint}（{exc.detail}）")

    @filter.llm_tool(name="aion2_item")
    async def tool_item(self, event: AstrMessageEvent, item_id: int = 0):
        """按道具 ID 查询 AION2 道具的属性、品级、可用职业与外观。

        Args:
            item_id(number): 道具 ID
        """
        try:
            detail = await self.client.item(int(item_id))
            ctx = await render.item_context(self.client, detail, width=self._width())
            yield await self._emit(
                event, "item", ctx, lambda: render.item_text(self.client, detail)
            )
        except Aion2Error as exc:
            yield event.plain_result(f"查询失败：{exc.hint}（{exc.detail}）")

    @filter.llm_tool(name="aion2_servers")
    async def tool_servers(self, event: AstrMessageEvent):
        """列出 AION2 当前区域的服务器，以及每个服务器的 ID。"""
        try:
            rows = await self.client.servers()
        except Aion2Error as exc:
            yield event.plain_result(f"查询失败：{exc.hint}")
            return
        text = "、".join(f"{server_name(s.name)}({s.server_id})" for s in rows)
        yield event.plain_result(f"{self.client.region.label}服务器：{text}")

    @filter.llm_tool(name="aion2_classes")
    async def tool_classes(self, event: AstrMessageEvent):
        """列出 AION2 当前区域的职业，以及搜索时用的职业 ID。"""
        try:
            rows = await self.client.classes()
        except Aion2Error as exc:
            yield event.plain_result(f"查询失败：{exc.hint}")
            return
        text = "、".join(f"{class_name(c.id, localized(c.text))}({c.id})" for c in rows)
        yield event.plain_result(f"{self.client.region.label}职业：{text}")

    @filter.llm_tool(name="aion2_notices")
    async def tool_notices(self, event: AstrMessageEvent, board: str = "notice"):
        """查看 AION2 官方公告或更新公告。

        Args:
            board(string): 板块，notice 公告、update 更新公告、cm_story 开发日志
        """
        key = _resolve_option(board or "notice", BOARDS)
        if not key:
            yield event.plain_result(f"未知板块。可选：{_option_hint(BOARDS)}")
            return
        try:
            rows = await self.client.posts(key, self._limit())
        except Aion2Error as exc:
            yield event.plain_result(f"查询失败：{exc.hint}（{exc.detail}）")
            return
        if not rows:
            yield event.plain_result(f"{BOARDS[key]}暂时没有内容。")
            return
        lines = [f"{BOARDS[key]}（最新 {len(rows)} 条）"]
        for p in rows:
            when = p.posted_at.strftime("%Y-%m-%d") if p.posted_at else ""
            lines.append(f"[{when}] {localized(p.title)}")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------ 辅助

    def _capability_note(self) -> str:
        """数据缺口说明。国际服各分片共用同一套社区数据，目前只有公告在更新。"""
        if self.client.region.shard:
            return "国际服仍处于抢先体验期，除官方公告外的板块都还没有内容。"
        return ""
