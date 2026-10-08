"""活动时刻表：时空裂隙、小游戏等定时事件的本地推算。

时刻表来自客户端数据与社区实测。Global 各分片（美东/美西/欧服/南美服/日服）
是同一物理时刻，只有显示的钟面不同，因此这里统一按东八区计算，
与插件的 region 配置无关。上游接口没有日程类端点，全部为本地推算。

各活动的时刻由 Schedule 描述，默认值写在 DEFAULT_SCHEDULE 里；插件从配置读入后
传给下面的函数，官方临时调整时可以不改代码直接改配置。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta

# 时空裂隙：每 3 小时一次，东八区落在 02/05/08/11/14/17/20/23 点，
# 换成 UTC 就是 0/3/6/9/12/15/18/21 点，各分片同一场。
DEFAULT_RIFT_HOURS = (2, 5, 8, 11, 14, 17, 20, 23)
RIFT_DURATION = 60
RIFT_NOTE = "传送门开启后尽快进场，名额满即关闭。"

# 小游戏（Shugo Festival）：每小时整点开一场，两组游戏按小时交替。
# 游戏名官方没有中文来源，保留英文原文，不臆造译名。
DEFAULT_MINIGAME_MINUTES = (0,)
MINIGAME_DURATION = 10
MINIGAME_SETS = (
    ("Jump Jump", "Not This Tile?", "Wraith Evasion", "Defend Shugo Merchants", "Odyle Flight Frenzy"),
    ("Goldrin's Treasure", "Hidden Lugi", "Mysterious Track", "Up! Up! Up!", "Ppang Ppang"),
)

DEFAULT_INVASION_MINUTE = 30
INVASION_DURATION = 10

DEFAULT_RESET_HOUR = 15
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
class Schedule:
    """活动时刻表。各字段都可在插件配置里改，官方调整时不必改代码。"""

    rift_hours: tuple[int, ...] = DEFAULT_RIFT_HOURS
    minigame_minutes: tuple[int, ...] = DEFAULT_MINIGAME_MINUTES
    invasion_minute: int = DEFAULT_INVASION_MINUTE
    reset_hour: int = DEFAULT_RESET_HOUR

    def normalized(self) -> "Schedule":
        """收敛成可用的取值：小时去重升序、分钟去重升序、越界回退到默认。"""
        hours = tuple(sorted({h for h in self.rift_hours if 0 <= h <= 23}))
        minutes = tuple(sorted({m for m in self.minigame_minutes if 0 <= m <= 59}))
        invasion = self.invasion_minute if 0 <= self.invasion_minute <= 59 else DEFAULT_INVASION_MINUTE
        reset = self.reset_hour if 0 <= self.reset_hour <= 23 else DEFAULT_RESET_HOUR
        return replace(
            self,
            rift_hours=hours or DEFAULT_RIFT_HOURS,
            minigame_minutes=minutes or DEFAULT_MINIGAME_MINUTES,
            invasion_minute=invasion,
            reset_hour=reset,
        )


DEFAULT_SCHEDULE = Schedule()


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


def _schedule(schedule: Schedule | None) -> Schedule:
    return (schedule or DEFAULT_SCHEDULE).normalized()


def minigame_set(start: datetime, schedule: Schedule | None = None) -> tuple[str, ...]:
    """小游戏窗口对应的组别。

    同一小时内若有多个窗口，按窗口顺序取组别；跨小时则按小时数交替，
    所以默认的每小时整点场会一组一小时地轮换。官方若调整轮换，改 MINIGAME_SETS 即可。
    """
    spec = _schedule(schedule)
    minutes = list(spec.minigame_minutes)
    if len(minutes) > 1:
        slot = minutes.index(start.minute) if start.minute in minutes else 0
    else:
        slot = start.hour
    return MINIGAME_SETS[slot % len(MINIGAME_SETS)]


def day_occurrences(
    day: date,
    kinds: tuple[str, ...] | None = None,
    schedule: Schedule | None = None,
) -> list[Occurrence]:
    """某一天的全部场次，按时间排序。"""
    spec = _schedule(schedule)
    out: list[Occurrence] = []
    for hour in spec.rift_hours:
        out.append(Occurrence(KIND_RIFT, datetime.combine(day, time(hour, 0))))
    for hour in range(24):
        for minute in spec.minigame_minutes:
            start = datetime.combine(day, time(hour, minute))
            out.append(Occurrence(KIND_MINIGAME, start, "、".join(minigame_set(start, spec))))
        out.append(Occurrence(KIND_INVASION, datetime.combine(day, time(hour, spec.invasion_minute))))
    out.append(Occurrence(KIND_DAILY, datetime.combine(day, time(spec.reset_hour, 0))))
    if day.weekday() == WEEKLY_RESET_WEEKDAY:
        out.append(Occurrence(KIND_WEEKLY, datetime.combine(day, time(spec.reset_hour, 0))))
    if kinds is not None:
        out = [o for o in out if o.kind in kinds]
    return sorted(out, key=lambda o: o.start)


def occurrences(
    now: datetime,
    days: int = 1,
    kinds: tuple[str, ...] | None = None,
    schedule: Schedule | None = None,
) -> list[Occurrence]:
    """从 now 所在的那天起，往后若干天的场次。"""
    out: list[Occurrence] = []
    for offset in range(max(1, days)):
        day = (now + timedelta(days=offset)).date()
        out.extend(day_occurrences(day, kinds, schedule))
    return sorted(out, key=lambda o: o.start)


def next_occurrence(
    now: datetime,
    kind: str,
    schedule: Schedule | None = None,
    *,
    allow_same: bool = False,
) -> Occurrence | None:
    """下一次某类活动，跨零点时自动看明天。

    allow_same 为真时把正好落在 now 的那一场也算进来，
    用于同一时刻既有裂隙又有小游戏时互相提及。
    """
    for occ in occurrences(now, days=2, kinds=(kind,), schedule=schedule):
        if occ.start > now or (allow_same and occ.start == now):
            return occ
    return None


def pending_reminders(
    now: datetime,
    lead: int = 5,
    kinds: tuple[str, ...] | None = None,
    window: int = 60,
    schedule: Schedule | None = None,
) -> list[Occurrence]:
    """此刻该发出的提醒。

    提醒时刻已到、且还在 window 秒之内才认；轮询间隔短于 window 时，
    每场活动只会命中一次，配合已推记录即可保证不重发。
    """
    out: list[Occurrence] = []
    for occ in occurrences(now, days=2, kinds=kinds, schedule=schedule):
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


def timeline(day: date, now: datetime, schedule: Schedule | None = None) -> list[dict]:
    """一天 24 小时的视图，供卡片画时间轴。"""
    spec = _schedule(schedule)
    cells: list[dict] = []
    for hour in range(24):
        windows = []
        for minute in spec.minigame_minutes:
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
                "rift": hour in spec.rift_hours,
                "rift_state": state_of(rift_start, now, RIFT_DURATION) if hour in spec.rift_hours else "",
                "windows": windows,
            }
        )
    return cells


def reminder_text(
    now: datetime,
    occ: Occurrence,
    lead: int = 5,
    schedule: Schedule | None = None,
) -> str:
    """一条提醒的正文。裂隙与小游戏互相提及，两条链路都带全信息。"""
    other_kind = KIND_MINIGAME if occ.kind == KIND_RIFT else KIND_RIFT
    other = next_occurrence(occ.start, other_kind, schedule, allow_same=True)
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


def schedule_payload(
    tomorrow: bool = False,
    now: datetime | None = None,
    schedule: Schedule | None = None,
) -> dict:
    """时刻表的结构化数据，供配置面板展开成时间轴。"""
    spec = _schedule(schedule)
    moment = now or datetime.now()
    day = (moment + timedelta(days=1)).date() if tomorrow else moment.date()
    rifts = [
        {
            "clock": occ.clock,
            "state": "" if tomorrow else state_of(occ.start, moment, RIFT_DURATION),
        }
        for occ in day_occurrences(day, kinds=(KIND_RIFT,), schedule=spec)
    ]
    upcoming = []
    if not tomorrow:
        for kind in (KIND_RIFT, KIND_MINIGAME):
            occ = next_occurrence(moment, kind, spec)
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
        "timeline": timeline(day, moment, spec),
        "sets": [
            {"label": f"第 {index} 组", "games": list(games)}
            for index, games in enumerate(MINIGAME_SETS, 1)
        ],
        "next": upcoming,
        "gameMinutes": len(spec.minigame_minutes),
        "dailyReset": f"{spec.reset_hour:02d}:00",
        "weeklyReset": f"周三 {spec.reset_hour:02d}:00",
        "invasionMinute": f"{spec.invasion_minute:02d}",
        "note": "时刻表为本地推算，官方临时调整不会同步。",
    }


def text_table(
    now: datetime | None = None,
    *,
    tomorrow: bool = False,
    schedule: Schedule | None = None,
) -> str:
    """纯文本时刻表，卡片渲染不可用时使用。"""
    spec = _schedule(schedule)
    moment = now or datetime.now()
    day = (moment + timedelta(days=1)).date() if tomorrow else moment.date()
    lines = [
        f"{'明日' if tomorrow else '今日'}活动（{day:%m-%d} {WEEKDAYS[day.weekday()]}）",
        "Global 各分片是同一时刻，以下为北京时间",
        "",
        f"时空裂隙（{len(spec.rift_hours)} 场）",
        "  " + "　".join(f"{hour:02d}:00" for hour in spec.rift_hours),
    ]
    rift = next_occurrence(moment, KIND_RIFT, spec)
    if rift is not None and not tomorrow:
        lines.append(f"  下一次 {rift.clock}（{countdown(moment, rift.start)}）")
    lines += ["", f"小游戏（每小时 {len(spec.minigame_minutes)} 场）"]
    for minute, games in zip(spec.minigame_minutes, MINIGAME_SETS):
        lines.append(f"  :{minute:02d} 组　{'、'.join(games)}")
    game = next_occurrence(moment, KIND_MINIGAME, spec)
    if game is not None and not tomorrow:
        lines.append(f"  下一场 {game.clock}（{countdown(moment, game.start)}）")
    lines += [
        "",
        f"次元入侵　每小时 :{spec.invasion_minute:02d}",
        f"重置　每日 {spec.reset_hour:02d}:00，每周三 {spec.reset_hour:02d}:00",
        "",
        "时刻表为本地推算，官方临时调整不会同步。",
    ]
    return "\n".join(lines)
