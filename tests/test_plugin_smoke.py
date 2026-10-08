"""冒烟测试：用替身模块模拟 AstrBot，验证插件能加载、指令能跑通。

替身会把 html_render 收到的 HTML 写到 tests/out/，便于离线检查卡片外观。
"""

import asyncio
import importlib
import inspect
import json
import re
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")


# ------------------------------------------------------------- AstrBot 替身


class FakeLogger:
    def info(self, *_a, **_k):
        pass

    warning = error = debug = info


class FakeConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = None

    def save_config(self, replace_config=None, indent=2):
        if replace_config:
            self.update(replace_config)
        self.saved = dict(self)


class FakeGroup:
    def __init__(self, name):
        self.name = name
        self.subs = {}

    def command(self, name, **_kw):
        def deco(fn):
            self.subs[name] = fn
            return fn

        return deco


class FakeEventMessageType:
    ALL = "all"
    PRIVATE_MESSAGE = "private"
    GROUP_MESSAGE = "group"


class FakeFilter:
    def __init__(self):
        self.commands = {}  # 指令名 → 处理函数
        self.aliases = {}  # 别名 → 处理函数
        self.listeners = []  # 事件监听器

    def command(self, name, alias=None, **_kw):
        def deco(fn):
            fn.__command__ = name
            self.commands[name] = fn
            for one in alias or ():
                self.aliases[one] = fn
            return fn

        return deco

    def command_group(self, name, **_kw):
        def deco(fn):
            group = FakeGroup(name)
            group.fn = fn
            return group

        return deco

    def event_message_type(self, kind):
        def deco(fn):
            fn.__event_message_type__ = kind
            self.listeners.append(fn)
            return fn

        return deco

    def llm_tool(self, name=None, **_kw):
        def deco(fn):
            fn.__tool__ = name or fn.__name__
            return fn

        return deco


FakeFilter.EventMessageType = FakeEventMessageType

# 插件模块导入时会往这个替身上注册指令，测试需要读它
STUB_FILTER = FakeFilter()


class FakeMessageEvent:
    def __init__(self, text="", umo="test:session", sender="tester"):
        self.message_str = text
        self.unified_msg_origin = umo
        self._sender = sender
        self.plain = []
        self.images = []

    def plain_result(self, text):
        self.plain.append(text)
        return ("plain", text)

    def image_result(self, url):
        self.images.append(url)
        return ("image", url)

    def get_sender_id(self):
        return self._sender

    def get_sender_name(self):
        return self._sender


class FakeStar:
    def __init__(self, context):
        self.context = context

    async def html_render(self, tmpl, data, options=None):
        # 替身把卡片写到磁盘，供离线查看
        name = getattr(self, "_card_seq", 0)
        self._card_seq = name + 1
        path = OUT / f"smoke_card_{name}.html"
        path.write_text(
            "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body "
            "style='background:#0b0d13;margin:0;padding:16px'>" + tmpl + "</body></html>",
            encoding="utf-8",
        )
        return f"file://{path}"


class FakeMessageChain:
    def __init__(self):
        self.parts = []

    def message(self, text):
        self.parts.append(text)
        return self


class FakeContext:
    def __init__(self):
        self.tools = []
        self.sent = []
        self.web_apis = []

    def add_llm_tools(self, *tools):
        self.tools.extend(tools)

    def register_web_api(self, route, handler, methods, desc):
        self.web_apis.append(
            {"route": route, "handler": handler, "methods": methods, "desc": desc}
        )

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))


class FakeQuery:
    def __init__(self, data=None):
        self._data = dict(data or {})

    def get(self, key, default=None, type=None):
        value = self._data.get(key, default)
        if type is not None and value is not None:
            try:
                return type(value)
            except (TypeError, ValueError):
                return default
        return value


class FakeRequest:
    """面板请求的替身，测试里直接改 query 与 json_body。"""

    def __init__(self):
        self.query = FakeQuery()
        self.json_body = {}

    async def json(self, default=None):
        if self.json_body:
            return self.json_body
        return {} if default is None else default


STUB_REQUEST = FakeRequest()


def json_response(payload):
    return {"kind": "json", "payload": payload}


def error_response(message, status_code=400):
    return {"kind": "error", "message": message, "status_code": status_code}


def install_stub():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = FakeLogger()
    api.AstrBotConfig = FakeConfig
    api.FunctionTool = object

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.filter = STUB_FILTER
    event_mod.AstrMessageEvent = FakeMessageEvent
    event_mod.MessageChain = FakeMessageChain

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = FakeContext
    star_mod.Star = FakeStar

    web_mod = types.ModuleType("astrbot.api.web")
    web_mod.request = STUB_REQUEST
    web_mod.json_response = json_response
    web_mod.error_response = error_response

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event_mod,
            "astrbot.api.star": star_mod,
            "astrbot.api.web": web_mod,
        }
    )


install_stub()

# 让 astrbot.core.utils.astrbot_path 也能被导入（data 目录用本地 out/）
core_utils = types.ModuleType("astrbot.core.utils.astrbot_path")
core_utils.get_astrbot_data_path = lambda: str(OUT)
core_mod = types.ModuleType("astrbot.core")
utils_mod = types.ModuleType("astrbot.core.utils")
utils_mod.astrbot_path = core_utils
sys.modules.update(
    {
        "astrbot.core": core_mod,
        "astrbot.core.utils": utils_mod,
        "astrbot.core.utils.astrbot_path": core_utils,
    }
)

plugin_pkg = types.ModuleType("aion2_plugin")
plugin_pkg.__path__ = [str(PLUGIN_DIR)]
sys.modules["aion2_plugin"] = plugin_pkg

print("== 插件加载 ==")
main = importlib.import_module("aion2_plugin.main")
check("模块导入成功", hasattr(main, "Aion2Plugin"))

cfg = FakeConfig(json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8")))
cfg.update({k: v.get("default") for k, v in cfg.items() if isinstance(v, dict)})
ctx = FakeContext()
plugin = main.Aion2Plugin(ctx, cfg)
check("插件实例化", plugin is not None)
check("客户端惰性构建", plugin.client.region.key == "asia", plugin.client.region.key)
check("卡片宽度读配置", plugin._width() == 720, str(plugin._width()))
check("输出模式读配置", plugin._image_mode() is True)

print("\n== 指令注册 ==")
registered = STUB_FILTER.commands
expected = {"帮助", "区域", "服务器", "职业", "角色", "装备", "道具", "公告", "排行", "活动", "基纳"}
missing = expected - set(registered)
check("平铺指令齐全", not missing, f"缺少 {missing}" if missing else f"{len(registered)} 个")
check("不再有 /aion 指令组", "aion" not in registered)
check("帮助指令有别名", "help" in STUB_FILTER.aliases and "aion" not in STUB_FILTER.aliases)
check("注册了关键词监听", len(STUB_FILTER.listeners) == 1, str(len(STUB_FILTER.listeners)))

print("\n== 配置面板接口注册 ==")
console_routes = {api["route"]: api for api in ctx.web_apis}
expected_routes = {
    "/astrbot_plugin_aion2/console/overview",
    "/astrbot_plugin_aion2/console/state",
    "/astrbot_plugin_aion2/console/config",
    "/astrbot_plugin_aion2/console/reset",
    "/astrbot_plugin_aion2/console/cache/clear",
    "/astrbot_plugin_aion2/console/schedule",
    "/astrbot_plugin_aion2/console/selftest",
    "/astrbot_plugin_aion2/console/subscriptions/remove",
    "/astrbot_plugin_aion2/console/subscriptions/test",
}
missing_routes = expected_routes - set(console_routes)
check(
    "面板接口齐全",
    not missing_routes,
    f"缺少 {missing_routes}" if missing_routes else f"{len(console_routes)} 条",
)
check(
    "路由都带插件名前缀",
    all(route.startswith("/astrbot_plugin_aion2/console/") for route in console_routes),
)
check(
    "读取用 GET、写入用 POST",
    all(
        console_routes[route]["methods"] == ["GET"]
        for route in (
            "/astrbot_plugin_aion2/console/overview",
            "/astrbot_plugin_aion2/console/state",
            "/astrbot_plugin_aion2/console/schedule",
        )
    )
    and console_routes["/astrbot_plugin_aion2/console/config"]["methods"] == ["POST"],
)
check(
    "每个接口都有说明",
    all(api["desc"] for api in console_routes.values()),
)

# 面板前端把 endpoint 直接拼在 Dashboard 的 extensions/<插件名>/ 之后，
# 所以前端调用的名字必须等于注册路由去掉插件名前缀的部分。签名对不上时
# 真实环境会返回「未找到该路由」，而纯 mock 测试是发现不了的。
app_js = (PLUGIN_DIR / "pages" / "console" / "app.js").read_text(encoding="utf-8")
called = set(re.findall(r'api\(\s*"(?:GET|POST)"\s*,\s*"([^"]+)"', app_js))
called |= set(re.findall(r'apiGet\(API_BASE\s*\+\s*"([^"]+)"', app_js))
unmatched = {
    name for name in called if f"/astrbot_plugin_aion2/console/{name}" not in console_routes
}
check(
    "前端调用的接口都能对上注册路由",
    bool(called) and not unmatched,
    f"对不上 {unmatched}" if unmatched else f"{len(called)} 个",
)
check(
    "前端直调 bridge 时都带 API_BASE 前缀",
    not re.search(r'bridge\.api(?:Get|Post)\(\s*"', app_js),
)
check(
    "面板文件与 i18n 同时兼容 views/pages 两种名字",
    '"views"' in (PLUGIN_DIR / ".astrbot-plugin" / "i18n" / "zh-CN.json").read_text(
        encoding="utf-8"
    )
    and '"pages"' in (PLUGIN_DIR / ".astrbot-plugin" / "i18n" / "zh-CN.json").read_text(
        encoding="utf-8"
    )
    and "AstrBotPluginView" in app_js,
)

print("\n== 板块与榜单选项 ==")
check("板块接受英文键", main._resolve_option("update", main.BOARDS) == "update")
check("板块接受中文名", main._resolve_option("更新公告", main.BOARDS) == "update")
check("板块名忽略大小写", main._resolve_option(" Notice ", main.BOARDS) == "notice")
check("榜单接受中文名", main._resolve_option("深渊", main.RANKING_LABELS) == "abyss")
check("榜单接受英文键", main._resolve_option("abyss", main.RANKING_LABELS) == "abyss")
check("未知选项判空", main._resolve_option("不存在", main.BOARDS) == "")
check("空值判空", main._resolve_option("", main.BOARDS) == "")
check(
    "帮助列的选项与实际一致",
    all(main._resolve_option(v, main.BOARDS) for v in main.BOARDS.values())
    and all(main._resolve_option(v, main.RANKING_LABELS) for v in main.RANKING_LABELS.values()),
)
check(
    "选项提示含中英文",
    "更新公告（update）" in main._option_hint(main.BOARDS),
    main._option_hint(main.BOARDS)[:60],
)
check(
    "长列表按行折开",
    main._wrap(["a", "b", "c", "d", "e"]) == ["  a　b　c　d", "  e"],
    str(main._wrap(list("abcde"))),
)

print("\n== 大模型工具注册 ==")
tools = [
    getattr(plugin, n).__tool__
    for n in dir(plugin)
    if getattr(getattr(plugin, n, None), "__tool__", None)
]
check("工具数量", len(tools) == 7, str(tools))
check(
    "工具命名规范",
    all(t.startswith("aion2_") for t in tools),
    "、".join(sorted(tools)),
)

print("\n== 指令执行（离线数据）==")


async def run_commands():
    ev = FakeMessageEvent()
    async for _ in plugin.help(ev):
        pass
    check("帮助输出", any("AION2 查询" in t for t in ev.plain))
    check(
        "帮助不出现斜杠写法",
        all("/" not in line for t in ev.plain for line in t.splitlines()),
        ev.plain[0].splitlines()[0] if ev.plain else "",
    )
    check(
        "帮助不含自然语言元信息",
        all("自然语言" not in t and "不带斜杠" not in t for t in ev.plain),
    )

    ev = FakeMessageEvent()
    async for _ in plugin.region_info(ev):
        pass
    check("区域输出", any("美东" in t for t in ev.plain), ev.plain[0].splitlines()[0] if ev.plain else "")

    # 用替身客户端替换真实客户端，避免联网
    class StubClient:
        region = importlib.import_module("aion2_plugin.core.routes").region_of("asia")

        def __init__(self):
            self._models = importlib.import_module("aion2_plugin.core.models")

        async def servers(self):
            raw = json.loads(
                (PLUGIN_DIR.parent / "reference" / "samples" / "servers.json").read_text("utf-8")
            )
            rows = [self._models.Server.from_raw(s) for s in raw["serverList"]]
            # 欧服比台服多 8 个服，这批没有官方中文名，用来覆盖「回退英文」分支
            rows.append(
                self._models.Server.from_raw(
                    {"raceId": 1, "serverId": 1319, "serverName": "Ishtar"}
                )
            )
            return rows

        async def classes(self):
            raw = json.loads(
                (PLUGIN_DIR.parent / "reference" / "samples" / "classes.json").read_text("utf-8")
            )
            return [self._models.GameClass.from_raw(c) for c in raw["classList"]]

        _item_zh = {
            110160008: {
                "name": "訓練用巨劍",
                "category": "巨劍",
                "race": "全部",
                "costumes": ["廢棄遺產(巨劍)"],
                "stats": {"WeaponFixingDamage": "攻擊力"},
            }
        }

        async def search_characters(self, keyword, **kw):
            # 关键词决定命中数，覆盖唯一匹配 / 多条候选 / 无结果三种分支
            row = {
                "serverId": 2102,
                "characterId": "x",
                # 上游会把命中的片段包成高亮标签，这里照实模拟
                "name": f"<strong>{keyword}</strong>",
                "level": 45,
                "race": 2,
                "serverName": "Zikel",
                "pcId": 15,  # 魔族弓星
            }
            if keyword == "多个":
                rows = [dict(row, serverId=2101), row]
            elif keyword == "不存在":
                rows = []
            else:
                rows = [row]
            return self._models.Page.from_raw(
                {
                    "pagination": {"page": 1, "size": len(rows), "total": len(rows), "endPage": 1},
                    "list": rows,
                }
            )

        async def character(self, ref):
            raw = json.loads(
                (PLUGIN_DIR.parent / "reference" / "samples" / "character_info.json").read_text(
                    "utf-8"
                )
            )
            return self._models.Character.from_raw(raw, ref)

        async def equipment(self, ref):
            raw = json.loads(
                (PLUGIN_DIR.parent / "reference" / "samples" / "equipment.json").read_text(
                    "utf-8"
                )
            )
            return self._models.Equipment.from_raw(raw)

        async def item_zh(self, item_id):
            return self._item_zh.get(item_id, {})

        async def item_names_zh(self, ids):
            return {i: v["name"] for i, v in self._item_zh.items() if i in ids}

        async def posts(self, board, limit):
            if board != "notice":  # 玩家板块在国际服抢先体验期是空的
                return []
            raw = json.loads(
                (PLUGIN_DIR.parent / "reference" / "samples" / "board_notice.json").read_text("utf-8")
            )
            return [self._models.Post.from_raw(p, board) for p in raw["contentList"][:limit]]

        async def close(self):
            return None

    plugin._client = StubClient()

    ev = FakeMessageEvent()
    async for _ in plugin.servers(ev):
        pass
    check("服务器指令", any("希埃尔" in t for t in ev.plain), ev.plain[0].splitlines()[0] if ev.plain else "")
    check(
        "服务器列表每行不超长",
        max(len(l) for t in ev.plain for l in t.splitlines()) <= 60,
        str(max(len(l) for t in ev.plain for l in t.splitlines())),
    )
    check(
        "无中文名的服务器有说明",
        any("官方没有中文名" in t and "Ishtar" in t for t in ev.plain),
        next((l for t in ev.plain for l in t.splitlines() if "注：" in l), ""),
    )

    ev = FakeMessageEvent()
    async for _ in plugin.classes(ev):
        pass
    check("职业指令", any("剑星" in t for t in ev.plain), ev.plain[0].splitlines()[0] if ev.plain else "")

    ev = FakeMessageEvent()
    async for _ in plugin.notices(ev):
        pass
    check("公告指令", any("Scheduled maintenance" in t for t in ev.plain), ev.plain[0].splitlines()[0] if ev.plain else "")

    ev = FakeMessageEvent()
    async for _ in plugin.notices(ev, "nosuchboard"):
        pass
    check("未知板块有提示", any("未知板块" in t for t in ev.plain), ev.plain[0][:40] if ev.plain else "")

    ev = FakeMessageEvent()
    async for _ in plugin.notices(ev, "free"):
        pass
    check("空板块有提示", any("空" in t or "没有内容" in t for t in ev.plain))

    # 帮助里列出的是中文名，所以参数必须也认中文名
    ev = FakeMessageEvent()
    async for _ in plugin.notices(ev, "更新公告"):
        pass
    check(
        "公告接受中文板块名",
        any("更新公告暂时没有内容" in t for t in ev.plain),
        ev.plain[0][:50] if ev.plain else "",
    )

    ev = FakeMessageEvent()
    async for _ in plugin.notices(ev, "nosuchboard"):
        pass
    check(
        "未知板块提示给出中英文名",
        any("公告（notice）" in t for t in ev.plain),
        ev.plain[0][:70] if ev.plain else "",
    )

    ev = FakeMessageEvent()
    async for _ in plugin.ranking(ev, "nosuch"):
        pass
    check("未知榜单有提示", any("未知榜单" in t for t in ev.plain))

    # ---------------- 活动时刻表与提醒
    from datetime import datetime as dt

    for key in plugin._subs.keys():
        plugin._subs.remove(key)
    for key in plugin._sent.keys():
        plugin._sent.remove(key)

    ev = FakeMessageEvent()
    async for _ in plugin.event_schedule(ev):
        pass
    check("活动指令出图", len(ev.images) == 1, str(ev.images))
    card_path = Path(ev.images[0].replace("file://", ""))
    card_html = card_path.read_text(encoding="utf-8") if card_path.exists() else ""
    check(
        "活动卡片含裂隙与小游戏",
        "时空裂隙" in card_html and "小游戏" in card_html and ":15" in card_html,
        card_path.name,
    )
    check("活动卡片写明本地推算", "本地推算" in card_html)

    ev = FakeMessageEvent()
    async for _ in plugin.event_schedule(ev, "明天"):
        pass
    check("活动可查明天", len(ev.images) == 1, str(ev.images))

    ev = FakeMessageEvent()
    async for _ in plugin.event_schedule(ev, "不存在的参数"):
        pass
    check("活动未知参数有提示", any("未知参数" in t for t in ev.plain), str(ev.plain)[:40])

    ev = FakeMessageEvent(umo="g1")
    async for _ in plugin.event_schedule(ev, "状态"):
        pass
    check("状态显示未订阅", any("未订阅" in t for t in ev.plain), str(ev.plain)[:40])

    ev = FakeMessageEvent(umo="g1")
    async for _ in plugin.event_schedule(ev, "订阅"):
        pass
    check("活动可订阅", any("已订阅" in t for t in ev.plain), str(ev.plain)[:40])
    check("订阅记下会话", plugin.subscribers() == ["g1"], str(plugin.subscribers()))
    check("订阅说明含提前分钟", any("提前 5 分钟" in t for t in ev.plain), str(ev.plain)[:60])
    check("订阅说明含静默时段", any("00:00–08:00" in t for t in ev.plain), str(ev.plain)[:60])

    ev = FakeMessageEvent(umo="g1")
    async for _ in plugin.event_schedule(ev, "状态"):
        pass
    check("状态显示已订阅", any("已订阅" in t for t in ev.plain), str(ev.plain)[:40])

    # 到点推送：16:55 同时命中 17:00 的裂隙与小游戏（整点场）
    ctx.sent.clear()
    sent = await plugin.push_due(dt(2026, 10, 7, 16, 55))
    check("到点推送到订阅会话", sent == 2 and len(ctx.sent) == 2, str(sent))
    check("推给的是会话标识", bool(ctx.sent) and ctx.sent[0][0] == "g1", str(ctx.sent[:1]))
    pushed = "\n".join(chain.parts[0] for _, chain in ctx.sent)
    check(
        "推送含裂隙与小游戏",
        "时空裂隙" in pushed and "小游戏" in pushed,
        pushed.replace("\n", " / "),
    )
    check("推送写明提前分钟", "还有 5 分钟" in pushed, pushed.replace("\n", " / "))

    ctx.sent.clear()
    sent = await plugin.push_due(dt(2026, 10, 7, 16, 56))
    check("同一场不重发", sent == 0 and not ctx.sent, str(sent))

    ctx.sent.clear()
    sent = await plugin.push_due(dt(2026, 10, 7, 15, 55))
    check("整点前 5 分钟命中整点小游戏", sent == 1 and len(ctx.sent) == 1, str(sent))

    # 静默时段
    cfg["quiet_start"] = "16:00"
    cfg["quiet_end"] = "17:00"
    ctx.sent.clear()
    sent = await plugin.push_due(dt(2026, 10, 7, 16, 55))
    check("静默时段不推送", sent == 0 and not ctx.sent, str(sent))
    cfg["quiet_start"] = "00:00"
    cfg["quiet_end"] = "08:00"
    sent = await plugin.push_due(dt(2026, 10, 7, 19, 55))
    check("静默解除后照常推", sent == 2, str(sent))
    cfg["quiet_start"] = ""
    cfg["quiet_end"] = ""
    check("留空表示不静默", plugin._quiet_range() is None)
    cfg["quiet_start"] = "00:00"
    cfg["quiet_end"] = "08:00"
    check("跨零点的静默时段", plugin._in_quiet(dt(2026, 10, 7, 23, 30)) is False)
    cfg["quiet_start"] = "23:00"
    cfg["quiet_end"] = "09:00"
    check("夜间静默覆盖凌晨", plugin._in_quiet(dt(2026, 10, 7, 23, 30)) and plugin._in_quiet(dt(2026, 10, 8, 3, 0)))
    check("夜间静默不影响白天", not plugin._in_quiet(dt(2026, 10, 7, 12, 0)))
    cfg["quiet_start"] = "00:00"
    cfg["quiet_end"] = "08:00"

    # 推送项可收窄
    cfg["event_remind_minigame"] = False
    check("可只推裂隙", plugin._push_kinds() == ("rift",), str(plugin._push_kinds()))
    cfg["event_remind_minigame"] = True
    cfg["event_remind_rift"] = False
    check("可只推小游戏", plugin._push_kinds() == ("minigame",), str(plugin._push_kinds()))
    cfg["event_remind_rift"] = True

    # 退订后不再推送
    ev = FakeMessageEvent(umo="g1")
    async for _ in plugin.event_schedule(ev, "退订"):
        pass
    check("活动可退订", any("已退订" in t for t in ev.plain), str(ev.plain)[:40])
    check("退订后没有订阅者", plugin.subscribers() == [], str(plugin.subscribers()))
    ctx.sent.clear()
    sent = await plugin.push_due(dt(2026, 10, 7, 17, 55))
    check("无订阅者不推送", sent == 0 and not ctx.sent, str(sent))

    # 后台循环的生与死
    cfg["event_push"] = False
    cfg["kinah_enable"] = False
    await plugin.initialize()
    check(
        "总开关关闭不起循环",
        plugin._event_task is None and plugin._kinah_task is None,
    )
    cfg["event_push"] = True
    cfg["kinah_enable"] = True
    await plugin.initialize()
    await plugin.initialize()
    check(
        "开启后起后台循环",
        plugin._event_task is not None and plugin._kinah_task is not None,
    )
    # 再喊一次不该起出第二份
    task_ids = (id(plugin._event_task), id(plugin._kinah_task))
    await plugin.initialize()
    check(
        "重复 initialize 不会重复起循环",
        (id(plugin._event_task), id(plugin._kinah_task)) == task_ids,
    )
    await plugin._stop_event_loop()
    await plugin._stop_kinah_loop()
    check(
        "可停掉后台循环",
        plugin._event_task is None and plugin._kinah_task is None,
    )

    # 没走 initialize 时，收到消息也要能把循环带起来
    plugin._event_task = None
    plugin._kinah_task = None
    ev = FakeMessageEvent("帮助")
    async for _ in plugin.on_free_text(ev):
        pass
    check(
        "收到消息会补起循环",
        plugin._event_task is not None and plugin._kinah_task is not None,
    )
    await plugin._stop_event_loop()
    await plugin._stop_kinah_loop()

    # 角色定位的三种分支
    ev = FakeMessageEvent()
    async for _ in plugin.character(ev, "不存在"):
        pass
    check("角色无结果有提示", any("没有找到" in t for t in ev.plain), ev.plain[0][:40] if ev.plain else "")

    ev = FakeMessageEvent()
    async for _ in plugin.character(ev, "多个"):
        pass
    check(
        "角色多结果列候选",
        any("匹配到 2 个角色" in t and "弓星·魔族" in t for t in ev.plain),
        ev.plain[0].splitlines()[1] if ev.plain and len(ev.plain[0].splitlines()) > 1 else "",
    )
    check(
        "候选列表带序号与时限",
        any("1." in t and "2." in t and "秒内有效" in t for t in ev.plain),
        ev.plain[0].splitlines()[0] if ev.plain else "",
    )
    check("候选列表不带高亮标签", all("<strong>" not in t and "&lt;" not in t for t in ev.plain))
    check("重名时挂起等待", plugin._session_key(ev) in plugin._pending)

    # 大模型工具那条链路不挂序号，只提示补服务器 ID
    plugin._pending.clear()
    ev = FakeMessageEvent()
    async for _ in plugin.tool_character(ev, "多个"):
        pass
    check("工具路径提示补服务器", any("请补充服务器" in t for t in ev.plain))
    check("工具路径不挂起等待", not plugin._pending, str(plugin._pending))

    ev = FakeMessageEvent()
    async for _ in plugin.character(ev, "A"):
        pass
    check("角色唯一匹配出图", len(ev.images) == 1, str(ev.images))

    # 道具指令走完整链路（含卡片渲染）
    from aion2_plugin.core import Item

    item_raw = json.loads(
        (PLUGIN_DIR.parent / "reference" / "samples" / "item.json").read_text("utf-8")
    )

    async def fake_item(_id):
        return Item.from_raw(item_raw)

    plugin._client.item = fake_item
    ev = FakeMessageEvent()
    async for _ in plugin.item(ev, 110160008):
        pass
    check("道具指令出图", len(ev.images) == 1, str(ev.images))

    card_path = Path(ev.images[0].replace("file://", ""))
    card_html = card_path.read_text(encoding="utf-8") if card_path.exists() else ""
    check(
        "道具卡内文字已中文化",
        "训练用巨剑" in card_html and "攻击力" in card_html and "巨剑" in card_html,
        card_path.name,
    )

    # 无斜杠关键词触发
    async def fire(text, umo="test:session", sender="tester"):
        event = FakeMessageEvent(text, umo, sender)
        async for _ in plugin.on_free_text(event):
            pass
        return event

    got = await fire("角色 A")
    check("关键词触发角色", len(got.images) == 1, str(got.images))

    got = await fire("装备 A")
    check("关键词触发装备", len(got.images) == 1, str(got.images))

    got = await fire("道具 110160008")
    check("关键词触发道具", len(got.images) == 1, str(got.images))

    got = await fire("角色 A 2102")
    check("关键词可带服务器", len(got.images) == 1, str(got.images))

    got = await fire("服务器")
    check("关键词触发服务器", any("希埃尔" in t for t in got.plain), str(got.plain)[:40])

    got = await fire("职业")
    check("关键词触发职业", any("剑星" in t for t in got.plain), str(got.plain)[:40])

    got = await fire("区域")
    check("关键词触发区域", any("当前区域" in t for t in got.plain), str(got.plain)[:40])

    got = await fire("公告")
    check("关键词触发公告", any("Scheduled maintenance" in t for t in got.plain), str(got.plain)[:40])

    got = await fire("公告 更新公告")
    check(
        "关键词可带板块",
        any("更新公告暂时没有内容" in t for t in got.plain),
        str(got.plain)[:50],
    )

    got = await fire("排行 不存在")
    check("关键词触发排行", any("未知榜单" in t for t in got.plain), str(got.plain)[:40])

    got = await fire("活动")
    check("关键词触发活动", len(got.images) == 1, str(got.images))

    got = await fire("活动 订阅", umo="g2")
    check("关键词可订阅", any("已订阅" in t for t in got.plain), str(got.plain)[:40])
    got = await fire("活动 退订", umo="g2")
    check("关键词可退订", any("已退订" in t for t in got.plain), str(got.plain)[:40])

    got = await fire("帮助")
    check("关键词触发帮助", any("AION2 查询" in t for t in got.plain), str(got.plain)[:40])

    got = await fire("help")
    check("help 亦可触发", any("AION2 查询" in t for t in got.plain), str(got.plain)[:40])

    # ---------------- 基纳价格
    from aion2_plugin.core import kinah as kinah_mod

    plugin._kinah = kinah_mod.Snapshot(
        fetched_at=1758000000.0,
        rate=6.7119,
        zones=("asia", "eu"),
        quotes=[
            kinah_mod.Quote(kinah_mod.SOURCE_7881, "asia", 6.1, 7.46, 9.9, 60, 378),
            kinah_mod.Quote(kinah_mod.SOURCE_PA, "asia", 9.3, 15.77, 22.4, 60, 67),
        ],
    )
    kinah_card = plugin.kinah_card_path()
    kinah_card.parent.mkdir(parents=True, exist_ok=True)
    kinah_card.write_bytes(b"\x89PNG\r\n\x1a\ncached")

    cfg["output_mode"] = "image"
    ev = FakeMessageEvent()
    async for _ in plugin.kinah_price(ev):
        pass
    check(
        "基纳指令直接发缓存的图",
        len(ev.images) == 1 and ev.images[0] == str(kinah_card),
        str(ev.images),
    )
    check("有缓存图时不回退文字", not ev.plain, str(ev.plain)[:40])

    cfg["output_mode"] = "text"
    ev = FakeMessageEvent()
    async for _ in plugin.kinah_price(ev):
        pass
    check(
        "文本模式给价格表",
        len(ev.images) == 0 and any("基纳价格" in t for t in ev.plain),
        str(ev.plain)[:60],
    )
    check("价格表含日服中位价", any("7.5" in t for t in ev.plain), str(ev.plain)[:120])

    # 漏了图就现抓一次，让第一次使用也能拿到结果
    cfg["output_mode"] = "image"
    kinah_card.unlink()
    refreshed = []

    async def fake_refresh():
        refreshed.append(1)
        kinah_card.write_bytes(b"\x89PNG\r\n\x1a\nfresh")
        return {"ok": True}

    plugin.refresh_kinah = fake_refresh
    ev = FakeMessageEvent()
    async for _ in plugin.kinah_price(ev):
        pass
    check("缺图时现抓一次", refreshed == [1], str(refreshed))
    check(
        "先提示再出图",
        any("正在获取" in t for t in ev.plain) and len(ev.images) == 1,
        str(ev.plain)[:40],
    )

    # 抓完仍无图且无快照时如实报错，不要只说「准备中」
    plugin._kinah = None
    kinah_card.unlink()
    plugin._kinah_error = "未安装 playwright，基纳价格抓取不可用"

    async def give_up():
        return {"ok": False, "detail": plugin._kinah_error}

    plugin.refresh_kinah = give_up
    ev = FakeMessageEvent()
    async for _ in plugin.kinah_price(ev):
        pass
    check(
        "取不到价格时给出原因",
        any("未安装 playwright" in t for t in ev.plain) and not ev.images,
        str(ev.plain)[:60],
    )

    cfg["kinah_enable"] = False
    ev = FakeMessageEvent()
    async for _ in plugin.kinah_price(ev):
        pass
    check(
        "配置里关掉时明说",
        any("关闭" in t for t in ev.plain) and not ev.images,
        str(ev.plain)[:40],
    )
    cfg["kinah_enable"] = True

    # 关键词触发走的是同一条指令
    plugin._kinah = kinah_mod.Snapshot(
        fetched_at=1758000000.0,
        rate=6.7119,
        zones=("asia",),
        quotes=[kinah_mod.Quote(kinah_mod.SOURCE_7881, "asia", 6.1, 7.46, 9.9, 60, 378)],
    )
    kinah_card.write_bytes(b"\x89PNG\r\n\x1a\nback")
    got = await fire("基纳")
    check("关键词触发基纳", len(got.images) == 1, str(got.images))

    # 区域与间隔的容错
    cfg["kinah_zones"] = ""
    check(
        "留空表示五大区",
        plugin._kinah_zones() == kinah_mod.DEFAULT_ZONES,
        str(plugin._kinah_zones()),
    )
    cfg["kinah_zones"] = "asia, EU　kr"
    check(
        "区域写法宽松且忽略大小写",
        plugin._kinah_zones() == ("asia", "eu", "kr"),
        str(plugin._kinah_zones()),
    )
    cfg["kinah_zones"] = "台服、乱写"
    check(
        "只认区域键，全无效时回退默认",
        plugin._kinah_zones() == kinah_mod.DEFAULT_ZONES,
        str(plugin._kinah_zones()),
    )
    cfg["kinah_zones"] = ""

    cfg["kinah_interval"] = 10
    check("刷新间隔有下限", plugin._kinah_interval() == 600, str(plugin._kinah_interval()))
    cfg["kinah_interval"] = 999999
    check("刷新间隔有上限", plugin._kinah_interval() == 86400, str(plugin._kinah_interval()))
    cfg["kinah_interval"] = "abc"
    check("坏值回退默认一小时", plugin._kinah_interval() == 3600, str(plugin._kinah_interval()))
    cfg["kinah_interval"] = 3600

    for text, label in (
        ("/角色 A", "带斜杠的消息不重复响应"),
        ("角色", "缺参数不触发"),
        ("今天角色很棒", "闲聊不误触发"),
        ("今天的服务器很卡", "单字指令不做部分匹配"),
        ("活动很卡", "活动后面没空格不触发"),
        ("角色 " + "x" * 40, "超长消息不触发"),
    ):
        got = await fire(text)
        check(label, not got.images and not got.plain, text[:20])

    # 重名后回序号选中
    plugin._pending.clear()
    ev = FakeMessageEvent()
    async for _ in plugin.character(ev, "多个"):
        pass
    got = await fire("1")
    check("回序号出图", len(got.images) == 1, str(got.images))
    check("选中后清掉上下文", not plugin._pending)

    # 越界序号给提示，且不消耗上下文，用户可以再回一次
    ev = FakeMessageEvent()
    async for _ in plugin.character(ev, "多个"):
        pass
    got = await fire("9")
    check("序号越界有提示", any("超出范围" in t for t in got.plain), str(got.plain)[:30])
    check("越界不消耗上下文", plugin._session_key(ev) in plugin._pending)
    got = await fire("2")
    check("越界后仍可重选", len(got.images) == 1, str(got.images))

    # 装备指令重名时选出来的也是装备面板
    ev = FakeMessageEvent()
    async for _ in plugin.equipment(ev, "多个"):
        pass
    got = await fire("1")
    check("装备候选回序号出图", len(got.images) == 1, str(got.images))

    # 超时（把到期时间推到过去）后序号不再生效
    ev = FakeMessageEvent()
    async for _ in plugin.character(ev, "多个"):
        pass
    key = plugin._session_key(ev)
    plugin._pending[key].expires_at = 0
    got = await fire("1")
    check("超时后序号无效", not got.images and not got.plain)
    check("超时后清掉上下文", key not in plugin._pending)

    # 没有候选上下文时，纯数字不响应
    plugin._pending.clear()
    got = await fire("1")
    check("无候选时纯数字不响应", not got.images and not got.plain)

    # 候选按会话 + 发送者隔离，别人替你回序号不算
    ev = FakeMessageEvent(umo="g9", sender="alice")
    async for _ in plugin.character(ev, "多个"):
        pass
    got = await fire("1", umo="g9", sender="bob")
    check("他人不能代选", not got.images and not got.plain)
    got = await fire("1", umo="g9", sender="alice")
    check("本人可继续选", len(got.images) == 1, str(got.images))
    check("选完清掉上下文", not plugin._pending)

    cfg["free_trigger"] = False
    got = await fire("角色 A")
    check("关键词开关可关闭", not got.images and not got.plain)
    cfg["free_trigger"] = True

    # 图片模式下必须真的出图，而不是悄悄回退成文字
    cfg["output_mode"] = "image"
    ev = FakeMessageEvent()
    async for _ in plugin.item(ev, 110160008):
        pass
    check(
        "图片模式出图",
        len(ev.images) == 1 and not ev.plain,
        f"images={ev.images} plain={len(ev.plain)}",
    )
    check("出图后不残留渲染错误", plugin._last_render_error == "", plugin._last_render_error)
    check(
        "渲染参数不带 timeout（Playwright 按毫秒解释，会直接超时）",
        "timeout" not in main.IMAGE_OPTIONS,
        str(main.IMAGE_OPTIONS),
    )
    # 替身比真的宽松过一次：给 data 加了默认值，导致漏传 data 的调用被放过，
    # 真机上却每次都 TypeError 回退文字。这里盯住签名，别再松回去。
    check(
        "渲染替身的 data 与真实签名一样是必填",
        inspect.signature(FakeStar.html_render).parameters["data"].default
        is inspect.Parameter.empty,
    )

    # 关掉图片模式应回退纯文本
    cfg["output_mode"] = "text"
    ev = FakeMessageEvent()
    async for _ in plugin.item(ev, 110160008):
        pass
    check("文本模式回退", len(ev.images) == 0 and len(ev.plain) == 1, ev.plain[0][:50] if ev.plain else "")
    check("文本模式属性中文化", bool(ev.plain) and "攻击力" in ev.plain[0], ev.plain[0][:80] if ev.plain else "")

    # 渲染抛错时应回退而不是崩
    cfg["output_mode"] = "image"

    async def boom(*_a, **_k):
        raise RuntimeError("playwright not installed")

    plugin.html_render = boom
    ev = FakeMessageEvent()
    async for _ in plugin.item(ev, 110160008):
        pass
    check("渲染失败回退文本", len(ev.plain) == 1 and "训练用巨剑" in ev.plain[0], ev.plain[0][:40] if ev.plain else "")
    check(
        "渲染失败会记下原因供面板展示",
        "playwright not installed" in plugin._last_render_error,
        plugin._last_render_error,
    )

    await plugin.terminate()
    check("terminate 正常", plugin._client is None)

    # 繁转简开关：关掉后保留繁体
    i18n = importlib.import_module("aion2_plugin.i18n")
    i18n.set_simplify(False)
    check("关闭繁转简", i18n.localized("訓練用巨劍") == "訓練用巨劍", i18n.localized("訓練用巨劍"))
    i18n.set_simplify(True)
    check("开启繁转简", i18n.localized("訓練用巨劍") == "训练用巨剑", i18n.localized("訓練用巨劍"))


asyncio.run(run_commands())


async def run_console():
    """配置面板的读取、校验、保存、热生效与订阅管理。"""
    from aion2_plugin.core import settings as core_settings

    print("\n== 配置面板：读取 ==")
    panel_cfg = FakeConfig(json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8")))
    panel_cfg.update({k: v.get("default") for k, v in panel_cfg.items() if isinstance(v, dict)})
    panel_ctx = FakeContext()
    panel_plugin = main.Aion2Plugin(panel_ctx, panel_cfg)

    async def no_push(*_a, **_k):
        return 0

    # 提醒循环会在后台按真实时钟跑，测试里把它换成空实现
    panel_plugin.push_due = no_push
    console = panel_plugin._console
    check("面板读到了 schema", len(console.schema) >= 15, str(len(console.schema)))

    STUB_REQUEST.query = FakeQuery()
    STUB_REQUEST.json_body = {}
    res = await console.overview()
    check("overview 返回 JSON", res["kind"] == "json", str(res)[:60])
    data = res["payload"]
    check("overview 带分组", len(data["groups"]) >= 5, str(len(data["groups"])))
    shown = {field["key"] for group in data["groups"] for field in group["fields"]}
    check(
        "面板覆盖全部可配置项",
        shown == set(core_settings.editable_keys(console.schema)),
        f"差 {shown ^ set(core_settings.editable_keys(console.schema))}",
    )
    check(
        "区域选项显示中文名",
        any(
            option["label"] == "日服"
            for group in data["groups"]
            for field in group["fields"]
            if field["key"] == "region"
            for option in field["options"]
        ),
    )
    check("overview 带时刻表", len(data["schedule"]["timeline"]) == 24, str(len(data["schedule"]["timeline"])))
    check("overview 带运行状态", bool(data["state"]["next"]) and "cache" in data["state"])
    check("overview 带订阅列表", isinstance(data["subscriptions"], list))

    print("\n== 配置面板：校验与保存 ==")
    STUB_REQUEST.json_body = {"values": {"event_lead": 15, "t2s": False}}
    res = await console.save_config()
    check("合法改动被接受", res["kind"] == "json", str(res)[:80])
    check("配置写回内存", panel_cfg["event_lead"] == 15 and panel_cfg["t2s"] is False)
    check("配置落盘", bool(panel_cfg.saved) and panel_cfg.saved.get("event_lead") == 15)
    check(
        "只把变化的项列进 changed",
        sorted(res["payload"]["changed"]) == ["event_lead", "t2s"],
        str(res["payload"]["changed"]),
    )
    check(
        "返回刷新后的分组",
        any(
            field["value"] == 15
            for group in res["payload"]["groups"]
            for field in group["fields"]
            if field["key"] == "event_lead"
        ),
    )

    for values, label in (
        ({"event_lead": 0}, "低于下限被拒"),
        ({"event_lead": 999}, "高于上限被拒"),
        ({"event_lead": "abc"}, "非数值被拒"),
        ({"image_width": 19.5}, "小数写整数项被拒"),
        ({"region": "xx"}, "非法选项被拒"),
        ({"quiet_start": "25:00"}, "非法时刻被拒"),
        ({"t2s": "yes"}, "非布尔被拒"),
        ({"nope": 1}, "未登记的配置项被拒"),
        ({"output_mode": 1}, "类型不符被拒"),
    ):
        STUB_REQUEST.json_body = {"values": values}
        res = await console.save_config()
        check(label, res["kind"] == "error", str(res.get("message"))[:50])
    check("拒绝后配置未被改动", panel_cfg["event_lead"] == 15)

    STUB_REQUEST.json_body = {"values": "not-a-dict"}
    res = await console.save_config()
    check("请求体形状不对被拒", res["kind"] == "error")

    STUB_REQUEST.json_body = {"values": {"quiet_start": ""}}
    res = await console.save_config()
    check("留空时刻表示不静默", res["kind"] == "json" and panel_cfg["quiet_start"] == "")

    STUB_REQUEST.json_body = {"keys": ["event_lead"]}
    res = await console.reset_config()
    check("按项恢复默认", res["kind"] == "json" and panel_cfg["event_lead"] == 5)
    STUB_REQUEST.json_body = {"keys": ["不存在"]}
    res = await console.reset_config()
    check("恢复未知项被拒", res["kind"] == "error")
    STUB_REQUEST.json_body = {}
    res = await console.reset_config()
    check("全部恢复默认", res["kind"] == "json" and panel_cfg["t2s"] is True)

    print("\n== 配置面板：热生效 ==")
    check("先建出客户端", panel_plugin.client.region.key == "asia")
    STUB_REQUEST.json_body = {"values": {"region": "eu"}}
    res = await console.save_config()
    check("换区域后客户端被丢掉", res["kind"] == "json" and panel_plugin.peek_client() is None)
    check("新配置立即生效", panel_plugin.client.region.key == "eu", panel_plugin.client.region.key)
    check("运行状态跟着变", panel_plugin.status()["region"]["label"] == "欧服")

    STUB_REQUEST.json_body = {"values": {"t2s": False}}
    res = await console.save_config()
    i18n = importlib.import_module("aion2_plugin.i18n")
    check(
        "繁转简开关热生效",
        res["kind"] == "json" and i18n.localized("訓練用巨劍") == "訓練用巨劍",
    )
    STUB_REQUEST.json_body = {"values": {"t2s": True}}
    await console.save_config()

    STUB_REQUEST.json_body = {"values": {"glossary_ttl": 7200}}
    await console.save_config()
    check("译名缓存时长跟着改", panel_plugin._glossary.ttl == 7200, str(panel_plugin._glossary.ttl))

    print("\n== 配置面板：自定义时刻表 ==")
    check(
        "默认小游戏每小时整点一场",
        panel_plugin._schedule().minigame_minutes == (0,),
        str(panel_plugin._schedule().minigame_minutes),
    )
    STUB_REQUEST.json_body = {"values": {"event_minigame_minutes": "15,45"}}
    res = await console.save_config()
    check("面板可改小游戏分钟", res["kind"] == "json", str(res.get("payload")))
    check(
        "改完立即生效",
        panel_plugin._schedule().minigame_minutes == (15, 45),
        str(panel_plugin._schedule().minigame_minutes),
    )
    STUB_REQUEST.json_body = {"values": {"event_rift_hours": "1,13"}}
    await console.save_config()
    check(
        "面板可改裂隙小时",
        panel_plugin._schedule().rift_hours == (1, 13),
        str(panel_plugin._schedule().rift_hours),
    )
    STUB_REQUEST.json_body = {"values": {"event_minigame_minutes": "99"}}
    res = await console.save_config()
    check("越界分钟被拒", res["kind"] == "error", str(res.get("payload")))
    STUB_REQUEST.json_body = {"values": {"event_minigame_minutes": "0", "event_rift_hours": "2,5,8,11,14,17,20,23"}}
    await console.save_config()
    check(
        "改回默认",
        panel_plugin._schedule().minigame_minutes == (0,)
        and panel_plugin._schedule().rift_hours[0] == 2,
    )

    print("\n== 配置面板：订阅与缓存 ==")
    panel_plugin._subs.set(main.SUB_KEY + "aiocqhttp:GroupMessage:111", 1)
    panel_plugin._subs.set(main.SUB_KEY + "aiocqhttp:FriendMessage:222", 2)
    STUB_REQUEST.json_body = {}
    res = await console.overview()
    check(
        "订阅列表带可读名",
        {row["label"] for row in res["payload"]["subscriptions"]} == {"群 111", "私聊 222"},
        str(res["payload"]["subscriptions"]),
    )

    STUB_REQUEST.json_body = {"target": "aiocqhttp:GroupMessage:111"}
    res = await console.remove_subscription()
    check(
        "移除订阅",
        res["kind"] == "json" and "aiocqhttp:GroupMessage:111" not in panel_plugin.subscribers(),
    )
    STUB_REQUEST.json_body = {"target": "aiocqhttp:GroupMessage:999"}
    res = await console.remove_subscription()
    check("移除未订阅的会话被拒", res["kind"] == "error")
    STUB_REQUEST.json_body = {"target": ""}
    res = await console.remove_subscription()
    check("缺 target 被拒", res["kind"] == "error")

    STUB_REQUEST.json_body = {"target": "aiocqhttp:FriendMessage:222"}
    res = await console.test_subscription()
    check(
        "测试推送发出消息",
        res["kind"] == "json" and len(panel_ctx.sent) == 1,
        str(panel_ctx.sent)[:60],
    )
    STUB_REQUEST.json_body = {"target": "aiocqhttp:GroupMessage:111"}
    res = await console.test_subscription()
    check("向未订阅会话测试被拒", res["kind"] == "error")

    panel_plugin._glossary.set("item:1", {"name": "x"})
    STUB_REQUEST.json_body = {"scope": "glossary"}
    res = await console.clear_cache()
    check("清空译名缓存", res["kind"] == "json" and len(panel_plugin._glossary) == 0)
    STUB_REQUEST.json_body = {"scope": "bad"}
    res = await console.clear_cache()
    check("非法 scope 被拒", res["kind"] == "error")
    STUB_REQUEST.json_body = {"scope": "result"}
    res = await console.clear_cache()
    check("清空结果缓存", res["kind"] == "json" and "state" in res["payload"])

    print("\n== 配置面板：自检与时刻表 ==")

    async def fake_selftest():
        return {"ok": True, "region": "欧服", "servers": 44, "ms": 12}

    panel_plugin.selftest = fake_selftest
    STUB_REQUEST.json_body = {}
    res = await console.selftest()
    check(
        "自检结果透传并带状态",
        res["kind"] == "json" and res["payload"]["servers"] == 44 and "state" in res["payload"],
    )

    STUB_REQUEST.query = FakeQuery({"day": "tomorrow"})
    res = await console.schedule()
    check("明日时刻表", res["kind"] == "json" and res["payload"]["label"] == "明日")
    check(
        "明日不标已过",
        all(row["state"] == "" for row in res["payload"]["rifts"]),
    )
    STUB_REQUEST.query = FakeQuery({"day": "later"})
    res = await console.schedule()
    check("非法 day 被拒", res["kind"] == "error")
    STUB_REQUEST.query = FakeQuery()

    state = panel_plugin.status()
    check("状态含下一次活动", len(state["next"]) == 2, str(state["next"]))
    check("状态含提醒配置", state["push"]["lead"] == 5 and state["push"]["enabled"] is True)
    check("状态含缓存条数", "glossary" in state["cache"] and "entries" in state["cache"])

    # 基纳那块状态，面板直接照着渲染
    check(
        "状态含基纳字段",
        {"enabled", "running", "interval", "zones", "card", "sources", "rate"} <= set(state["kinah"]),
        str(sorted(state["kinah"])),
    )
    check("基纳默认每小时一次", state["kinah"]["interval"] == 3600, str(state["kinah"]["interval"]))
    check(
        "基纳列出两个数据源",
        state["kinah"]["sources"] == ["7881", "PlayerAuctions"],
        str(state["kinah"]["sources"]),
    )
    check("没有快照时不给区数", state["kinah"]["zones"] == 0, str(state["kinah"]["zones"]))

    STUB_REQUEST.json_body = {"values": {"kinah_interval": 7200, "kinah_zones": "asia, eu"}}
    res = await console.save_config()
    check(
        "基纳配置可写",
        res["kind"] == "json" and panel_cfg["kinah_interval"] == 7200 and panel_cfg["kinah_zones"] == "asia, eu",
        str(res)[:60],
    )
    check("面板跟着显示新间隔", panel_plugin.status()["kinah"]["interval"] == 7200)
    STUB_REQUEST.json_body = {"values": {"kinah_interval": 10}}
    res = await console.save_config()
    check("基纳间隔低于下限被拒", res["kind"] == "error", str(res.get("message"))[:40])
    STUB_REQUEST.json_body = {"values": {"kinah_source_pa": False}}
    res = await console.save_config()
    check(
        "可只留一个数据源",
        res["kind"] == "json" and panel_plugin.status()["kinah"]["sources"] == ["7881"],
        str(panel_plugin.status()["kinah"]["sources"]),
    )
    STUB_REQUEST.json_body = {"values": {"kinah_source_pa": True, "kinah_interval": 3600, "kinah_zones": ""}}
    res = await console.save_config()
    check("基纳配置可复位", res["kind"] == "json", str(res)[:60])

    # 面板标的「今日已推」要按当天统计，历史记录不能算进来
    from datetime import datetime as dt2

    for key in list(panel_plugin._sent.keys()):
        panel_plugin._sent.remove(key)
    panel_plugin._sent.set("sent:rift@2020-01-01T00:00", 1)
    check(
        "历史已推不计入今日",
        panel_plugin.status()["push"]["sent"] == 0,
        str(panel_plugin.status()["push"]["sent"]),
    )
    panel_plugin._sent.set(f"sent:rift@{dt2.now():%Y-%m-%d}T00:00", 1)
    check(
        "今日已推按当天统计",
        panel_plugin.status()["push"]["sent"] == 1,
        str(panel_plugin.status()["push"]["sent"]),
    )

    print("\n== 配置面板：旧版本降级 ==")
    webapi_module = importlib.import_module("aion2_plugin.webapi")
    saved_json = webapi_module.json_response
    webapi_module.json_response = None
    try:
        console.register("astrbot_plugin_aion2")
        degraded = True
    except Exception as exc:  # noqa: BLE001 - 这里就是要确认不抛
        degraded = False
        print(f"      {exc}")
    finally:
        webapi_module.json_response = saved_json
    check("没有页面接口时不注册也不报错", degraded)

    class BareContext:
        pass

    bare = main.Aion2Plugin.__new__(main.Aion2Plugin)
    bare.context = BareContext()
    bare.config = FakeConfig()
    bare.plugin_dir = PLUGIN_DIR
    try:
        webapi_module.ConsoleAPI(bare).register("astrbot_plugin_aion2")
        degraded = True
    except Exception as exc:  # noqa: BLE001 - 同上
        degraded = False
        print(f"      {exc}")
    check("宿主没有注册方法时也不报错", degraded)

    await panel_plugin.terminate()
    check("面板用过的插件能正常停用", panel_plugin.peek_client() is None)


asyncio.run(run_console())


print(f"\n结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
for name in FAIL:
    print(f"  - {name}")
sys.exit(1 if FAIL else 0)
