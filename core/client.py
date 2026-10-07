"""上游 HTTP 客户端：请求节流、结果缓存、错误语义化。

角色与排行接口走不带语言前缀的路径，游戏数据接口走带前缀的路径，
两条路径由 Routes 区分，这里只负责取数与解析。
"""

import asyncio
import time
import urllib.parse
from typing import Any

import httpx

from . import routes as R
from .cache import GlossaryStore, TTLCache
from .errors import (
    Aion2Error,
    FeatureUnavailable,
    NetworkError,
    NoSeason,
    NotFound,
    UpstreamError,
    from_status,
)
from .models import (
    Character,
    CharacterRef,
    Equipment,
    GameClass,
    Item,
    Page,
    Post,
    PostBody,
    Server,
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# 上游以 302 把不可用的路由甩到错误页，按未找到处理
REDIRECT_STATUS = (301, 302, 303, 307, 308)


class RateLimiter:
    """把请求按最小间隔排队，避免触发上游限流。"""

    def __init__(self, rate: float):
        self._interval = 1.0 / max(float(rate), 0.1)
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next_at > now:
                await asyncio.sleep(self._next_at - now)
                now = time.monotonic()
            self._next_at = now + self._interval


def decode_character_id(raw: str) -> str:
    """搜索结果里的角色 id 带百分号转义，直接回传会被上游当成另一个 id。"""
    if not raw or "%" not in raw:
        return raw
    return urllib.parse.unquote(raw)


class Aion2Client:
    def __init__(
        self,
        *,
        region_key: str = "asia",
        rate_limit: float = 5.0,
        timeout: int = 15,
        cache_ttl: int = 600,
        glossary: GlossaryStore | None = None,
        logger=None,
    ):
        self.region = R.region_of(region_key)
        # 取数语言由区域决定（国际服英文、台服繁中），中文一律本地映射
        self.routes = R.Routes(self.region)
        self.logger = logger
        self._cache = TTLCache(cache_ttl)
        self._limiter = RateLimiter(rate_limit)
        self._glossary = glossary
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(float(timeout)),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"},
            follow_redirects=False,
        )
        self._glossary_routes: R.Routes | None = None

    async def close(self) -> None:
        await self._http.aclose()
        if self._glossary is not None:
            self._glossary.save()

    def cache_stats(self) -> dict[str, int]:
        """结果缓存的存活条数与容量上限，供面板展示。"""
        return {"entries": len(self._cache), "ttl": self._cache.ttl}

    def clear_cache(self) -> int:
        """清空结果缓存，返回清掉的条数。"""
        count = len(self._cache)
        self._cache.clear()
        return count

    # ---------------------------------------------------------------- 取数

    async def _get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        cache_key: str = "",
        ttl: int | None = None,
    ) -> Any:
        if cache_key:
            hit = self._cache.get(cache_key)
            if hit is not None:
                return hit

        await self._limiter.wait()
        try:
            response = await self._http.get(url, params=params)
        except httpx.HTTPError as exc:
            raise NetworkError(str(exc)) from exc

        if response.status_code in REDIRECT_STATUS or response.status_code >= 400:
            raise from_status(response.status_code, f"{url} -> {response.status_code}")

        # 上游对不支持的参数组合会返回空的 200，而不是报错，这里单独识别出来
        if not response.content:
            raise FeatureUnavailable(f"{url} 返回空响应")

        try:
            data = response.json()
        except ValueError as exc:
            raise UpstreamError(f"{url} 返回的不是 JSON") from exc

        if cache_key:
            self._cache.set(cache_key, data, ttl)
        return data

    # ------------------------------------------------------------ 游戏数据

    async def servers(self) -> list[Server]:
        raw = await self._get(
            self.routes.game(R.P_SERVERS),
            self.routes.site_params(),
            cache_key=f"{self.region.key}:servers",
        )
        return [Server.from_raw(s) for s in (raw.get("serverList") or [])]

    async def classes(self) -> list[GameClass]:
        raw = await self._get(
            self.routes.game(R.P_CLASSES),
            self.routes.site_params(),
            cache_key=f"{self.region.key}:classes",
        )
        return [GameClass.from_raw(c) for c in (raw.get("classList") or [])]

    async def item(self, item_id: int) -> Item:
        if item_id <= 0:
            raise NotFound("道具 id 无效")
        raw = await self._get(
            self.routes.game(R.P_ITEM),
            self.routes.item_params(item_id),
            cache_key=f"{self.region.key}:item:{item_id}",
        )
        item = Item.from_raw(raw)
        if item.id == 0 or not item.name:
            raise NotFound(f"道具 {item_id}")
        return item

    # -------------------------------------------------------------- 角色

    async def search_characters(
        self,
        keyword: str,
        *,
        race_id: int = 0,
        server_id: int = 0,
        page: int = 1,
        size: int = 40,
    ) -> Page:
        if not keyword.strip():
            raise Aion2Error("请输入角色名关键词")
        params: dict[str, Any] = {
            "keyword": keyword.strip(),
            "page": max(page, 1),
            "size": max(1, min(size, 100)),
        }
        # 不传种族等于两族都搜；传 0 会被上游当成没有匹配
        if race_id > 0:
            params["race"] = race_id
        if server_id > 0:
            params["serverId"] = server_id
        # 国际服的搜索接口要分片与语言，且都在查询串里
        if self.region.shard:
            params["region"] = self.region.shard
            params["localeInfo"] = self.routes.locale

        raw = await self._get(
            self.routes.search(R.P_SEARCH_CHARACTER),
            params,
            cache_key=f"{self.region.key}:search:{keyword.strip()}:{race_id}:{server_id}:{page}:{size}",
        )
        return Page.from_raw(raw)

    def _character_params(self, ref: CharacterRef) -> dict[str, Any]:
        if ref.server_id <= 0 or not ref.character_id:
            raise Aion2Error("缺少角色标识，请先搜索角色")
        params = self.routes.site_params()
        params["serverId"] = ref.server_id
        params["characterId"] = decode_character_id(ref.character_id)
        return params

    async def character(self, ref: CharacterRef) -> Character:
        raw = await self._get(
            self.routes.api(R.P_CHARACTER),
            self._character_params(ref),
            cache_key=f"{self.region.key}:ch:{ref.server_id}:{ref.character_id}",
        )
        if not (raw.get("profile") or {}).get("characterName"):
            raise NotFound("角色不存在")
        return Character.from_raw(raw, ref)

    async def equipment(self, ref: CharacterRef) -> Equipment:
        raw = await self._get(
            self.routes.api(R.P_EQUIPMENT),
            self._character_params(ref),
            cache_key=f"{self.region.key}:eq:{ref.server_id}:{ref.character_id}",
        )
        gear = (raw.get("equipment") or {}).get("equipmentList")
        skills = (raw.get("skill") or {}).get("skillList")
        if gear is None and skills is None:
            raise NotFound("该角色没有装备数据")
        return Equipment.from_raw(raw)

    # -------------------------------------------------------------- 排行

    async def ranking(
        self,
        contents: str,
        server_id: int,
        *,
        class_id: int = 0,
        name: str = "",
    ) -> list[dict[str, Any]]:
        contents_type = R.RANKINGS.get(contents)
        if contents_type is None:
            raise Aion2Error(f"未知的排行榜类型 {contents}")
        params: dict[str, Any] = {
            **self.routes.site_params(),
            "rankingContentsType": contents_type,
            "rankingType": class_id,
            "serverId": server_id,
        }
        if name:
            params["searchCharacterName"] = name

        raw = await self._get(
            self.routes.api(R.P_RANKING),
            params,
            cache_key=f"{self.region.key}:rk:{contents}:{server_id}:{class_id}:{name}",
            ttl=300,
        )
        # 官方关停榜单时 season 为空，榜单列表也是空
        if not raw.get("season"):
            raise NoSeason(f"{R.RANKING_LABELS.get(contents, contents)}榜")
        return raw.get("rankingList") or []

    # -------------------------------------------------------------- 公告

    async def posts(self, board: str, limit: int = 10) -> list[Post]:
        if board not in R.BOARDS:
            raise Aion2Error(f"未知的板块 {board}")
        raw = await self._get(
            self.routes.board(board, R.P_BOARD_ARTICLES),
            {"moreSize": 100, "moreDirection": "BEFORE", "previousArticleId": 0},
            cache_key=f"{self.region.key}:posts:{board}",
            ttl=300,
        )
        rows = raw.get("contentList") or []
        return [Post.from_raw(row, board) for row in rows[: max(0, limit)]]

    async def post(self, board: str, post_id: str) -> PostBody:
        raw = await self._get(
            self.routes.board(board, f"/article/{post_id}"),
            cache_key=f"{self.region.key}:post:{board}:{post_id}",
            ttl=600,
        )
        body = PostBody.from_raw(raw)
        if not body.title:
            raise NotFound(f"帖子 {post_id}")
        return body

    # --------------------------------------------------- 道具译名（台服反查）

    def _glossary_route(self) -> R.Routes:
        if self._glossary_routes is None:
            self._glossary_routes = R.Routes(R.region_of(R.GLOSSARY_REGION))
        return self._glossary_routes

    async def item_zh(self, item_id: int) -> dict[str, Any]:
        """取台服同 id 道具的中文信息：名称、品类、种族、外观与主属性译名。

        两区道具 id 通用，但数值随区域版本不同，因此只取名称不取数值。
        """
        if item_id <= 0 or self._glossary is None:
            return {}
        key = f"item:{item_id}"
        cached = self._glossary.get(key)
        if isinstance(cached, dict):
            return cached

        routes = self._glossary_route()
        try:
            raw = await self._get(
                routes.game(R.P_ITEM),
                routes.item_params(item_id),
                cache_key=f"glossary:item:{item_id}",
            )
        except Aion2Error:
            return {}

        if not isinstance(raw, dict) or int(raw.get("id") or 0) != item_id:
            return {}
        info = {
            "name": raw.get("name") or "",
            "category": raw.get("categoryName") or "",
            "race": raw.get("raceName") or "",
            "costumes": [str(c) for c in (raw.get("costumes") or [])],
            "stats": {
                str(s.get("id")): str(s.get("name"))
                for s in (raw.get("mainStats") or [])
                if isinstance(s, dict) and s.get("id") and s.get("name")
            },
        }
        self._glossary.set(key, info)
        return info

    async def item_names_zh(self, item_ids: list[int], concurrency: int = 8) -> dict[int, str]:
        """批量取中文名。请求仍按限流器排队，只是并发等待以缩短总耗时。"""
        ids = [i for i in dict.fromkeys(item_ids) if i > 0]
        if not ids:
            return {}
        gate = asyncio.Semaphore(max(1, concurrency))

        async def one(item_id: int) -> tuple[int, str]:
            async with gate:
                info = await self.item_zh(item_id)
                return item_id, str(info.get("name") or "")

        results = await asyncio.gather(*(one(i) for i in ids))
        return {item_id: name for item_id, name in results if name}
