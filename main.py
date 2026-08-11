from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
import uuid
from functools import partial
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


PLUGIN_ID = "astrbot_plugin_yunzai_bridge"
MAX_COMMAND_LENGTH = 1000


def _tool_decorator(name: str):
    """Keep the plugin importable on older AstrBot versions without llm_tool."""
    decorator = getattr(filter, "llm_tool", None)
    if decorator is None:
        return lambda fn: fn
    try:
        return decorator(name=name)
    except TypeError:
        return decorator(name)


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _sync_http_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
) -> tuple[int, Any]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = int(response.status)
    except urllib.error.HTTPError as error:
        raw = error.read()
        status = int(error.code)

    text = raw.decode("utf-8", errors="replace")
    try:
        return status, json.loads(text)
    except json.JSONDecodeError:
        return status, {"raw": text}


@register(
    PLUGIN_ID,
    "l52312516-cell",
    "让 AstrBot Agent 通过 HTTP 调用远程 Yunzai 命令和游戏查询模板",
    "1.3.1",
)
class AstrBotYunzaiBridge(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}

    def _cfg(self, key: str, default: Any = None) -> Any:
        if hasattr(self.config, "get"):
            return self.config.get(key, default)
        return getattr(self.config, key, default)

    def _base_url(self) -> str:
        return str(self._cfg("yunzai_url", "") or "").strip().rstrip("/")

    def _token(self) -> str:
        return str(self._cfg("token", "") or "").strip()

    def _timeout(self) -> float:
        try:
            return max(1.0, min(float(self._cfg("request_timeout", 30)), 120.0))
        except (TypeError, ValueError):
            return 30.0

    def _error(self, error: str, duration_ms: int = 0, **extra: Any) -> dict[str, Any]:
        return {
            "success": False,
            "request_id": uuid.uuid4().hex,
            "messages": [],
            "error": error,
            "duration_ms": duration_ms,
            **extra,
        }

    def _target_from_event(self, event: AstrMessageEvent | None) -> dict[str, str]:
        target = {"group_id": "", "user_id": ""}
        if event is None:
            return target
        group_getter = getattr(event, "get_group_id", None)
        user_getter = getattr(event, "get_sender_id", None)
        if callable(group_getter):
            target["group_id"] = str(group_getter() or "").strip()
        if callable(user_getter):
            target["user_id"] = str(user_getter() or "").strip()
        return target

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        base_url = self._base_url()
        token = self._token()
        if not base_url:
            return self._error("未配置 Yunzai 地址，请填写服务器地址，例如 http://127.0.0.1:1145 或 http://192.168.1.100:1145")
        if not token:
            return self._error("未配置共享 Token，已拒绝发送请求")

        url = f"{base_url}{path}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "AstrBot-Yunzai-Bridge/1.3.1",
        }
        body = None
        if payload is not None:
            body = _json_text(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        try:
            request_call = partial(
                _sync_http_request,
                method,
                url,
                headers,
                body,
                self._timeout(),
            )
            to_thread = getattr(asyncio, "to_thread", None)
            if callable(to_thread):
                status, data = await to_thread(request_call)
            else:
                status, data = await asyncio.get_running_loop().run_in_executor(None, request_call)
        except TimeoutError:
            duration = int((time.perf_counter() - started) * 1000)
            return self._error("连接 Yunzai 超时", duration)
        except urllib.error.URLError as error:
            duration = int((time.perf_counter() - started) * 1000)
            return self._error(f"连接 Yunzai 失败: {error.reason}", duration)
        except Exception as error:
            duration = int((time.perf_counter() - started) * 1000)
            logger.warning(f"[Yunzai Bridge] 请求失败: {error}")
            return self._error(f"请求 Yunzai 失败: {type(error).__name__}", duration)

        duration = int((time.perf_counter() - started) * 1000)
        if isinstance(data, dict):
            result = dict(data)
        else:
            result = {"data": data}
        result.setdefault("success", 200 <= status < 300)
        result.setdefault("request_id", uuid.uuid4().hex)
        result.setdefault("messages", [])
        result.setdefault("duration_ms", duration)
        for message in result.get("messages", []):
            if not isinstance(message, dict):
                continue
            media_url = message.get("url")
            if isinstance(media_url, str) and media_url.startswith("/"):
                message["url"] = f"{base_url}{media_url}"
        if status >= 400:
            result["success"] = False
            result.setdefault("error", f"Yunzai HTTP {status}")
        return result

    def _rpc_payload(
        self,
        action: str,
        event: AstrMessageEvent,
        body: dict[str, Any],
        send_reply: bool = False,
    ) -> dict[str, Any]:
        return {
            "id": uuid.uuid4().hex,
            "action": action,
            "send_reply": bool(send_reply),
            "target": self._target_from_event(event),
            **body,
        }

    @_tool_decorator("yunzai_health")
    async def yunzai_health(self, event: AstrMessageEvent) -> str:
        """检查远程 Yunzai 桥接服务是否可访问。

        Returns:
            JSON 字符串，包含服务版本、监听地址和连接状态。
        """
        return _json_text(await self._request("GET", "/astrbot-bridge/v1/health"))

    @_tool_decorator("yunzai_capabilities")
    async def yunzai_capabilities(self, event: AstrMessageEvent) -> str:
        """查询 Yunzai 分级权限、游戏模板和已加载插件规则。

        Returns:
            JSON 字符串，包含主人/普通用户策略、游戏模板、候选插件规则和服务状态。
        """
        return _json_text(await self._request("GET", "/astrbot-bridge/v1/capabilities"))

    @_tool_decorator("yunzai_execute")
    async def yunzai_execute(
        self,
        event: AstrMessageEvent,
        command: str,
        send_reply: bool = False,
    ) -> str:
        """以当前真实会话用户身份执行远程 Yunzai 命令。

        Args:
            command(string): 完整 Yunzai 命令；主人全权限，普通用户仅限基础游戏操作和查询。
            send_reply(boolean): 是否让 Yunzai 把命令回复实际发送到目标会话，默认 false。

        Returns:
            JSON 字符串，包含执行状态和捕获到的消息。
        """
        command = str(command or "").strip()
        if not command:
            return _json_text(self._error("command 不能为空"))
        if len(command) > MAX_COMMAND_LENGTH:
            return _json_text(self._error(f"command 长度不能超过 {MAX_COMMAND_LENGTH} 个字符"))
        if send_reply and not bool(self._cfg("allow_send_reply", False)):
            return _json_text(self._error("AstrBot 插件配置未允许 Agent 发送 Yunzai 消息"))

        payload = self._rpc_payload(
            "command.execute",
            event,
            {"command": command},
            send_reply,
        )
        return _json_text(await self._request("POST", "/astrbot-bridge/v1/rpc", payload))

    @_tool_decorator("yunzai_game_query")
    async def yunzai_game_query(
        self,
        event: AstrMessageEvent,
        game: str,
        action: str,
        keyword: str = "",
        uid: str = "",
        args: str = "",
        send_reply: bool = False,
    ) -> str:
        """通过远程 Yunzai 游戏查询模板执行任意已注册游戏的结构化查询。

        Args:
            game(string): 游戏或插件标识，先从 yunzai_capabilities 的 game_queries 中选择。
            action(string): 查询动作，先从对应 game 的 game_queries 中选择。
            keyword(string): 查询关键词，例如角色名、图鉴名、攻略关键词。
            uid(string): 可选游戏 UID。
            args(string): 可选附加文本参数，会交给 Yunzai 侧模板渲染。
            send_reply(boolean): 是否实际发送到目标会话，默认 false。

        Returns:
            JSON 字符串，包含渲染出的命令、执行状态和捕获到的消息。
        """
        game = str(game or "").strip().lower()
        action = str(action or "").strip().lower()
        keyword = str(keyword or "").strip()
        uid = str(uid or "").strip()
        args = str(args or "").strip()
        if not game:
            return _json_text(self._error("game 不能为空"))
        if not action:
            return _json_text(self._error("action 不能为空"))
        for field_name, value in (("game", game), ("action", action), ("keyword", keyword), ("uid", uid), ("args", args)):
            if len(value) > MAX_COMMAND_LENGTH:
                return _json_text(self._error(f"{field_name} 长度不能超过 {MAX_COMMAND_LENGTH} 个字符"))
        if send_reply and not bool(self._cfg("allow_send_reply", False)):
            return _json_text(self._error("AstrBot 插件配置未允许 Agent 发送 Yunzai 消息"))

        payload = self._rpc_payload(
            "game.query",
            event,
            {
                "game": game,
                "query_action": action,
                "keyword": keyword,
                "uid": uid,
                "args": args,
            },
            send_reply,
        )
        return _json_text(await self._request("POST", "/astrbot-bridge/v1/rpc", payload))
