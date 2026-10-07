"""两级缓存：进程内 TTL 缓存，以及可落盘的中文译名缓存。

译名几乎不变，落盘后跨重启复用，避免每次查询都去台服取一遍。
"""

import json
import time
from pathlib import Path
from typing import Any


class TTLCache:
    """按 key 缓存任意值，超过存活时间即失效。"""

    def __init__(self, ttl: int = 600):
        self.ttl = max(1, int(ttl))
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

    def clear(self) -> None:
        self._data.clear()

    def purge(self) -> None:
        now = time.monotonic()
        for key in [k for k, (exp, _) in self._data.items() if exp < now]:
            self._data.pop(key, None)

    def __len__(self) -> int:
        return len(self._data)


class GlossaryStore:
    """中文译名的持久缓存，键为 "<类别>:<id>"。"""

    def __init__(self, path: Path, ttl: int = 2592000):
        self.path = path
        self.ttl = max(60, int(ttl))
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
        if self._data.get(key) and self._data[key][1] == value:
            return
        self._data[key] = [time.time(), value]
        self._dirty = True

    def remove(self, key: str) -> None:
        """删掉一个键，用于退订这类取消操作。"""
        self._load()
        if self._data.pop(key, None) is not None:
            self._dirty = True

    def keys(self) -> list[str]:
        """当前存下来的全部键，用于遍历订阅这类集合型数据。"""
        self._load()
        return list(self._data)

    def clear(self) -> None:
        """清空全部条目，用于译名失效后重新拉取。"""
        self._load()
        if self._data:
            self._data = {}
            self._dirty = True

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
