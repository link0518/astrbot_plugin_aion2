"""配置面板（AstrBot WebUI 的 Plugin Pages）的后端接口。

面板跑在受限 iframe 里，所有读写都经由这里转发；请求参数一律当作不可信输入，
重新校验后再落到配置或存储上。这里只做 HTTP 边界，业务动作交给插件实例。
"""

from __future__ import annotations

from datetime import datetime

from astrbot.api import logger

try:
    from astrbot.api.web import error_response, json_response, request
except ImportError:
    # 早期版本没有插件页面接口，面板用不了，但查询功能不受影响
    error_response = json_response = request = None

from .core import events, settings

# 会话标识的形状是 <平台>:<消息类型>:<会话号>
SESSION_KINDS = {
    "GroupMessage": "群",
    "FriendMessage": "私聊",
    "GuildMessage": "频道",
    "DMs": "私聊",
}


def describe_target(target: str) -> str:
    """把会话标识说成人话，面板上直接看得出是哪个群。"""
    parts = str(target).split(":")
    if len(parts) < 3:
        return str(target)
    kind = SESSION_KINDS.get(parts[1], parts[1])
    return f"{kind} {' '.join(parts[2:])}"


class ConsoleAPI:
    """把插件能力暴露成面板可调用的接口。"""

    def __init__(self, plugin):
        self.plugin = plugin
        self.schema = settings.load_schema(plugin.plugin_dir)
        self.defaults = settings.defaults_of(self.schema)

    def register(self, name: str) -> None:
        """把各接口挂到 WebUI，路由需要带插件名前缀。"""
        register_api = getattr(self.plugin.context, "register_web_api", None)
        if json_response is None or not callable(register_api):
            logger.info("当前 AstrBot 版本不支持插件页面接口，配置面板不可用")
            return
        for path, handler, methods, desc in (
            ("overview", self.overview, ["GET"], "面板初始数据"),
            ("state", self.state, ["GET"], "运行状态"),
            ("config", self.save_config, ["POST"], "保存配置"),
            ("reset", self.reset_config, ["POST"], "恢复默认值"),
            ("cache/clear", self.clear_cache, ["POST"], "清空缓存"),
            ("schedule", self.schedule, ["GET"], "活动时刻表"),
            ("selftest", self.selftest, ["POST"], "上游连通性自检"),
            ("subscriptions/remove", self.remove_subscription, ["POST"], "移除订阅"),
            ("subscriptions/test", self.test_subscription, ["POST"], "测试推送"),
        ):
            self.plugin.context.register_web_api(
                f"/{name}/console/{path}", handler, methods, desc
            )

    # ---------------------------------------------------------------- 读取

    async def overview(self):
        """面板打开时的一次性数据。"""
        return json_response(
            {
                "groups": self._groups(),
                "state": self.plugin.status(),
                "subscriptions": self._subscriptions(),
                "schedule": self._schedule(False),
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    async def state(self):
        """运行状态，面板定时刷新用。"""
        return json_response({"state": self.plugin.status()})

    async def schedule(self):
        """活动时刻表，day 取 today 或 tomorrow。"""
        day = (request.query.get("day") or "today").strip().lower()
        if day not in ("today", "tomorrow"):
            return error_response("day 只能是 today 或 tomorrow", status_code=400)
        return json_response(self._schedule(day == "tomorrow"))

    def _groups(self) -> list[dict]:
        return settings.describe(self.schema, self.plugin.config, self.defaults)

    def _subscriptions(self) -> list[dict]:
        rows = []
        for row in self.plugin.subscription_list():
            rows.append(
                {
                    "target": row["target"],
                    "label": describe_target(row["target"]),
                    "since": row["since"],
                }
            )
        return rows

    def _schedule(self, tomorrow: bool) -> dict:
        return events.schedule_payload(tomorrow)

    # ---------------------------------------------------------------- 写入

    async def save_config(self):
        """保存面板改动的配置项，并让新值立即生效。"""
        payload = await request.json(default={})
        values = payload.get("values") if isinstance(payload, dict) else None
        if not isinstance(values, dict):
            return error_response("请求体需要包含 values 对象", status_code=400)
        clean, errors = settings.validate_payload(values, self.schema)
        if errors:
            return error_response("；".join(errors), status_code=400)
        if not clean:
            return error_response("没有要保存的改动", status_code=400)

        # 只记录真正变化的键，避免拿旧值触发无谓的客户端重建
        changed = {k: v for k, v in clean.items() if self.plugin.config.get(k) != v}
        await self._persist(clean)
        await self.plugin.apply_config(changed)
        return json_response(
            {
                "saved": clean,
                "changed": list(changed),
                "groups": self._groups(),
                "state": self.plugin.status(),
            }
        )

    async def reset_config(self):
        """把选定配置项（或全部）恢复为 schema 里的默认值。"""
        payload = await request.json(default={})
        keys = payload.get("keys") if isinstance(payload, dict) else None
        editable = settings.editable_keys(self.schema)
        if keys is None:
            keys = editable
        if not isinstance(keys, list):
            return error_response("keys 需要是数组", status_code=400)
        unknown = [str(k) for k in keys if str(k) not in editable]
        if unknown:
            return error_response(f"未知配置项：{'、'.join(unknown)}", status_code=400)
        if not keys:
            return error_response("没有要恢复的配置项", status_code=400)

        target = {str(k): self.defaults.get(str(k)) for k in keys}
        changed = {k: v for k, v in target.items() if self.plugin.config.get(k) != v}
        await self._persist(target)
        await self.plugin.apply_config(changed)
        return json_response(
            {
                "reset": sorted(target),
                "changed": list(changed),
                "groups": self._groups(),
                "state": self.plugin.status(),
            }
        )

    async def _persist(self, values: dict) -> None:
        """写入配置。新版用异步落盘，旧版退回到同步接口。"""
        saver = getattr(self.plugin.config, "save_config_async", None)
        if callable(saver):
            await saver(values)
            return
        self.plugin.config.save_config(values)

    async def clear_cache(self):
        """清空接口结果缓存与译名缓存。"""
        payload = await request.json(default={})
        scope = str((payload or {}).get("scope") or "all")
        if scope not in ("all", "result", "glossary"):
            return error_response("scope 只能是 all、result 或 glossary", status_code=400)

        dropped = 0
        client = self.plugin.peek_client()
        if scope in ("all", "result") and client is not None:
            dropped = client.clear_cache()
        if scope in ("all", "glossary"):
            self.plugin.clear_glossary()
        return json_response({"removed": dropped, "scope": scope, "state": self.plugin.status()})

    async def selftest(self):
        """按当前配置向真实上游发一次请求。"""
        result = await self.plugin.selftest()
        result["state"] = self.plugin.status()
        return json_response(result)

    async def remove_subscription(self):
        """移除一个活动提醒订阅。"""
        payload = await request.json(default={})
        target = str((payload or {}).get("target") or "")
        if not target:
            return error_response("缺少 target", status_code=400)
        if not self.plugin.unsubscribe(target):
            return error_response("该会话不在订阅列表里", status_code=404)
        return json_response(
            {
                "removed": target,
                "subscriptions": self._subscriptions(),
                "state": self.plugin.status(),
            }
        )

    async def test_subscription(self):
        """往指定会话发一条测试消息。"""
        payload = await request.json(default={})
        target = str((payload or {}).get("target") or "")
        if not target:
            return error_response("缺少 target", status_code=400)
        if target not in self.plugin.subscribers():
            return error_response("该会话尚未订阅，无法测试", status_code=404)
        result = await self.plugin.send_test(target)
        if not result.get("ok"):
            return error_response(
                f"发送失败：{result.get('detail') or '未知原因'}", status_code=502
            )
        return json_response({"sent": target})
