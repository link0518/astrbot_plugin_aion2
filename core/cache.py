"""两级缓存：进程内 TTL 缓存，以及可落盘的中文译名缓存。

译名几乎不变，落盘后跨重启复用，避免每次查询都去台服取一遍。
"""

import json
import time
from pathlib import Path
from typing import Any


class TTLCache:
    """按 key 缓存任意值，超过存活时间即失效。

    缓存的 key 带着搜索词与角色 id，长期挂在群里几乎不会重复命中，
    因此除了 TTL 还要有容量上限，否则内存只增不减。
    """

    def __init__(self, ttl: int = 600, max_entries: int = 2000):
        self.ttl = max(1, int(ttl))
        self.max_entries = max(1, int(max_entries))
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        hit = self._data.get(key)
        if hit is None:
            return None
        expires, value = hit
        if expires < time.monotonic():
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        self._data[key] = (time.monotonic() + (ttl or self.ttl), value)
        if len(self._data) > self.max_entries:
            self.purge()
            self._evict(len(self._data) - self.max_entries)

    def clear(self) -> None:
        self._data.clear()

    def purge(self) -> None:
        now = time.monotonic()
        for key in [k for k, (exp, _) in self._data.items() if exp < now]:
            self._data.pop(key, None)

    def _evict(self, count: int) -> None:
        """超出容量时按插入顺序丢掉最旧的条目。"""
        for key in list(self._data)[: max(0, count)]:
            self._data.pop(key, None)

    def __len__(self) -> int:
        return len(self._data)


class GlossaryStore:
    """中文译名的持久缓存，键为 "<类别>:<id>"。

    活动订阅、已推记录也复用它，所以同样要有容量上限：
    已推记录的键（每场活动一个）过期后不会有人再去读，只能靠淘汰回收。
    """

    def __init__(self, path: Path, ttl: int = 2592000, max_entries: int = 5000):
        self.path = path
        self.ttl = max(60, int(ttl))
        self.max_entries = max(1, int(max_entries))
        self._data: dict[str, list] = {}
        self._loaded = False
        self._dirty = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            self._data = {k: v for k, v in raw.items() if isinstance(v, list) and len(v) == 2}

    def get(self, key: str) -> Any | None:
        self._load()
        hit = self._data.get(key)
        if hit is None:
            return None
        saved_at, value = hit
        if time.time() - saved_at > self.ttl:
            self._data.pop(key, None)
            self._dirty = True
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._load()
        if key in self._data and self._data[key][1] == value:
            return
        self._data[key] = [time.time(), value]
        self._dirty = True
        if len(self._data) > self.max_entries:
            self._drop_expired()
            self._evict(len(self._data) - self.max_entries)

    def remove(self, key: str) -> None:
        """删掉一个键，用于退订这类取消操作。"""
        self._load()
        if self._data.pop(key, None) is not None:
            self._dirty = True

    def keys(self) -> list[str]:
        """当前有效的全部键，用于遍历订阅这类集合型数据。

        顺带清掉过期条目，否则这些不会再被读到的键会一直留着。
        """
        self._load()
        self._drop_expired()
        return list(self._data)

    def clear(self) -> None:
        """清空全部条目，用于译名失效后重新拉取。"""
        self._load()
        if self._data:
            self._data = {}
            self._dirty = True

    def _drop_expired(self) -> None:
        now = time.time()
        stale = [k for k, (saved_at, _) in self._data.items() if now - saved_at > self.ttl]
        for key in stale:
            self._data.pop(key, None)
        if stale:
            self._dirty = True

    def _evict(self, count: int) -> None:
        """超出容量时按插入顺序丢掉最旧的条目。"""
        for key in list(self._data)[: max(0, count)]:
            self._data.pop(key, None)

    def save(self) -> None:
        if not self._dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=0), encoding="utf-8")
            tmp.replace(self.path)
            self._dirty = False
        except OSError:
            pass

    def __len__(self) -> int:
        self._load()
        return len(self._data)
