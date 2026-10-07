"""基纳价格：聚合 7881 与 PlayerAuctions 的各区实时价。

两个源都上了反爬，抓取集中在一段 Playwright 会话里完成：

- 7881 的商品接口带签名（lb-timestamp + lb-sign），由页面业务代码现算，
  外部拼不出来，因此只能在页面里钩 XHR 把它自己发出的响应收下来。
  按区取数靠 URL 里的完整 groupId（形如 G6212P017），传短写法接口会返回空。
- PlayerAuctions 全站 Cloudflare 挑战，只有系统真实浏览器内核过得去：
  Playwright 自带的 Chromium 会被识别，表现为标题正常但正文始终为空。
  另外用持久化的浏览器 profile，把放行 cookie 留到下一次抓取。

价格一律归一到「元 / 百万基纳」再比较：7881 报的是元/万基纳，乘 100；
PA 报的是美元/百万基纳，乘当日汇率。卡面以中位价为准，区间取 10%~90%
分位 —— 两个源的两头都见过 $0.05、$100 这类异常挂单，极值没有参考价值。
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

# 7881 里 AION2 的标识：游戏「永恒之塔2国际服」、交易类型「基纳」
GAME_ID = "G6212"
GOODS_TYPE = "100001"

# 商品列表页。group 传完整 groupId，server 传 0 表示该区全部服
LIST_PAGE = "https://search.7881.com/{game}-{goods}-{group}-{server}-0.html"

# 区 → 该区两个阵营在 7881 的 groupId
ZONE_7881: dict[str, tuple[str, str]] = {
    "asia": ("G6212P017", "G6212P022"),
    "eu": ("G6212P021", "G6212P010"),
    "naw": ("G6212P013", "G6212P014"),
    "nae": ("G6212P019", "G6212P012"),
    "sa": ("G6212P020", "G6212P016"),
    "tw": ("G6212P001", "G6212P002"),
    "kr": ("G6212P003", "G6212P004"),
}

# 区 → PlayerAuctions 的 Serverid（天族、魔族）
ZONE_PA: dict[str, tuple[int, int]] = {
    "asia": (15389, 15391),
    "eu": (15363, 15368),
    "naw": (15373, 15375),
    "nae": (15377, 15380),
    "sa": (15383, 15386),
}

# 展示顺序与名称，与本插件的区域口径保持一致
ZONE_LABELS: dict[str, str] = {
    "asia": "日服",
    "eu": "欧服",
    "naw": "美西",
    "nae": "美东",
    "sa": "南美服",
    "tw": "台服",
    "kr": "韩服",
}

# 国际服五大区，默认只抓这些
DEFAULT_ZONES: tuple[str, ...] = ("asia", "eu", "naw", "nae", "sa")

SOURCE_7881 = "7881"
SOURCE_PA = "pa"
SOURCE_LABELS = {SOURCE_7881: "7881", SOURCE_PA: "PlayerAuctions"}

# 汇率源，按顺序尝试
RATE_URLS = (
    "https://open.er-api.com/v6/latest/USD",
    "https://api.frankfurter.app/latest?from=USD&to=CNY",
)

# 先用系统真实内核，最后才退回 Playwright 自带 Chromium
BROWSER_CHANNELS: tuple[str | None, ...] = ("chrome", "msedge", None)

# 挂单太少时中位价会被单条异常挂单带偏（南美服曾出现只有 1 条、报出 50 元），
# 少于这个条数就不报价，只记一条原因。
MIN_SAMPLES = 3

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 钩住 XMLHttpRequest，把响应体留在页面里。7881 的签名拦不住浏览器自己发的请求，
# 但能拦住我们手动重发的，所以只能这样取。
HOOK_XHR = """
(function () {
  window.__kinah = [];
  const open = XMLHttpRequest.prototype.open;
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) { this.__u = u; return open.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function () {
    const self = this;
    this.addEventListener('load', function () {
      try { window.__kinah.push({ url: self.__u, resp: self.responseText }); } catch (e) {}
    });
    return send.apply(this, arguments);
  };
  // 顺手压掉 webdriver 特征
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
})();
"""

# PA 的商品数据是服务端直出的，页面内 fetch 即可拿到，不必逐个渲染。
# 失败重试一次：放行后紧接的重载会让第一个请求落空。
PA_FETCH_JS = """
async (sids) => {
    const one = async (sid) => {
        const url = '/zh/aion-2-kinah/?Serverid=' + sid;
        for (let attempt = 0; attempt < 3; attempt++) {
            try {
                const r = await fetch(url, { credentials: 'include', cache: 'no-store' });
                const t = await r.text();
                const units = [...t.matchAll(/title=['"]\\$([\\d.]+) \\/ M /g)].map(m => parseFloat(m[1]));
                const hit = t.match(/<span class="count">(\\d+)<\\/span>/);
                return { units: units, count: hit ? parseInt(hit[1], 10) : 0 };
            } catch (e) {
                if (attempt === 2) {
                    return { units: [], count: 0, error: String(e).slice(0, 80) };
                }
                await new Promise(res => setTimeout(res, 1500));
            }
        }
    };
    const out = {};
    for (let i = 0; i < sids.length; i += 3) {
        const batch = sids.slice(i, i + 3);
        const res = await Promise.all(batch.map(one));
        batch.forEach((s, j) => { out[s] = res[j]; });
    }
    return out;
}
"""


class KinahError(Exception):
    """抓取链路不可用，或配置无法满足。"""


def available() -> tuple[bool, str]:
    """检查抓取所需的 Playwright 是否就绪。"""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False, "未安装 playwright，基纳价格抓取不可用"
    return True, ""


@dataclass(frozen=True)
class Quote:
    """一个区在一个来源上的价格，单位统一为「元 / 百万基纳」。"""

    source: str
    region: str
    low: float
    median: float
    high: float
    samples: int  # 参与统计的条数
    total: int  # 该区在售总数

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "region": self.region,
            "low": round(self.low, 4),
            "median": round(self.median, 4),
            "high": round(self.high, 4),
            "samples": self.samples,
            "total": self.total,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Quote":
        return cls(
            source=str(raw.get("source") or ""),
            region=str(raw.get("region") or ""),
            low=float(raw.get("low") or 0),
            median=float(raw.get("median") or 0),
            high=float(raw.get("high") or 0),
            samples=int(raw.get("samples") or 0),
            total=int(raw.get("total") or 0),
        )


@dataclass
class Snapshot:
    """一次抓取的完整结果。"""

    fetched_at: float = 0.0
    rate: float = 0.0  # USD → CNY，取不到为 0
    quotes: list[Quote] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    elapsed: float = 0.0
    zones: tuple[str, ...] = ()
    channel: str = ""

    def quote(self, region: str, source: str) -> Quote | None:
        for item in self.quotes:
            if item.region == region and item.source == source:
                return item
        return None

    def rows(self) -> list[dict]:
        """按展示顺序整理成卡片用的行。"""
        order = list(self.zones) or list(DEFAULT_ZONES)
        out = []
        for region in order:
            out.append(
                {
                    "key": region,
                    "label": ZONE_LABELS.get(region, region),
                    "cn": self.quote(region, SOURCE_7881),
                    "us": self.quote(region, SOURCE_PA),
                }
            )
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "fetched_at": self.fetched_at,
            "rate": self.rate,
            "quotes": [q.to_dict() for q in self.quotes],
            "errors": list(self.errors),
            "elapsed": round(self.elapsed, 1),
            "zones": list(self.zones),
            "channel": self.channel,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "Snapshot | None":
        if not isinstance(raw, dict):
            return None
        try:
            return cls(
                fetched_at=float(raw.get("fetched_at") or 0),
                rate=float(raw.get("rate") or 0),
                quotes=[Quote.from_dict(q) for q in (raw.get("quotes") or []) if isinstance(q, dict)],
                errors=[str(e) for e in (raw.get("errors") or [])],
                elapsed=float(raw.get("elapsed") or 0),
                zones=tuple(str(z) for z in (raw.get("zones") or ())),
                channel=str(raw.get("channel") or ""),
            )
        except (TypeError, ValueError):
            return None


# ------------------------------------------------------------------ 小工具


def _num(raw: Any) -> float | None:
    """把上游返回的数字（可能是字符串）转成 float，失败返回 None。"""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")) or value <= 0:
        return None
    return value


def _percentile(ordered: list[float], frac: float) -> float:
    """线性插值取分位，输入需已升序。"""
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    pos = frac * (len(ordered) - 1)
    lower = min(int(pos), len(ordered) - 1)
    upper = min(lower + 1, len(ordered) - 1)
    weight = pos - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _stats(values: list[float]) -> tuple[float, float, float]:
    """返回 (区间下限, 中位, 区间上限)。空列表返回三个 0。

    区间取 10%~90% 分位而不是最低/最高：两个源都出现过 $0.05、$100 这类
    异常挂单，用极值会把区间拉得完全没有参考价值。
    """
    if not values:
        return 0.0, 0.0, 0.0
    ordered = sorted(values)
    return _percentile(ordered, 0.1), statistics.median(ordered), _percentile(ordered, 0.9)


def _json(raw: Any) -> dict:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _quote(source: str, region: str, units: list[float], total: int) -> tuple[Quote | None, str]:
    """把一组已归一的单价收成一个报价。返回 (报价, 不报价的原因)。"""
    label = f"{SOURCE_LABELS.get(source, source)} {ZONE_LABELS.get(region, region)}"
    if not units:
        return None, f"{label} 没有可用的在售数据"
    if len(units) < MIN_SAMPLES:
        return None, f"{label} 在售挂单仅 {len(units)} 条，样本太少不作报价"
    low, median, high = _stats(units)
    return Quote(source, region, low, median, high, len(units), total or len(units)), ""


def _safe_text(page) -> str:
    try:
        return page.evaluate("() => (document.body ? document.body.innerText : '')") or ""
    except Exception:  # noqa: BLE001 - 页面可能在重载，取不到就当空
        return ""


# ------------------------------------------------------------------ 抓取


def fetch_rate(timeout: float = 12.0) -> tuple[float, str]:
    """美元兑人民币汇率，返回 (汇率, 错误)。取不到时汇率为 0。"""
    import httpx

    headers = {"User-Agent": UA}
    for url in RATE_URLS:
        try:
            resp = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
            if resp.status_code != 200:
                continue
            data = resp.json()
            rates = data.get("rates") if isinstance(data, dict) else None
            if not isinstance(rates, dict):
                continue
            value = _num(rates.get("CNY"))
            if value:
                return value, ""
        except Exception:  # noqa: BLE001 - 换下一个源继续试
            continue
    return 0.0, "汇率源都不可用，美元价无法换算成人民币"


PA_HOME = "https://www.playerauctions.com/zh/aion-2-kinah/"

def _launch_args() -> list[str]:
    """浏览器启动参数。

    容器里通常以 root 运行，Chrome 的沙箱在这种环境下起不来，必须关掉；
    容器的 /dev/shm 默认只有 64MB，不换地方会渲染到一半崩。
    """
    args = ["--disable-blink-features=AutomationControlled"]
    if sys.platform.startswith("linux"):
        args += ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
    return args


CONTEXT_KW = {
    "user_agent": UA,
    "locale": "zh-CN",
    "viewport": {"width": 1440, "height": 900},
}


def _open_browser(pw, profile_dir=None) -> tuple[Any, Any, str]:
    """挑一个内核开浏览器，返回 (context, 关闭函数, 内核名)。

    给了 profile_dir 就用持久化 profile：Cloudflare 放行的 cookie 能留到下一次。
    否则每次都是全新浏览器，反复过挑战很容易被当成异常流量，同一台机器上
    时而过得去时而过不去。持久化的 profile 打不开时退回非持久模式。
    """
    last: Exception | None = None
    for channel in BROWSER_CHANNELS:
        base = {"headless": True, "args": _launch_args()}
        if channel:
            base["channel"] = channel
        if profile_dir is not None:
            try:
                Path(profile_dir).mkdir(parents=True, exist_ok=True)
                context = pw.chromium.launch_persistent_context(
                    str(profile_dir), **base, **CONTEXT_KW
                )
                return context, context.close, channel or "chromium"
            except Exception as exc:  # noqa: BLE001 - profile 有问题时退回非持久
                last = exc
        try:
            browser = pw.chromium.launch(**base)
            context = browser.new_context(**CONTEXT_KW)
            return context, browser.close, channel or "chromium"
        except Exception as exc:  # noqa: BLE001 - 换下一个内核
            last = exc
    raise KinahError(f"没有可用的浏览器内核：{last}")


def _open_pa(page) -> str:
    """打开 PA 目标页并等 Cloudflare 放行。返回错误描述，空串表示成功。

    挑战结果不稳定，同一台机器上一次过得去下一次可能卡住，因此重来两轮。
    """
    last = ""
    for _ in range(2):
        try:
            page.goto(PA_HOME, wait_until="commit", timeout=45000)
        except Exception as exc:  # noqa: BLE001 - 超时常见，靠 _settle 兜住
            last = f"打开失败：{type(exc).__name__}"
        if _settle(page, timeout=50.0):
            return ""
        last = "没能通过 Cloudflare 挑战"
        # 卡住时先离开该页再回来，避免在挑战面上原地打转
        try:
            page.goto("about:blank", wait_until="commit", timeout=10000)
        except Exception:  # noqa: BLE001 - 清页面失败不影响重试
            pass
        page.wait_for_timeout(3000)
    return last


def _settle(page, timeout: float = 40.0, need: int = 800) -> bool:
    """等页面真正渲染出来。

    Cloudflare 挑战期间标题是 "Just a moment..."、正文为空；放行之后页面还会
    再重载一次，所以除正文长度外也要确认标题已经换成目标页的。
    7881 的列表同样是异步拉取的，一并靠这里等。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        page.wait_for_timeout(2500)
        try:
            title = page.title()
        except Exception:  # noqa: BLE001 - 导航中途取不到标题，继续等
            continue
        if not title or "Just a moment" in title:
            continue
        if len(_safe_text(page)) >= need:
            return True
    return False


def _quiesce(page, seconds: float = 3.0) -> None:
    """等页面安静下来再发请求。

    Cloudflare 放行后常紧跟着一次重载，此时发出的 fetch 会被当成导航中断而
    直接失败（表现为 TypeError: Failed to fetch）。
    """
    page.wait_for_timeout(int(seconds * 1000))


def _wait_xhr(page, keyword: str, timeout: float = 15.0) -> dict | None:
    """等页面自己发出某个接口并留下响应。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        page.wait_for_timeout(700)
        try:
            reqs = page.evaluate("() => window.__kinah || []")
        except Exception:  # noqa: BLE001 - 导航中途取不到，继续等
            continue
        for item in reqs or []:
            if keyword in str(item.get("url") or ""):
                return item
    return None


def fetch_7881(page, zones: tuple[str, ...]) -> tuple[list[Quote], list[str]]:
    """从 7881 取各区的基纳价格（人民币）。"""
    quotes: list[Quote] = []
    errors: list[str] = []
    for region in zones:
        groups = ZONE_7881.get(region)
        if not groups:
            continue
        label = ZONE_LABELS.get(region, region)
        units: list[float] = []
        total = 0
        for group in groups:
            url = LIST_PAGE.format(game=GAME_ID, goods=GOODS_TYPE, group=group, server="0")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:  # noqa: BLE001 - 单区失败不影响其他区
                errors.append(f"7881 {label} 打开失败：{type(exc).__name__}")
                continue
            hit = _wait_xhr(page, "goods/list")
            if hit is None:
                errors.append(f"7881 {label} 未取到商品接口")
                continue
            body = (_json(hit.get("resp")).get("body") or {})
            rows = body.get("results") or []
            total += int(_num(body.get("records")) or len(rows))
            for row in rows:
                # priceOfUnit 是「元/万基纳」，乘 100 归一到元/百万基纳
                value = _num(row.get("priceOfUnit"))
                if value:
                    units.append(value * 100)
        quote, why = _quote(SOURCE_7881, region, units, total)
        if quote is None:
            # 打开失败或接口没取到时已经记过原因，这里不重复
            if why and not any(label in e for e in errors):
                errors.append(why)
            continue
        quotes.append(quote)
    return quotes, errors


def fetch_pa(page, zones: tuple[str, ...], rate: float) -> tuple[list[Quote], list[str]]:
    """从 PlayerAuctions 取各区的基纳价格（美元，按汇率换成人民币）。"""
    quotes: list[Quote] = []
    errors: list[str] = []
    pairs = [(region, ZONE_PA[region]) for region in zones if region in ZONE_PA]
    if not pairs:
        return quotes, errors

    why = _open_pa(page)
    if why:
        errors.append(f"PlayerAuctions {why}")
        return quotes, errors
    _quiesce(page)

    sids = [sid for _, group in pairs for sid in group]
    try:
        result = page.evaluate(PA_FETCH_JS, sids)
    except Exception as exc:  # noqa: BLE001 - 取数失败不阻塞其他源
        errors.append(f"PlayerAuctions 取数失败：{type(exc).__name__}")
        return quotes, errors
    if not isinstance(result, dict):
        errors.append("PlayerAuctions 返回了意外的结构")
        return quotes, errors

    for region, group in pairs:
        label = ZONE_LABELS.get(region, region)
        units: list[float] = []
        total = 0
        for sid in group:
            item = result.get(str(sid)) or result.get(sid) or {}
            if item.get("error"):
                errors.append(f"PA {label} 取数失败：{str(item['error'])[:60]}")
            total += int(_num(item.get("count")) or 0)
            for raw in item.get("units") or []:
                value = _num(raw)
                if value:
                    units.append(value * rate)
        quote, why = _quote(SOURCE_PA, region, units, total)
        if quote is None:
            # 单枪取数失败时上面已经记过原因，避免同一件事报两遍
            if why and not any(f"{label} " in e for e in errors):
                errors.append(why)
            continue
        quotes.append(quote)
    return quotes, errors


def collect(
    zones: tuple[str, ...] = DEFAULT_ZONES,
    *,
    want_7881: bool = True,
    want_pa: bool = True,
    profile_dir=None,
) -> Snapshot:
    """同步抓取一次。调用方负责放进线程，别在事件循环里直接跑。

    任一源失败都不影响另一源；两个源都拿不到时返回的 quotes 为空，
    由上层决定是沿用旧快照还是如实报错。profile_dir 是浏览器的持久化目录，
    传了才能把 Cloudflare 的放行 cookie 留到下一次抓取。
    """
    ok, reason = available()
    if not ok:
        raise KinahError(reason)

    from playwright.sync_api import sync_playwright

    zones = tuple(z for z in zones if z in ZONE_LABELS) or DEFAULT_ZONES
    started = time.time()
    errors: list[str] = []
    quotes: list[Quote] = []

    rate, rate_error = fetch_rate()
    if rate_error:
        errors.append(rate_error)
    if rate <= 0 and want_pa:
        want_pa = False  # 没有汇率就换不出人民币，美元价没有可比性

    channel = ""
    try:
        with sync_playwright() as pw:
            context, close, channel = _open_browser(pw, profile_dir)
            context.add_init_script(HOOK_XHR)
            try:
                if want_7881:
                    # 各源用各自的页面，避免上一站的导航状态影响下一次取数
                    got, errs = fetch_7881(context.new_page(), zones)
                    quotes += got
                    errors += errs
                if want_pa:
                    got, errs = fetch_pa(context.new_page(), zones, rate)
                    quotes += got
                    errors += errs
            finally:
                try:
                    close()
                except Exception:  # noqa: BLE001 - 关不掉不影响结果
                    pass
    except KinahError:
        raise
    except Exception as exc:  # noqa: BLE001 - 浏览器起不来时如实上报
        raise KinahError(f"抓取失败：{type(exc).__name__}: {exc}") from exc

    return Snapshot(
        fetched_at=time.time(),
        rate=rate,
        quotes=quotes,
        errors=errors,
        elapsed=time.time() - started,
        zones=zones,
        channel=channel,
    )


# ------------------------------------------------------------------ 展示


def zone_count(snapshot: Snapshot | None) -> int:
    """有数据的区数（任一来源有价即算）。"""
    if snapshot is None:
        return 0
    return len({q.region for q in snapshot.quotes})


def text_table(snapshot: Snapshot, now: float | None = None) -> str:
    """纯文本价格表，卡片渲染不可用时使用。价格单位：元 / 百万基纳。"""
    moment = time.localtime(snapshot.fetched_at or (now or time.time()))
    lines = [
        f"AION2 基纳价格（{time.strftime('%Y-%m-%d %H:%M', moment)}）",
        "单位：元 / 百万基纳，取自各区在售挂单的中位价",
        "",
    ]
    rows = snapshot.rows()
    if not rows or not any(r["cn"] or r["us"] for r in rows):
        lines.append("暂时没有取到任何区的价格。")
    else:
        for row in rows:
            cells = []
            if row["cn"] is not None:
                cells.append(f"7881 {row['cn'].median:,.1f}")
            if row["us"] is not None:
                cells.append(f"PA {row['us'].median:,.1f}")
            if not cells:
                continue
            lines.append(f"  {row['label']}　{'　'.join(cells)}")
    if snapshot.rate:
        lines.append("")
        lines.append(f"汇率 1 美元 ≈ {snapshot.rate:.4f} 元人民币")
    if snapshot.errors:
        lines.append("")
        lines.append("本次未取到的部分：")
        for err in snapshot.errors[:6]:
            lines.append(f"  · {err}")
    lines.append("")
    lines.append("价格为第三方交易平台的在售挂单，仅供参考。")
    return "\n".join(lines)
