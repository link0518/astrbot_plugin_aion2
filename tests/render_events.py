"""生成活动时刻表卡片的 HTML，交给 render_preview.py 截图检查。

按当前时间构造，尽量贴近真实输出；不需要联网。
"""

import importlib
import sys
import types
from datetime import datetime
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

pkg = types.ModuleType("aion2_plugin")
pkg.__path__ = [str(PLUGIN_DIR)]
sys.modules["aion2_plugin"] = pkg

render = importlib.import_module("aion2_plugin.render")
cards = importlib.import_module("aion2_plugin.render.cards")

now = datetime.now()
for name, tomorrow in (("card_events.html", False), ("card_events_tomorrow.html", True)):
    ctx = cards.events_context(now=now, tomorrow=tomorrow, width=720)
    path = OUT / name
    path.write_text(
        "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body "
        "style='background:#0b0d13;margin:0;padding:16px'>"
        + render.render("events", ctx)
        + "</body></html>",
        encoding="utf-8",
    )
    print(f"  {path.name}")

sys.exit(0)
