"""配置面板的离线预览：用真实的配置 schema 与时刻表数据喂一个假 bridge，截图检查外观。

面板在 AstrBot 里由 Dashboard 注入 bridge SDK，这里造一个等价实现，
因此不装 AstrBot 也能看到页面的实际样子，并验证保存、重置、切页等交互。
"""

import asyncio
import importlib
import json
import sys
import threading
import types
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.async_api import async_playwright

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PAGES = PLUGIN_DIR / "pages" / "console"
OUT = Path(__file__).resolve().parent / "out"

MOCK_JS = """
const DATA = __DATA__;

(function () {
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const params = new URLSearchParams(location.search);
  const isDark = params.get("theme") === "dark";
  document.documentElement.dataset.theme = isDark ? "dark" : "light";

  // 截图时把固定定位的头部与保存栏放回文档流，整页截图才不会错位
  if (params.get("layout") === "flat") {
    const style = document.createElement("style");
    style.textContent =
      ".top,.savebar{position:static}" + ".main{padding-bottom:18px}" +
      ".nav{position:static}";
    document.head.append(style);
  }

  let groups = clone(DATA.groups);
  let subscriptions = clone(DATA.subscriptions);
  let state = clone(DATA.state);

  const findField = (key) => {
    for (const group of groups) {
      for (const field of group.fields) if (field.key === key) return field;
    }
    return null;
  };

  window.AstrBotPluginPage = {
    ready: async () => ({ pageTitle: "AION2 查询 · 控制台", isDark }),
    getContext: () => ({ isDark }),
    getLocale: () => "zh-CN",
    getI18n: () => ({}),
    t: (key, fallback) => (fallback === undefined ? key : fallback),
    onContext: () => () => {},
    apiGet: async (endpoint, query = {}) => {
      if (endpoint === "overview") return clone({ ...DATA, groups, subscriptions, state });
      if (endpoint === "state") return { state: clone(state) };
      if (endpoint === "schedule") {
        return clone(query.day === "tomorrow" ? DATA.tomorrow : DATA.schedule);
      }
      throw new Error("预览未实现的接口：" + endpoint);
    },
    apiPost: async (endpoint, body = {}) => {
      if (endpoint === "config") {
        const changed = [];
        for (const [key, value] of Object.entries(body.values || {})) {
          const field = findField(key);
          if (!field) throw new Error("未知配置项 " + key);
          if (JSON.stringify(field.value) !== JSON.stringify(value)) changed.push(key);
          field.value = value;
        }
        return clone({ saved: body.values, changed, groups, state });
      }
      if (endpoint === "reset") {
        const keys = body.keys || [];
        for (const key of keys) {
          const field = findField(key);
          if (field) field.value = clone(field.default);
        }
        return clone({ reset: keys, changed: keys, groups, state });
      }
      if (endpoint === "cache/clear") {
        state.cache.entries = 0;
        if (body.scope !== "result") state.cache.glossary = 0;
        return clone({ removed: 14, scope: body.scope, state });
      }
      if (endpoint === "selftest") {
        return clone({ ok: true, region: "日服", servers: 36, ms: 218, state });
      }
      if (endpoint === "subscriptions/remove") {
        subscriptions = subscriptions.filter((row) => row.target !== body.target);
        state.push.subscribers = subscriptions.length;
        return clone({ removed: body.target, subscriptions, state });
      }
      if (endpoint === "subscriptions/test") return clone({ sent: body.target });
      throw new Error("预览未实现的接口：" + endpoint);
    },
  };
})();
"""


def load_core():
    """把插件目录当作包加载，只用到不依赖 AstrBot 运行时的那部分。"""
    package = types.ModuleType("aion2_plugin")
    package.__path__ = [str(PLUGIN_DIR)]
    sys.modules["aion2_plugin"] = package
    return importlib.import_module("aion2_plugin.core")


def fake_state(core, now: datetime) -> dict:
    events = core.events
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
    return {
        "now": f"{now:%Y-%m-%d %H:%M:%S}",
        "region": {"key": "asia", "label": "日服"},
        "push": {
            "enabled": True,
            "lead": 5,
            "kinds": ["时空裂隙", "小游戏"],
            "quiet": "00:00–08:00",
            "quietNow": False,
            "running": True,
            "subscribers": 2,
            "sent": 7,
        },
        "cache": {"entries": 14, "ttl": 600, "glossary": 128},
        "next": upcoming,
    }


def build_data() -> dict:
    core = load_core()
    schema = core.settings.load_schema(PLUGIN_DIR)
    defaults = core.settings.defaults_of(schema)
    values = dict(defaults)
    values.update({"region": "asia", "free_trigger": True, "t2s": True})
    now = datetime.now()
    return {
        "groups": core.settings.describe(schema, values, defaults),
        "state": fake_state(core, now),
        "subscriptions": [
            {
                "target": "aiocqhttp:GroupMessage:887766554",
                "label": "群 887766554",
                "since": "10-05 21:12",
            },
            {
                "target": "aiocqhttp:FriendMessage:10086",
                "label": "私聊 10086",
                "since": "10-07 09:03",
            },
        ],
        "schedule": core.events.schedule_payload(False, now),
        "tomorrow": core.events.schedule_payload(True, now),
        "timestamp": f"{now:%Y-%m-%d %H:%M:%S}",
    }


class Handler(SimpleHTTPRequestHandler):
    mock_js = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PAGES), **kwargs)

    def log_message(self, *args):
        pass

    def _send(self, body: str, content_type: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/__mock.js":
            self._send(self.mock_js, "application/javascript; charset=utf-8")
            return
        if path in ("/", "/index.html"):
            html = (PAGES / "index.html").read_text(encoding="utf-8")
            html = html.replace(
                '    <script type="module" src="./app.js"></script>',
                '    <script src="/__mock.js"></script>\n'
                '    <script type="module" src="./app.js"></script>',
            )
            self._send(html, "text/html; charset=utf-8")
            return
        super().do_GET()


async def shoot(port: int) -> list[Path]:
    shots: list[Path] = []
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        for theme in ("light", "dark"):
            page = await browser.new_page(
                viewport={"width": 1120, "height": 880}, device_scale_factor=2
            )
            await page.goto(
                f"http://127.0.0.1:{port}/?theme={theme}&layout=flat", wait_until="load"
            )
            await page.wait_for_selector(".stat", timeout=15000)
            await page.wait_for_timeout(250)

            target = OUT / f"webui_overview_{theme}.png"
            await page.screenshot(path=str(target), full_page=True)
            shots.append(target)

            await page.click('button[data-tab="config"]')
            await page.wait_for_selector(".field", timeout=5000)
            # 改一项再还原，确认「已改」标记与保存栏会跟着出现
            await page.click('.field:has-text("繁体转简体") .switch')
            await page.wait_for_timeout(150)
            await page.click('.field:has-text("繁体转简体") .switch')
            await page.wait_for_timeout(150)
            await page.click('.field:has-text("提前推送分钟数") input[type="number"]')
            await page.fill('.field:has-text("提前推送分钟数") input[type="number"]', "15")
            await page.dispatch_event(
                '.field:has-text("提前推送分钟数") input[type="number"]', "change"
            )
            await page.wait_for_timeout(250)
            target = OUT / f"webui_config_{theme}.png"
            await page.screenshot(path=str(target), full_page=True)
            shots.append(target)

            await page.click("#saveBtn")
            await page.wait_for_timeout(350)
            target = OUT / f"webui_saved_{theme}.png"
            await page.screenshot(path=str(target), full_page=True)
            shots.append(target)

            await page.click('button[data-tab="schedule"]')
            await page.wait_for_selector(".tl", timeout=5000)
            await page.wait_for_timeout(250)
            target = OUT / f"webui_schedule_{theme}.png"
            await page.screenshot(path=str(target), full_page=True)
            shots.append(target)

            await page.close()
        await browser.close()
    return shots


def main() -> int:
    Handler.mock_js = MOCK_JS.replace("__DATA__", json.dumps(build_data(), ensure_ascii=False))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        shots = asyncio.run(shoot(port))
    finally:
        server.shutdown()

    for path in shots:
        print(f"  {path.name}  {path.stat().st_size // 1024} KB")
    return 0


sys.exit(main())
