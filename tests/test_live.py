"""联调测试：直接打真实接口，验证客户端、路由、中文化、卡片。

会向上游发请求，注意限流。运行时会把卡片 HTML 存到 tests/out/ 便于肉眼检查。
"""

import asyncio
import importlib
import sys
import time
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

pkg = types.ModuleType("aion2_plugin")
pkg.__path__ = [str(PLUGIN_DIR)]
sys.modules["aion2_plugin"] = pkg

core = importlib.import_module("aion2_plugin.core")
render = importlib.import_module("aion2_plugin.render")
i18n = importlib.import_module("aion2_plugin.i18n")
cache_mod = importlib.import_module("aion2_plugin.core.cache")

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")


async def main() -> int:
    glossary = cache_mod.GlossaryStore(OUT / "glossary.json", 2592000)
    client = core.Aion2Client(region_key="asia", rate_limit=5.0, timeout=20, glossary=glossary)
    check("默认区域是日服", client.region.key == "asia" and client.region.label == "日服", client.region.label)
    check("日服站点与国际服同源", client.region.site == "https://aion2.plaync.com", client.region.site)
    # 区域只决定查哪个分片，取数语言固定英文，中文由词表映射
    check(
        "日服也走英文取数",
        client.routes.locale == "en-US" and client.routes.region.shard == "as",
        f"locale={client.routes.locale} shard={client.routes.region.shard}",
    )

    print("== 基础接口 ==")
    servers = await client.servers()
    check(
        "服务器列表",
        len(servers) > 0 and {s.race_id for s in servers} == {1, 2},
        f"{len(servers)} 个（天族 {sum(1 for s in servers if s.race_id == 1)} / "
        f"魔族 {sum(1 for s in servers if s.race_id == 2)}）",
    )
    # 分片越多的区域越容易暴露词表缺漏，必须全命中，不能靠回退英文蒙混
    zh = [s for s in servers if i18n.server_name(s.name) != s.name]
    check("服务器名全部中文化", len(zh) == len(servers), f"{len(zh)}/{len(servers)}")
    check(
        "日服服务器编号从 15xx 起",
        servers[0].server_id == 1501 and servers[0].name == "Siel",
        f"{servers[0].name}({servers[0].server_id})",
    )

    classes = await client.classes()
    check("职业列表", len(classes) == 8, f"{len(classes)} 个")
    print("    职业译名：" + "、".join(f"{c.name}→{i18n.class_name(c.id, c.text)}" for c in classes))

    print("\n== 角色 ==")
    page = await client.search_characters("mizoo", size=20)
    check("角色搜索", len(page.items) > 0, f"命中 {page.total} 条")
    target = max(page.items, key=lambda c: c.level)
    print(
        f"    取样：{target.name} Lv{target.level} "
        f"{i18n.pc_class_name(target.pc_id)} "
        f"{i18n.server_name(target.server_name)}({target.server_id})"
    )
    check(
        "搜索结果能解出职业",
        i18n.pc_class_name(target.pc_id) != "",
        f"pcId={target.pc_id} → {i18n.pc_class_name(target.pc_id)}",
    )
    check(
        "搜索结果已去高亮标签",
        all("<" not in c.name and ">" not in c.name for c in page.items),
        "、".join(repr(c.name) for c in page.items[:3]),
    )

    ref = core.CharacterRef(server_id=target.server_id, character_id=target.ref.character_id)
    ch = await client.character(ref)
    check("角色档案", ch.profile.name != "", ch.profile.name)
    check("角色属性", len(ch.stats) > 0, f"{len(ch.stats)} 项")
    check("守护者板", len(ch.daevanion) > 0, f"{len(ch.daevanion)} 个")
    by_pc = i18n.pc_class_name(ch.profile.pc_id)
    by_name = i18n.class_name(0, ch.profile.class_name)
    check("职业中文化", by_pc != "" and by_pc == by_name, f"pcId 解出 {by_pc}，职业名解出 {by_name}")
    check(
        "属性名中文化",
        all(stat.name != "" for stat in ch.stats),
        "、".join(i18n.stat_name(s.type, s.name) for s in ch.stats[:4]),
    )

    print("\n== 装备（含译名反查耗时）==")
    t0 = time.monotonic()
    equip = await client.equipment(ref)
    ctx = await render.equipment_context(client, equip, ch.profile, width=720)
    elapsed = time.monotonic() - t0
    resolved = sum(1 for s in ctx["slots"] if s["name"])
    check("装备槽位", len(equip.slots) > 0, f"{len(equip.slots)} 个")
    check("译名反查有结果", resolved > 0, f"{resolved}/{len(ctx['slots'])} 个槽位取到中文名")
    print(f"    首次反查耗时 {elapsed:.1f}s")
    sample = [s for s in ctx["slots"] if s["name"]][:3]
    for s in sample:
        print(f"      {s['slot']}：{s['name']} [{s['grade_label']}]")

    t1 = time.monotonic()
    await render.equipment_context(client, equip, ch.profile, width=720)
    print(f"    二次反查耗时 {time.monotonic() - t1:.2f}s（命中缓存）")

    print("\n== 道具 ==")
    item = await client.item(110160008)
    ictx = await render.item_context(client, item, width=720)
    check("道具详情", item.id == 110160008, item.name)
    check("道具名反查", ictx["name"] != item.name, f"{item.name} → {ictx['name']}")
    check("道具品级中文化", ictx["grade_label"] == "普通", ictx["grade_label"])
    check(
        "道具属性名中文化",
        [s["name"] for s in ictx["main_stats"]] == ["攻击力", "命中", "暴击", "格挡"],
        "、".join(s["name"] for s in ictx["main_stats"]),
    )
    check("道具数值仍取国际服", ictx["main_stats"][0]["value"] == "4 ~ 6", ictx["main_stats"][0]["value"])
    check("道具品类中文化", ictx["category"] == "巨剑", ictx["category"])
    check(
        "道具可用种族中文化",
        any(f["name"] == "可用种族" and f["value"] == "全部" for f in ictx["basics"]),
        str(ictx["basics"]),
    )

    print("\n== 排行与公告 ==")
    try:
        await client.ranking("abyss", target.server_id)
        check("排行榜", True, "有数据")
    except core.NoSeason:
        check("排行榜按未开放处理", True, "上游返回 season 为空，已给出友好提示")
    except core.Aion2Error as exc:
        check("排行榜", False, f"{type(exc).__name__}: {exc}")

    posts = await client.posts("notice", 5)
    check("官方公告", len(posts) == 5, f"{len(posts)} 条")
    if posts:
        print(f"    最新：{i18n.localized(posts[0].title)}")
        body = await client.post("notice", posts[0].id)
        check("公告正文", len(body.html) > 500, f"{len(body.html)} 字节")

    print("\n== 卡片产出 ==")
    cctx = await render.character_context(client, ch, width=720, note="")
    files = {
        "character": render.render("character", cctx),
        "equipment": render.render("equipment", ctx),
        "item": render.render("item", ictx),
    }
    for name, html in files.items():
        path = OUT / f"card_{name}.html"
        path.write_text(
            "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body "
            "style='background:#0b0d13;margin:0;padding:0'>" + html + "</body></html>",
            encoding="utf-8",
        )
        check(f"{name} 卡片", len(html) > 500 and "{{" not in html, f"{len(html)} 字节")
    print(f"    已写入 {OUT}")

    print("\n== 文本回退 ==")
    text = await render.equipment_text(client, equip, ch.profile)
    check("装备文本", "主手武器" in text, text.splitlines()[1][:60] if len(text.splitlines()) > 1 else "")
    print("    " + text.replace("\n", "\n    ")[:600])

    glossary.save()
    print(f"\n译名缓存 {len(glossary)} 条 → {OUT / 'glossary.json'}")
    await client.close()

    print(f"\n结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
    for name in FAIL:
        print(f"  - {name}")
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
