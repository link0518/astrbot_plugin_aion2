"""活动时刻表：时空裂隙、小游戏等定时事件的本地推算。

时刻表来自客户端数据与社区实测。Global 各分片（美东/美西/欧服/南美服/日服）
是同一物理时刻，只有显示的钟面不同，因此这里统一按东八区计算，
与插件的 region 配置无关。上游接口没有日程类端点，全部为本地推算。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

# 时空裂隙：每 3 小时一次，东八区落在 02/05/08/11/14/17/20/23 点，
# 换成 UTC 就是 0/3/6/9/12/15/18/21 点，各分片同一场。
RIFT_HOURS = (2, 5, 8, 11, 14, 17, 20, 23)
RIFT_DURATION = 60
RIFT_NOTE = "传送门开启后尽快进场，名额满即关闭。"

# 小游戏（Shugo Festival）：每小时 :15 与 :45 两个入场窗口，两组游戏交替。
# 游戏名官方没有中文来源，保留英文原文，不臆造译名。
MINIGAME_MINUTES = (15, 45)
MINIGAME_DURATION = 10
MINIGAME_SETS = (
    ("Jump Jump", "Not This Tile?", "Wraith Evasion", "Defend Shugo Merchants", "Odyle Flight Frenzy"),
    ("Goldrin's Treasure", "Hidden Lugi", "Mysterious Track", "Up! Up! Up!", "Ppang Ppang"),
)

INVASION_MINUTE = 30
INVASION_DURATION = 10

RESET_HOUR = 15
WEEKLY_RESET_WEEKDAY = 2  # 0 是周一，2 是周三

KIND_RIFT = "rift"
KIND_MINIGAME = "minigame"
KIND_INVASION = "invasion"
KIND_DAILY = "reset"
KIND_WEEKLY = "reset_weekly"

KIND_NAMES = {
    KIND_RIFT: "时空裂隙",
    KIND_MINIGAME: "小游戏",
    KIND_INVASION: "次元入侵",
    KIND_DAILY: "每日重置",
    KIND_WEEKLY: "每周重置",
}

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 卡片着色用的状态说明
STATE_LABELS = {"past": "已过", "live": "进行中", "soon": "即将", "future": ""}


@dataclass(frozen=True)
class Occurrence:
    """一场定时活动。"""

    kind: str
    start: datetime
    detail: str = ""

    @property
    def name(self) -> str:
        return KIND_NAMES[self.kind]

    @property
    def clock(self) -> str:
        return f"{self.start:%H:%M}"

    @property
    def key(self) -> str:
        """去重标识：事件类型加开始时刻，跨重启仍然有效。"""
        return f"{self.kind}@{self.start:%Y-%m-%dT%H:%M}"


def minigame_set(start: datetime) -> tuple[str, ...]:
    """小游戏窗口对应的组别。

    实测每个整小时的两个窗口都开，:15 固定第一组、:45 固定第二组，
    所以按分钟取组别即可。若官方调整轮换，改这里一处。
    """
    return MINIGAME_SETS[MINIGAME_MINUTES.index(start.minute) % len(MINIGAME_SETS)]


def day_occurrences(day: date, kinds: tuple[str, ...] | None = None) -> list[Occurrence]:
    """某一天的全部场次，按时间排序。"""
    out: list[Occurrence] = []
    for hour in RIFT_HOURS:
        out.append(Occurrence(KIND_RIFT, datetime.combine(day, time(hour, 0))))
    for hour in range(24):
        for minute in MINIGAME_MINUTES:
            start = datetime.combine(day, time(hour, minute))
            out.append(Occurrence(KIND_MINIGAME, start, "、".join(minigame_set(start))))
        out.append(Occurrence(KIND_INVASION, datetime.combine(day, time(hour, INVASION_MINUTE))))
    out.append(Occurrence(KIND_DAILY, datetime.combine(day, time(RESET_HOUR, 0))))
    if day.weekday() == WEEKLY_RESET_WEEKDAY:
        out.append(Occurrence(KIND_WEEKLY, datetime.combine(day, time(RESET_HOUR, 0))))
    if kinds is not None:
        out = [o for o in out if o.kind in kinds]
    return sorted(out, key=lambda o: o.start)


def occurrences(now: datetime, days: int = 1, kinds: tuple[str, ...] | None = None) -> list[Occurrence]:
    """从 now 所在的那天起，往后若干天的场次。"""
    out: list[Occurrence] = []
    for offset in range(max(1, days)):
        day = (now + timedelta(days=offset)).date()
        out.extend(day_occurrences(day, kinds))
    return sorted(out, key=lambda o: o.start)


def next_occurrence(now: datetime, kind: str) -> Occurrence | None:
    """下一次某类活动，跨零点时自动看明天。"""
    for occ in occurrences(now, days=2, kinds=(kind,)):
        if occ.start > now:
            return occ
    return None


def pending_reminders(
    now: datetime,
    lead: int = 5,
    kinds: tuple[str, ...] | None = None,
    window: int = 60,
) -> list[Occurrence]:
    """此刻该发出的提醒。

    提醒时刻已到、且还在 window 秒之内才认；轮询间隔短于 window 时，
    每场活动只会命中一次，配合已推记录即可保证不重发。
    """
    out: list[Occurrence] = []
    for occ in occurrences(now, days=2, kinds=kinds):
        offset = (occ.start - now).total_seconds() - lead * 60
        if 0 <= offset < window:
            out.append(occ)
    return out


def countdown(now: datetime, start: datetime) -> str:
    """把剩余时间说成人话。"""
    minutes = int((start - now).total_seconds() // 60)
    if minutes <= 0:
        return "即将开始"
    if minutes < 60:
        return f"{minutes} 分钟后"
    hours, rest = divmod(minutes, 60)
    return f"{hours} 小时{rest} 分钟后" if rest else f"{hours} 小时后"


def state_of(start: datetime, now: datetime, minutes: int) -> str:
    """past / live / soon / future，用于卡片着色。"""
    if now >= start + timedelta(minutes=minutes):
        return "past"
    if now >= start:
        return "live"
    if start - now <= timedelta(minutes=60):
        return "soon"
    return "future"


def timeline(day: date, now: datetime) -> list[dict]:
    """一天 24 小时的视图，供卡片画时间轴。"""
    cells: list[dict] = []
    for hour in range(24):
        windows = []
        for minute in MINIGAME_MINUTES:
            start = datetime.combine(day, time(hour, minute))
            windows.append(
                {
                    "time": f"{minute:02d}",
                    "left": f"{round(minute * 100 / 60)}%",
                    "state": state_of(start, now, MINIGAME_DURATION),
                }
            )
        rift_start = datetime.combine(day, time(hour, 0))
        cells.append(
            {
                "hour": f"{hour:02d}",
                "rift": hour in RIFT_HOURS,
                "rift_state": state_of(rift_start, now, RIFT_DURATION) if hour in RIFT_HOURS else "",
                "windows": windows,
            }
        )
    return cells


def reminder_text(now: datetime, occ: Occurrence, lead: int = 5) -> str:
    """一条提醒的正文。裂隙与小游戏互相提及，两条链路都带全信息。"""
    other_kind = KIND_MINIGAME if occ.kind == KIND_RIFT else KIND_RIFT
    other = next_occurrence(occ.start, other_kind)
    lines = [f"【{occ.name}】{occ.clock} 开始，还有 {lead} 分钟"]
    if occ.kind == KIND_RIFT:
        lines.append(RIFT_NOTE)
        if other is not None:
            lines.append(f"接着的小游戏 {other.clock}：{other.detail}")
    else:
        lines.append(f"本场：{occ.detail}")
        if other is not None:
            lines.append(f"下一次裂隙 {other.clock}")
    return "\n".join(lines)


def schedule_payload(tomorrow: bool = False, now: datetime | None = None) -> dict:
    """时刻表的结构化数据，供配置面板展开成时间轴。"""
    moment = now or datetime.now()
    day = (moment + timedelta(days=1)).date() if tomorrow else moment.date()
    rifts = [
        {
            "clock": occ.clock,
            "state": "" if tomorrow else state_of(occ.start, moment, RIFT_DURATION),
        }
        for occ in day_occurrences(day, kinds=(KIND_RIFT,))
    ]
    upcoming = []
    if not tomorrow:
        for kind in (KIND_RIFT, KIND_MINIGAME):
            occ = next_occurrence(moment, kind)
            if occ is None:
                continue
            upcoming.append(
                {
                    "kind": kind,
                    "name": occ.name,
                    "clock": occ.clock,
                    "detail": occ.detail,
                    "countdown": countdown(moment, occ.start),
                }
            )
    return {
        "label": "明日" if tomorrow else "今日",
        "day": f"{day:%Y-%m-%d}",
        "weekday": WEEKDAYS[day.weekday()],
        "isWeeklyResetDay": day.weekday() == WEEKLY_RESET_WEEKDAY,
        "rifts": rifts,
        "timeline": timeline(day, moment),
        "sets": [
            {"label": f"第 {index} 组", "games": list(games)}
            for index, games in enumerate(MINIGAME_SETS, 1)
        ],
        "next": upcoming,
        "dailyReset": f"{RESET_HOUR:02d}:00",
        "weeklyReset": f"周三 {RESET_HOUR:02d}:00",
        "invasionMinute": f"{INVASION_MINUTE:02d}",
        "note": "时刻表为本地推算，官方临时调整不会同步。",
    }


def text_table(now: datetime | None = None, *, tomorrow: bool = False) -> str:
    """纯文本时刻表，卡片渲染不可用时使用。"""
    moment = now or datetime.now()
    day = (moment + timedelta(days=1)).date() if tomorrow else moment.date()
    lines = [
        f"{'明日' if tomorrow else '今日'}活动（{day:%m-%d} {WEEKDAYS[day.weekday()]}）",
        "Global 各分片是同一时刻，以下为北京时间",
        "",
        f"时空裂隙（{len(RIFT_HOURS)} 场，每 3 小时）",
        "  " + "　".join(f"{hour:02d}:00" for hour in RIFT_HOURS),
    ]
    rift = next_occurrence(moment, KIND_RIFT)
    if rift is not None and not tomorrow:
        lines.append(f"  下一次 {rift.clock}（{countdown(moment, rift.start)}）")
    lines += ["", "小游戏（每半小时一场）"]
    for minute, games in zip(MINIGAME_MINUTES, MINIGAME_SETS):
        lines.append(f"  :{minute:02d} 组　{'、'.join(games)}")
    game = next_occurrence(moment, KIND_MINIGAME)
    if game is not None and not tomorrow:
        lines.append(f"  下一场 {game.clock}（{countdown(moment, game.start)}）")
    lines += [
        "",
        f"次元入侵　每小时 :{INVASION_MINUTE:02d}",
        f"重置　每日 {RESET_HOUR:02d}:00，每周三 {RESET_HOUR:02d}:00",
        "",
        "时刻表为本地推算，官方临时调整不会同步。",
    ]
    return "\n".join(lines)
