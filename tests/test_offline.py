"""离线测试：路由规则、中文化、数据模型解析、卡片上下文。

样本取自 reference/samples/，不需要联网。
"""

import importlib
import json
import sys
import types
from datetime import date, datetime
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
SAMPLES = PLUGIN_DIR.parent / "reference" / "samples"

# 插件以包的形式被 AstrBot 加载，这里构造同样的包结构以便用相对导入
pkg = types.ModuleType("aion2_plugin")
pkg.__path__ = [str(PLUGIN_DIR)]
sys.modules["aion2_plugin"] = pkg

core = importlib.import_module("aion2_plugin.core")
i18n = importlib.import_module("aion2_plugin.i18n")
models = importlib.import_module("aion2_plugin.core.models")
routes = importlib.import_module("aion2_plugin.core.routes")
render = importlib.import_module("aion2_plugin.render")

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail and not ok else ''}")


def load(name: str):
    return json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8"))


print("== 路由规则 ==")
r = routes.Routes(routes.region_of("nae"))
check(
    "地区名用口语叫法",
    [routes.region_of(k).label for k in ("nae", "naw", "eu", "sa", "asia")]
    == ["美东", "美西", "欧服", "南美服", "日服"],
    "、".join(routes.region_of(k).label for k in ("nae", "naw", "eu", "sa", "asia")),
)
check(
    "asa 分片即日服",
    routes.region_of("asia").shard == "as" and routes.region_of("asia").label == "日服",
    routes.region_of("asia").shard,
)
check(
    "gameinfo 带语言前缀",
    r.game(routes.P_SERVERS) == "https://aion2.plaync.com/en-us/api/gameinfo/servers",
    r.game(routes.P_SERVERS),
)
check(
    "角色接口不带语言前缀",
    r.api(routes.P_CHARACTER) == "https://aion2.plaync.com/api/character/info",
    r.api(routes.P_CHARACTER),
)
check(
    "道具接口刻意不带分片",
    routes.Routes(routes.region_of("asia")).item_params(110160008)
    == {"lang": "en-US", "id": "110160008", "enchantLevel": "0"},
    str(routes.Routes(routes.region_of("asia")).item_params(110160008)),
)
check(
    "角色接口仍带分片",
    routes.Routes(routes.region_of("asia")).site_params()
    == {"lang": "en-US", "region": "as"},
    str(routes.Routes(routes.region_of("asia")).site_params()),
)
check(
    "装备接口不带语言前缀",
    r.api(routes.P_EQUIPMENT) == "https://aion2.plaync.com/api/character/equipment",
    r.api(routes.P_EQUIPMENT),
)
check(
    "站点参数含分片",
    r.site_params() == {"lang": "en-US", "region": "nae"},
    str(r.site_params()),
)
check(
    "板块别名带语言后缀",
    r.board("notice") == "https://api-global-community.plaync.com/aion2_global/board/notice_en",
    r.board("notice"),
)

tw = routes.Routes(routes.region_of("tw"))
check("台服不带语言路径", tw.game(routes.P_SERVERS) == "https://tw.ncsoft.com/aion2/api/gameinfo/servers", tw.game(routes.P_SERVERS))
check("台服道具接口", tw.game(routes.P_ITEM) == "https://tw.ncsoft.com/aion2/api/gameconst/item", tw.game(routes.P_ITEM))
check("台服无分片参数", tw.site_params() == {"lang": "zh-TW"}, str(tw.site_params()))
check("台服板块后缀", tw.board("notice").endswith("/board/notice_zh"), tw.board("notice"))

print("\n== 中文化 ==")
check("职业按 id 查", i18n.class_name(2) == "剑星", i18n.class_name(2))
check("职业按名查", i18n.class_name(0, "Templar") == "守护星", i18n.class_name(0, "Templar"))
check("职业按 pcId 查", i18n.pc_class_name(14) == "弓星", i18n.pc_class_name(14))
check(
    "pcId 每四格换一个职业",
    [i18n.pc_class_name(x) for x in (5, 9, 13, 17, 21, 25, 29, 33, 45)]
    == ["剑星", "守护星", "弓星", "杀星", "精灵星", "魔道星", "治愈星", "护法星", "拳星"],
    "、".join(i18n.pc_class_name(x) for x in (5, 9, 13, 17, 21, 25, 29, 33, 45)),
)
check(
    "pcId 不会被当成职业表 id",
    i18n.pc_class_name(8) == "剑星" and i18n.class_name(8) == "治愈星",
    f"pcId 8→{i18n.pc_class_name(8)}，职业 id 8→{i18n.class_name(8)}",
)
check("pcId 缺名时回退职业名", i18n.pc_class_name(0, "Ranger") == "弓星")
check("pcId 与职业名都未知时回退原文", i18n.pc_class_name(999, "???") == "???")
check("属性查表", i18n.stat_name("STR") == "威力", i18n.stat_name("STR"))
check("部位查表", i18n.slot_name("MainHand") == "主手武器", i18n.slot_name("MainHand"))
check("品级查表", i18n.grade_name("Epic") == "英雄", i18n.grade_name("Epic"))
check("品级优先用上游中文", i18n.grade_name("Epic", "英雄") == "英雄")
check("服务器名对照", i18n.server_name("Siel") == "希埃尔", i18n.server_name("Siel"))
check(
    "服务器词表覆盖两族各 18 个",
    len(i18n.TERMS["servers"]) == 36,
    str(len(i18n.TERMS["servers"])),
)
# 日服有 36 个服，只覆盖前 16 个的话后 20 个会漏成英文
check(
    "后段服务器也有中文名",
    [i18n.server_name(x) for x in ("Hithanya", "Bakarma", "Tsenka", "Kochi", "Zemurru", "Baba")]
    == ["希塔尼耶", "巴卡尔摩", "天加隆", "科奇隆", "简卡卡", "巴巴隆"],
    "、".join(i18n.server_name(x) for x in ("Hithanya", "Tsenka", "Kochi")),
)
check("守护者板名对照", i18n.board_name("Nezekan") == "奈萨肯", i18n.board_name("Nezekan"))
check("地区词表", i18n.region_name("asia") == "日服", i18n.region_name("asia"))
check("繁转简", i18n.to_simplified("應龍王巨劍") == "应龙王巨剑", i18n.to_simplified("應龍王巨劍"))
check("繁转简保留英文", i18n.to_simplified("Training Greatsword") == "Training Greatsword")
check("未收录词条回退原文", i18n.class_name(999, "Unknown") == "Unknown", i18n.class_name(999, "Unknown"))

print("\n== 数据模型 ==")
servers = [models.Server.from_raw(s) for s in load("servers")["serverList"]]
check("服务器数量", len(servers) == 16, str(len(servers)))
check("服务器字段", servers[0].name == "Siel" and servers[0].server_id == 1101)

classes = [models.GameClass.from_raw(c) for c in load("classes")["classList"]]
check("职业数量", len(classes) == 8, str(len(classes)))

page = models.Page.from_raw(load("search"))
check("搜索分页", page.total == 10000 and page.last_page == 667, f"{page.total}/{page.last_page}")
check("搜索结果字段", page.items[0].server_id == 1105 and page.items[0].ref.character_id != "")
check("搜索结果的职业编码", page.items[0].pc_id == 5, str(page.items[0].pc_id))
# 上游把命中的片段包在 <strong> 里，直接输出会把标签发给用户
check("搜索结果已去高亮标签", page.items[0].name == "A", repr(page.items[0].name))
check("去标签不带多余改写", models.clean_text("<strong>mizoo</strong>") == "mizoo")
check("去标签保留普通文本", models.clean_text("Siel") == "Siel" and models.clean_text("") == "")
check("HTML 实体还原", models.clean_text("A &amp; B") == "A & B", models.clean_text("A &amp; B"))
check("命中的偏名不会被误删", models.clean_text("A<strong>mizoo</strong>") == "Amizoo")

ch = models.Character.from_raw(load("character_info"))
check("角色档案", ch.profile.name == "A" and ch.profile.level == 45, f"{ch.profile.name}/{ch.profile.level}")
check("档案的职业编码", ch.profile.pc_id == 14, str(ch.profile.pc_id))
check(
    "职业字段已正名",
    not hasattr(ch.profile, "class_id") and not hasattr(page.items[0], "class_id"),
    "pcId 与职业表 id 是两套编号，不能再混用同一个字段名",
)
check("战斗力", ch.profile.combat_power == 96036, str(ch.profile.combat_power))
check("属性条数", len(ch.stats) == 17, str(len(ch.stats)))
check("称号汇总", ch.titles.total == 297 and ch.titles.owned == 195)
check("守护者板", len(ch.daevanion) == 5 and ch.daevanion[0].name == "Nezekan")

eq = models.Equipment.from_raw(load("equipment"))
check("装备槽位", len(eq.slots) == 25, str(len(eq.slots)))
check("时装", len(eq.skins) == 8, str(len(eq.skins)))
check("宠物翅膀", eq.pet is not None and eq.wing is not None)
check("技能", len(eq.skills) == 35, str(len(eq.skills)))
check("装备槽位名", eq.slots[0].slot_pos_name == "MainHand", eq.slots[0].slot_pos_name)

empty = models.Equipment.from_raw(
    {"petwing": {"pet": {"id": None, "name": None}, "wing": None, "wingSkin": {}}}
)
check(
    "空槽位不造对象",
    empty.pet is None and empty.wing is None and empty.wing_skin is None,
    f"pet={empty.pet} wing={empty.wing} wingSkin={empty.wing_skin}",
)

item = models.Item.from_raw(load("item"))
check("道具解析", item.id == 110160008 and item.name == "Training Greatsword")
check("道具主属性", len(item.main_stats) == 4, str(len(item.main_stats)))
check("道具职业", item.class_names == ["Gladiator"], str(item.class_names))

posts = [models.Post.from_raw(p, "notice") for p in load("board_notice")["contentList"]]
check("公告条数", len(posts) == 20, str(len(posts)))
check("公告标题", posts[0].title.startswith("Scheduled maintenance"), posts[0].title)
check("公告时间", posts[0].posted_at is not None)

body = models.PostBody.from_raw(load("post_detail"))
check("帖子正文", len(body.html) > 1000, str(len(body.html)))

print("\n== 卡片上下文 ==")


class StubClient:
    """替身客户端，只提供台服中文信息，避免卡片测试联网。"""

    region = routes.region_of("nae")
    _zh = {
        110160008: {
            "name": "訓練用巨劍",
            "category": "巨劍",
            "race": "全部",
            "costumes": ["廢棄遺產(巨劍)"],
            "stats": {
                "WeaponFixingDamage": "攻擊力",
                "WeaponAccuracy": "命中",
                "Critical": "暴擊",
                "Block": "格擋",
            },
        },
        110430047: {
            "name": "魂魄弓",
            "category": "弓",
            "race": "全部",
            "costumes": [],
            "stats": {},
        },
    }

    async def item_zh(self, item_id):
        return self._zh.get(item_id, {})

    async def item_names_zh(self, ids):
        return {i: self._zh[i]["name"] for i in ids if i in self._zh}


async def card_checks():
    stub = StubClient()

    cctx = await render.character_context(stub, ch, width=720)
    check("角色卡姓名", cctx["name"] == "A", cctx["name"])
    check("角色卡职业中文化", cctx["class_name"] == "弓星", cctx["class_name"])
    check("角色卡服务器中文化", cctx["server"] == "希埃尔", cctx["server"])
    check("角色卡属性中文化", any(s["name"] == "威力" for s in cctx["stats"]))
    check("角色卡守护者板中文化", cctx["daevanion"][0]["name"] == "奈萨肯", cctx["daevanion"][0]["name"])
    html = render.render("character", cctx)
    check("角色卡 HTML 生成", "<div class=\"card\">" in html and "弓星" in html)
    check("角色卡无未渲染变量", "{{" not in html)

    ectx = await render.equipment_context(stub, eq, ch.profile, width=720)
    check("装备卡槽位中文化", ectx["slots"][0]["slot"] == "主手武器", ectx["slots"][0]["slot"])
    check("装备卡品级中文化", ectx["slots"][0]["grade_label"] == "独特", ectx["slots"][0]["grade_label"])
    check("装备卡宠物", any(c["slot"] == "宠物" for c in ectx["companions"]))
    ehtml = render.render("equipment", ectx)
    check("装备卡 HTML 生成", "<div class=\"card\">" in ehtml and "主手武器" in ehtml)
    check("装备卡无未渲染变量", "{{" not in ehtml)
    miss = sum(1 for s in ectx["slots"] if not s["zh"])
    check(
        "未取到中文名的槽位逐项标注",
        ("官方无中文" in ehtml) == (miss > 0),
        f"未命中 {miss}/{len(ectx['slots'])} 件",
    )
    known = set(StubClient._zh)
    check(
        "中文名命中数与词表一致",
        sum(1 for s in ectx["slots"] if s["zh"]) == sum(1 for s in eq.slots if s.item_id in known),
        f"{sum(1 for s in ectx['slots'] if s['zh'])} 件",
    )

    ictx = await render.item_context(stub, item, width=720)
    check("道具卡名称反查并转简体", ictx["name"] == "训练用巨剑", ictx["name"])
    check("道具卡品级中文化", ictx["grade_label"] == "普通", ictx["grade_label"])
    check("道具卡品类中文化", ictx["category"] == "巨剑", ictx["category"])
    check("道具卡职业中文化", ictx["classes"] == ["剑星"], str(ictx["classes"]))
    check(
        "道具卡属性名中文化",
        [s["name"] for s in ictx["main_stats"]] == ["攻击力", "命中", "暴击", "格挡"],
        str([s["name"] for s in ictx["main_stats"]]),
    )
    check(
        "道具卡属性数值仍用国际服",
        ictx["main_stats"][0]["value"] == "4 ~ 6",
        ictx["main_stats"][0]["value"],
    )
    check("道具卡外观中文化", ictx["costumes"] == ["废弃遗产(巨剑)"], str(ictx["costumes"]))
    check(
        "道具卡可用种族",
        any(f["name"] == "可用种族" and f["value"] == "全部" for f in ictx["basics"]),
        str(ictx["basics"]),
    )
    check("道具卡无缺名提示", ictx["name_note"] == "", ictx["name_note"])
    ihtml = render.render("item", ictx)
    check("道具卡 HTML 生成", "<div class=\"card\">" in ihtml and "剑星" in ihtml)
    check("道具卡无未渲染变量", "{{" not in ihtml)

    etext = await render.equipment_text(stub, eq, ch.profile)
    check("装备文本回退", "主手武器" in etext, etext[:60])
    itext = await render.item_text(stub, item)
    check("道具文本反查并转简体", "训练用巨剑" in itext, itext[:40])
    check("道具文本属性中文化", "攻击力" in itext, itext[:120])
    stext = render.search_text(stub, page)
    check("搜索文本回退", "共 10000 条" in stext, stext[:40])
    check(
        "搜索候选带职业与种族",
        "剑星·天族" in stext,
        stext.splitlines()[1] if len(stext.splitlines()) > 1 else "",
    )
    check("搜索文本提示不带斜杠", "角色 <角色名>" in stext and "/角色" not in stext)

    # 译名命中时应覆盖英文名
    stub2 = StubClient()
    eq2 = models.Equipment.from_raw(load("equipment"))
    eq2.slots[0].item_id = 110430047
    ctx2 = await render.equipment_context(stub2, eq2, ch.profile)
    check("装备名反查覆盖英文", ctx2["slots"][0]["name"] == "魂魄弓", ctx2["slots"][0]["name"])


import asyncio

asyncio.run(card_checks())

print("\n== 配置 ==")
schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
# 中文化链路完全建立在英文原文上，开放语言开关只会让人把自己的词表映射弄失效
check("配置不含语言开关", "locale" not in schema, "、".join(schema))
check(
    "默认区域是日服",
    schema["region"]["default"] == "asia",
    schema["region"]["default"],
)
check(
    "区域选项与词表一致",
    set(schema["region"]["options"]) == set(routes.REGIONS) - {routes.GLOSSARY_REGION},
    str(schema["region"]["options"]),
)
check(
    "国际服取数固定英文",
    routes.region_of("asia").default_locale == "en-US",
    routes.region_of("asia").default_locale,
)
check(
    "译名来源区是台服繁中",
    routes.region_of(routes.GLOSSARY_REGION).default_locale == "zh-TW",
    routes.GLOSSARY_REGION,
)

print("\n== 活动时刻表 ==")
ev = importlib.import_module("aion2_plugin.core.events")
cards = importlib.import_module("aion2_plugin.render.cards")

wed = date(2026, 10, 7)  # 周三
rifts = [o for o in ev.day_occurrences(wed) if o.kind == ev.KIND_RIFT]
games = [o for o in ev.day_occurrences(wed) if o.kind == ev.KIND_MINIGAME]
check("裂隙每天 8 场", len(rifts) == 8, str(len(rifts)))
check(
    "裂隙落在固定时刻",
    [o.clock for o in rifts]
    == ["02:00", "05:00", "08:00", "11:00", "14:00", "17:00", "20:00", "23:00"],
    "、".join(o.clock for o in rifts),
)
check("小游戏每半小时一场", len(games) == 48, str(len(games)))
check(
    "次元入侵每小时 :30",
    len([o for o in ev.day_occurrences(wed) if o.kind == ev.KIND_INVASION]) == 24,
)
check("周三含每周重置", len([o for o in ev.day_occurrences(wed) if o.kind == ev.KIND_WEEKLY]) == 1)
check(
    "非周三无每周重置",
    not [o for o in ev.day_occurrences(date(2026, 10, 8)) if o.kind == ev.KIND_WEEKLY],
)
check(
    "重置在 15:00",
    [o.clock for o in ev.day_occurrences(wed) if o.kind == ev.KIND_DAILY] == ["15:00"],
)
check(
    "场次按时间排序",
    [o.start for o in ev.day_occurrences(wed)] == sorted(o.start for o in ev.day_occurrences(wed)),
)
check(":15 固定第一组", ev.minigame_set(datetime(2026, 10, 7, 3, 15)) == ev.MINIGAME_SETS[0])
check(":45 固定第二组", ev.minigame_set(datetime(2026, 10, 7, 9, 45)) == ev.MINIGAME_SETS[1])
check("每组 5 个游戏", all(len(s) == 5 for s in ev.MINIGAME_SETS))

both = (ev.KIND_RIFT, ev.KIND_MINIGAME)
due = ev.pending_reminders(datetime(2026, 10, 7, 16, 55), lead=5, kinds=both)
check("整点前 5 分钟命中裂隙", [o.clock for o in due] == ["17:00"], "、".join(o.clock for o in due))
check(
    "窗口之外不命中",
    ev.pending_reminders(datetime(2026, 10, 7, 16, 40), lead=5, kinds=(ev.KIND_RIFT,)) == [],
)
check(
    "小游戏前 5 分钟命中",
    [o.clock for o in ev.pending_reminders(datetime(2026, 10, 7, 16, 10), lead=5, kinds=both)]
    == ["16:15"],
)
check(
    "跨零点能取到次日的场次",
    [o.clock for o in ev.pending_reminders(datetime(2026, 10, 7, 23, 55), lead=20, kinds=both)]
    == ["00:15"],
)
check("提前量可调", len(ev.pending_reminders(datetime(2026, 10, 7, 16, 50), lead=10, kinds=both)) == 1)
check(
    "下一次裂隙",
    ev.next_occurrence(datetime(2026, 10, 7, 16, 26), ev.KIND_RIFT).clock == "17:00",
)
check(
    "下一次小游戏",
    ev.next_occurrence(datetime(2026, 10, 7, 16, 26), ev.KIND_MINIGAME).clock == "16:45",
)
check(
    "倒计时文案",
    ev.countdown(datetime(2026, 10, 7, 16, 26), datetime(2026, 10, 7, 17)) == "34 分钟后",
    ev.countdown(datetime(2026, 10, 7, 16, 26), datetime(2026, 10, 7, 17)),
)

rift_now = datetime(2026, 10, 7, 16, 55)
rift_note = ev.reminder_text(rift_now, ev.next_occurrence(rift_now, ev.KIND_RIFT))
check(
    "裂隙提醒带上小游戏",
    "时空裂隙" in rift_note and "小游戏" in rift_note and "17:15" in rift_note,
    rift_note.replace("\n", " / "),
)
game_now = datetime(2026, 10, 7, 16, 40)
game_note = ev.reminder_text(game_now, ev.next_occurrence(game_now, ev.KIND_MINIGAME))
check(
    "小游戏提醒带上裂隙",
    "小游戏" in game_note and "下一次裂隙 17:00" in game_note,
    game_note.replace("\n", " / "),
)

ctx = cards.events_context(now=datetime(2026, 10, 7, 16, 26))
check("卡片时间轴 24 格", len(ctx["cells"]) == 24, str(len(ctx["cells"])))
check("卡片标出 8 个裂隙小时", sum(1 for c in ctx["cells"] if c["rift"]) == 8)
check("卡片显示下一次裂隙", ctx["next_rift"] == "17:00（34 分钟后）", ctx["next_rift"])
check("卡片显示下一次小游戏", ctx["next_game"] == "16:45（19 分钟后）", ctx["next_game"])
check(
    "明日卡片不标已过",
    all(r["state"] == "" for r in cards.events_context(now=datetime(2026, 10, 7, 16, 26), tomorrow=True)["rifts"]),
)
check(
    "明日卡片改标首场裂隙",
    cards.events_context(now=datetime(2026, 10, 7, 16, 26), tomorrow=True)["head_value"] == "02:00"
    and ctx["head_value"] == "17:00（34 分钟后）",
    cards.events_context(now=datetime(2026, 10, 7, 16, 26), tomorrow=True)["head_value"],
)
live = [c for c in ctx["cells"] if c["hour"] == "16"][0]
check("进行中的小时被标出来", any(w["state"] == "soon" for w in live["windows"]), str(live))
check(
    "时刻表文本含裂隙与小游戏",
    "时空裂隙" in cards.events_text(datetime(2026, 10, 7, 16, 26))
    and ":15 组" in cards.events_text(datetime(2026, 10, 7, 16, 26)),
)
check("活动卡片能渲染", "活动时刻表" in render.render("events", ctx))
check(
    "卡片写明是本地推算",
    "本地推算" in render.render("events", ctx),
)

print("\n== 面板用的时刻表数据 ==")
table = ev.schedule_payload(False, datetime(2026, 10, 7, 16, 26))
check("时刻表含 24 小时", len(table["timeline"]) == 24, str(len(table["timeline"])))
check("裂隙场次与时刻表一致", len(table["rifts"]) == len(ev.RIFT_HOURS), str(len(table["rifts"])))
check(
    "下标出已过与即将",
    [row["state"] for row in table["rifts"]]
    == ["past", "past", "past", "past", "past", "soon", "future", "future"],
    str([row["state"] for row in table["rifts"]]),
)
check("带两组小游戏", len(table["sets"]) == 2 and len(table["sets"][0]["games"]) == 5)
check("带下一次活动", [item["kind"] for item in table["next"]] == [ev.KIND_RIFT, ev.KIND_MINIGAME])
check(
    "带重置时刻",
    table["dailyReset"] == "15:00" and table["weeklyReset"] == "周三 15:00",
    f"{table['dailyReset']} / {table['weeklyReset']}",
)
check("标出每周重置日", table["isWeeklyResetDay"] is True and ev.WEEKDAYS[2] == "周三")

tomorrow_table = ev.schedule_payload(True, datetime(2026, 10, 7, 16, 26))
check(
    "明日表标题与日期",
    tomorrow_table["label"] == "明日" and tomorrow_table["day"] == "2026-10-08",
)
check(
    "明日不标状态也不给下一次",
    all(row["state"] == "" for row in tomorrow_table["rifts"]) and not tomorrow_table["next"],
)
check(
    "时间轴的裂隙格与裂隙时刻表对应",
    [cell["hour"] for cell in table["timeline"] if cell["rift"]]
    == [f"{hour:02d}" for hour in ev.RIFT_HOURS],
)

print("\n== 面板数据与配置校验 ==")
panel = importlib.import_module("aion2_plugin.core.settings")
schema = panel.load_schema(PLUGIN_DIR)
defaults = panel.defaults_of(schema)
values = dict(defaults)
values.update({"region": "asia", "quiet_end": "", "rate_limit": 5.0})

groups = panel.describe(schema, values, defaults)
check("面板分组非空", len(groups) >= 5, str(len(groups)))
check(
    "面板覆盖全部登记项",
    {f["key"] for g in groups for f in g["fields"]} == set(panel.editable_keys(schema)),
)
check(
    "未登记的 schema 项不进面板",
    {f["key"] for g in groups for f in g["fields"]} <= set(schema),
)
check(
    "分组顺序固定",
    [g["key"] for g in groups][:2] == ["query", "output"],
    str([g["key"] for g in groups]),
)
region_field = [f for g in groups for f in g["fields"] if f["key"] == "region"][0]
check("区域下拉用中文标签", [o["label"] for o in region_field["options"]][-1] == "日服")
check("面板带当前值", region_field["value"] == "asia" and region_field["default"] == "asia")
lead_field = [f for g in groups for f in g["fields"] if f["key"] == "event_lead"][0]
check("数值项带范围与单位", (lead_field["min"], lead_field["max"], lead_field["unit"]) == (1, 60, "分钟"))
check("提醒子项声明依赖", lead_field["depends"] == ["event_push"])
check(
    "时钟项标成 clock 控件",
    [f for g in groups for f in g["fields"] if f["key"] == "quiet_start"][0]["control"] == "clock",
)

for raw, ok, label in (
    (True, True, "布尔值通过"),
    (False, True, "布尔假值通过"),
    (1, False, "数字当布尔被拒"),
    ("true", False, "字符串当布尔被拒"),
):
    _, error = panel.validate("t2s", raw, schema)
    check(label, (not error) is ok, error)

for raw, ok, label in (
    (5, True, "整数通过"),
    ("15", True, "数字字符串可转"),
    (1, True, "等于下限通过"),
    (60, True, "等于上限通过"),
    (0, False, "低于下限被拒"),
    (61, False, "高于上限被拒"),
    (7.5, False, "小数被拒"),
    (True, False, "布尔当数字被拒"),
    ("x", False, "非数字字符串被拒"),
    (None, False, "None 被拒"),
):
    _, error = panel.validate("event_lead", raw, schema)
    check(label, (not error) is ok, error)

value, error = panel.validate("event_lead", "15", schema)
check("数字换算成 int", value == 15 and isinstance(value, int), repr(value))
value, error = panel.validate("rate_limit", "2.5", schema)
check("小数换算成 float", value == 2.5 and isinstance(value, float), repr(value))

for raw, ok, label in (
    ("", True, "空时刻表示不静默"),
    ("00:00", True, "零点通过"),
    ("23:59", True, "末刻通过"),
    ("24:00", False, "24 点被拒"),
    ("9:00", False, "缺前导零被拒"),
    ("0900", False, "缺冒号被拒"),
    ("08:60", False, "分钟越界被拒"),
):
    _, error = panel.validate("quiet_start", raw, schema)
    check(label, (not error) is ok, error)

for raw, ok, label in (
    ("asia", True, "合法区域通过"),
    ("ASIA", False, "区域大小写敏感"),
    ("xx", False, "未知区域被拒"),
    ("", False, "空区域被拒"),
):
    _, error = panel.validate("region", raw, schema)
    check(label, (not error) is ok, error)

unregistered = dict(schema)
unregistered["secret_key"] = {"type": "string", "default": "", "description": "未登记项"}
_, error = panel.validate("secret_key", "x", unregistered)
check("schema 里有但未登记的项被拒", bool(error), error)
check("现网 schema 全部已登记", set(panel.editable_keys(schema)) == set(schema))

clean, errors = panel.validate_payload({"t2s": True, "event_lead": 99, "nope": 1}, schema)
check("批量校验只留合法的", clean == {"t2s": True}, str(clean))
check("批量校验收集全部错误", len(errors) == 2, str(errors))
clean, errors = panel.validate_payload("not-a-dict", schema)
check("非对象请求体被拒", clean == {} and bool(errors), str(errors))

check(
    "默认值取自 schema",
    (defaults["region"], defaults["event_lead"], defaults["image_width"]) == ("asia", 5, 720),
    str(defaults["region"]),
)
check("每项都有说明与提示", all(f["label"] and f["type"] for g in groups for f in g["fields"]))
check(
    "越界的历史值会被报出来",
    panel.normalize({"event_lead": 999}, schema)["issues"][0]["key"] == "event_lead",
)

print(f"\n结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
if FAIL:
    print("失败项：")
    for name in FAIL:
        print(f"  - {name}")
sys.exit(1 if FAIL else 0)
