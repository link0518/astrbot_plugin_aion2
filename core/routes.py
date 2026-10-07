"""各区域的上游地址与路由规则。

上游站点的路径规则不统一：gameinfo 与 gameconst 必须带语言前缀，
而角色、排行等接口带了反而会被站点拒绝，因此两条路径分开构造。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    key: str
    label: str
    site: str  # 站点域名，角色与 gameinfo 都挂在这里
    shard: str  # 上游的分片参数，非国际服为空
    default_locale: str
    community: str  # 社区板块独立域名
    board_suffix: str  # 板块别名后缀，上游按语言拼在别名后面
    dict: str  # 道具字典前缀，空串表示该区没有字典
    search: str  # 角色搜索接口
    prefix: str = ""  # 站点下的接口前缀，台服是 /aion2，国际服没有


REGIONS: dict[str, Region] = {
    "nae": Region(
        key="nae",
        label="美东",
        site="https://aion2.plaync.com",
        shard="nae",
        default_locale="en-US",
        community="https://api-global-community.plaync.com/aion2_global",
        board_suffix="_en",
        dict="",
        search="https://api-search.plaync.com/aion2global/search/v2",
    ),
    "naw": Region(
        key="naw",
        label="美西",
        site="https://aion2.plaync.com",
        shard="naw",
        default_locale="en-US",
        community="https://api-global-community.plaync.com/aion2_global",
        board_suffix="_en",
        dict="",
        search="https://api-search.plaync.com/aion2global/search/v2",
    ),
    "eu": Region(
        key="eu",
        label="欧服",
        site="https://aion2.plaync.com",
        shard="eu",
        default_locale="en-US",
        community="https://api-global-community.plaync.com/aion2_global",
        board_suffix="_en",
        dict="",
        search="https://api-search.plaync.com/aion2global/search/v2",
    ),
    "sa": Region(
        key="sa",
        label="南美服",
        site="https://aion2.plaync.com",
        shard="la",
        default_locale="en-US",
        community="https://api-global-community.plaync.com/aion2_global",
        board_suffix="_en",
        dict="",
        search="https://api-search.plaync.com/aion2global/search/v2",
    ),
    "asia": Region(
        key="asia",
        label="日服",
        site="https://aion2.plaync.com",
        shard="as",
        default_locale="en-US",
        community="https://api-global-community.plaync.com/aion2_global",
        board_suffix="_en",
        dict="",
        search="https://api-search.plaync.com/aion2global/search/v2",
    ),
    "tw": Region(
        key="tw",
        label="台服",
        site="https://tw.ncsoft.com",
        shard="",
        default_locale="zh-TW",
        community="https://api-tw-community.ncsoft.com/aion2_tw",
        board_suffix="_zh",
        dict="/aion2_tw/v2.0",
        search="https://tw.ncsoft.com/aion2/api/search",
        prefix="/aion2",
    ),
}

# 台服是道具与技能译名的来源区，即使主区域是国际服也要能访问它
GLOSSARY_REGION = "tw"

# 游戏数据接口，按语言分路径
P_SERVERS = "/api/gameinfo/servers"
P_CLASSES = "/api/gameinfo/classes"
P_PC_DATA = "/api/gameinfo/pcdata"
P_ITEM = "/api/gameconst/item"

# 角色与排行接口，不带语言前缀
P_CHARACTER = "/api/character/info"
P_EQUIPMENT = "/api/character/equipment"
P_EQUIPPED_ITEM = "/api/character/equipment/item"
P_DAEVANION = "/api/character/daevanion/detail"
P_RANKING = "/api/ranking/list"

# 角色搜索的路径，前缀已在 Region.search 里
P_SEARCH_CHARACTER = "/character"

# 社区板块
P_BOARD_ARTICLES = "/article/search/moreArticle"

# 板块别名，上游按区域语言拼后缀
BOARDS: dict[str, str] = {
    "notice": "公告",
    "update": "更新公告",
    "cm_story": "开发日志",
    "free": "综合讨论",
    "member_recruit": "军团招募",
    "tip": "攻略",
    "image": "图文",
}

# 排行榜类型，取值来自上游官网页面的配置表
RANKINGS: dict[str, int] = {
    "abyss": 1,
    "nightmare": 3,
    "transcendence": 4,
    "solitude": 5,
    "cooperation": 6,
    "ascension": 21,
}

RANKING_LABELS: dict[str, str] = {
    "abyss": "深渊",
    "nightmare": "噩梦",
    "transcendence": "超越",
    "solitude": "孤独竞技场",
    "cooperation": "协力竞技场",
    "ascension": "升天试炼",
}


def region_of(key: str) -> Region:
    region = REGIONS.get((key or "").strip().lower())
    if region is None:
        raise KeyError(key)
    return region


class Routes:
    """绑定区域后的地址构造器。取数语言随区域确定，不单独配置。"""

    def __init__(self, region: Region):
        self.region = region
        self.locale = region.default_locale
        self._loc = f"/{self.locale.lower()}" if region.shard else ""
        self._shard = {"region": region.shard} if region.shard else {}

    def game(self, path: str) -> str:
        """游戏数据接口，国际服必须带语言路径。"""
        return f"{self.region.site}{self.region.prefix}{self._loc}{path}"

    def api(self, path: str) -> str:
        """角色与排行接口，带语言路径会被站点拒绝。"""
        return f"{self.region.site}{self.region.prefix}{path}"

    def search(self, path: str) -> str:
        return f"{self.region.search}{path}"

    def lexicon(self, path: str) -> str:
        """道具字典接口，只有台服提供。"""
        return f"{self.region.site}{self.region.dict}{path}"

    def board(self, alias: str, path: str = "") -> str:
        name = f"{alias}{self.region.board_suffix}"
        return f"{self.region.community}/board/{name}{path}"
    def site_params(self) -> dict[str, str]:
        """站点通用参数：语言，国际服另加分片。"""
        params = {"lang": self.locale}
        params.update(self._shard)
        return params

    def item_params(self, item_id: int, enchant_level: int = 0) -> dict[str, str]:
        """道具接口的参数，刻意不带分片。

        这个接口是全局道具库，各分片数据一致；而 asia 分片带上 region 参数
        会返回空的 200 响应（不是报错，很容易看漏），不带才正常。
        """
        return {
            "lang": self.locale,
            "id": str(item_id),
            "enchantLevel": str(enchant_level),
        }
