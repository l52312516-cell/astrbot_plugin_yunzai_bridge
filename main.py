from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from functools import partial
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api.message_components import File, Image, Json, Music, Record, Share, Video
except ImportError:
    File = Image = Json = Music = Record = Share = Video = None

try:
    from astrbot.core.agent.tool import FunctionTool
except ImportError:
    FunctionTool = None


PLUGIN_ID = "astrbot_plugin_yunzai_bridge"
MAX_COMMAND_LENGTH = 1000
MAX_MEDIA_BYTES = 20 * 1024 * 1024
MAX_DYNAMIC_TOOLS = 7


def _tool_decorator(name: str):
    """Keep the plugin importable on older AstrBot versions without llm_tool."""
    decorator = getattr(filter, "llm_tool", None)
    if decorator is None:
        return lambda fn: fn
    try:
        return decorator(name=name)
    except TypeError:
        return decorator(name)


def _event_decorator(name: str):
    decorator = getattr(filter, name, None)
    if decorator is None:
        return lambda fn: fn
    try:
        return decorator()
    except TypeError:
        return decorator


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


def _sync_download_media(url: str, token: str, timeout: float) -> tuple[bytes, str]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "*/*",
            "Authorization": f"Bearer {token}",
            "User-Agent": "AstrBot-Yunzai-Bridge/1.3.9",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_MEDIA_BYTES + 1)
        content_type = str(response.headers.get("Content-Type") or "application/octet-stream").split(";", 1)[0]
    if len(data) > MAX_MEDIA_BYTES:
        raise ValueError(f"媒体超过 {MAX_MEDIA_BYTES // 1024 // 1024} MiB 限制")
    if not data:
        raise ValueError("媒体内容为空")
    return data, content_type


@register(
    PLUGIN_ID,
    "l52312516-cell",
    "让 AstrBot Agent 通过 HTTP 调用远程 Yunzai 命令和游戏查询模板",
    "1.3.9",
)
class AstrBotYunzaiBridge(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._dynamic_tool_names: set[str] = set()
        self._agent_capabilities: dict[str, dict[str, Any]] = {}
        self._refresh_task: asyncio.Task[Any] | None = None

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

    def _bool_cfg(self, key: str, default: bool) -> bool:
        value = self._cfg(key, default)
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off", ""}
        return bool(value)

    def _refresh_seconds(self) -> int:
        try:
            return max(30, min(int(self._cfg("capability_refresh_seconds", 60)), 3600))
        except (TypeError, ValueError):
            return 60

    def _reply_delivery_mode(self) -> str:
        mode = str(self._cfg("reply_delivery_mode", "") or "").strip().lower()
        if mode in {"yunzai_native", "astrbot_forward", "capture_only"}:
            return mode
        # Old boolean settings are intentionally ignored. They could disagree
        # and silently force upgraded installations into capture-only mode.
        return "yunzai_native"

    async def initialize(self) -> None:
        if not self._bool_cfg("dynamic_tools_enabled", True) or self.context is None:
            return
        self._refresh_task = asyncio.create_task(self._capability_refresh_loop())

    async def terminate(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            self._refresh_task = None
        self._remove_dynamic_tools()

    async def _capability_refresh_loop(self) -> None:
        while True:
            try:
                await self._refresh_dynamic_tools()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(f"[Yunzai Bridge] 动态能力刷新失败: {type(error).__name__}: {error}")
            await asyncio.sleep(self._refresh_seconds())

    def _tool_manager(self) -> Any:
        getter = getattr(self.context, "get_llm_tool_manager", None)
        return getter() if callable(getter) else None

    def _remove_dynamic_tools(self, names: set[str] | None = None) -> None:
        manager = self._tool_manager() if self.context is not None else None
        remove_func = getattr(manager, "remove_func", None)
        for name in sorted(names if names is not None else self._dynamic_tool_names):
            if callable(remove_func):
                remove_func(name)
            self._dynamic_tool_names.discard(name)

    @staticmethod
    def _dynamic_tool_description(capability: dict[str, Any]) -> str:
        labels = {
            "music": "调用已加载的 Yunzai 音乐或点歌插件。本地点歌工具不存在或失败时必须使用本工具。",
            "search": "调用已加载的 Yunzai 搜索、百科或翻译插件；回答不支持前应先尝试本工具。",
            "game": "调用已加载的 Yunzai 游戏查询插件，包括面板之外的攻略、图鉴、体力和战绩。",
            "media": "调用已加载的 Yunzai 图片或媒体查询插件。",
            "entertainment": "调用已加载的 Yunzai 安全娱乐查询插件。",
            "utility": "调用已加载的 Yunzai 帮助、状态、计算或实用工具插件。",
            "plugins": "调用其他已加载的 Yunzai 插件。回答 Yunzai 不支持前应先尝试本工具。",
        }
        # Plugin metadata and regular expressions are untrusted. Keep tool
        # descriptions static so capability discovery cannot inject LLM text.
        return labels.get(str(capability.get("id") or ""), labels["plugins"])

    async def _dynamic_music_handler(
        self,
        event: AstrMessageEvent,
        keyword: str = "",
        command: str = "",
    ) -> str:
        keyword = str(keyword or "").strip()
        command = str(command or "").strip()
        capability = self._agent_capabilities.get("music", {})
        if not command:
            templates = [str(item) for item in capability.get("invocation_templates", []) if str(item).strip()]
            if not keyword:
                return _json_text(self._error("keyword 和 command 不能同时为空"))
            if not templates:
                return _json_text(self._error("未发现经过 Yunzai 规则验证的点歌模板，请提供完整 command"))
            command = templates[0].replace("{keyword}", keyword)
        return await self._execute_command(event, command)

    async def _dynamic_command_handler(self, event: AstrMessageEvent, command: str = "") -> str:
        return await self._execute_command(event, command)

    def _make_dynamic_tool(self, capability: dict[str, Any]) -> Any:
        if FunctionTool is None:
            return None
        name = str(capability.get("tool_name") or "")
        description = self._dynamic_tool_description(capability)
        if name == "yunzai_music":
            parameters = {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "歌曲名、歌手或点歌搜索词。"},
                    "command": {"type": "string", "description": "可选完整 Yunzai 点歌命令；留空时使用自动发现模板。"},
                },
            }
            handler = self._dynamic_music_handler
        else:
            parameters = {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的完整 Yunzai 命令。"},
                },
                "required": ["command"],
            }
            handler = self._dynamic_command_handler
        return FunctionTool(name=name, description=description, parameters=parameters, handler=handler)

    def _install_dynamic_tools(self, capabilities: list[dict[str, Any]]) -> None:
        allowed_names = {
            "yunzai_music", "yunzai_search", "yunzai_game", "yunzai_media",
            "yunzai_entertainment", "yunzai_utility", "yunzai_plugins",
        }
        selected = [
            item for item in capabilities[:MAX_DYNAMIC_TOOLS]
            if isinstance(item, dict) and item.get("tool_name") in allowed_names
        ]
        new_names = {str(item["tool_name"]) for item in selected}
        self._remove_dynamic_tools(self._dynamic_tool_names - new_names)
        add_tools = getattr(self.context, "add_llm_tools", None)
        if not callable(add_tools) or FunctionTool is None:
            return
        tools = [self._make_dynamic_tool(item) for item in selected]
        tools = [tool for tool in tools if tool is not None]
        if tools:
            add_tools(*tools)
        self._dynamic_tool_names = new_names
        self._agent_capabilities = {str(item.get("id")): dict(item) for item in selected}

    async def _refresh_dynamic_tools(self) -> bool:
        if not self._bool_cfg("dynamic_tools_enabled", True):
            self._remove_dynamic_tools()
            self._agent_capabilities = {}
            return False
        result = await self._request("GET", "/astrbot-bridge/v1/capabilities")
        if not result.get("success") or not isinstance(result.get("agent_capabilities"), list):
            return False
        self._install_dynamic_tools(result["agent_capabilities"])
        return True

    @_event_decorator("on_llm_request")
    async def on_llm_request(self, event: AstrMessageEvent, req: Any) -> None:
        if not self._bool_cfg("llm_bridge_hint_enabled", True) or not self._dynamic_tool_names:
            return
        marker = "[AstrBot Yunzai Bridge Guidance]"
        current = str(getattr(req, "system_prompt", "") or "")
        if marker in current:
            return
        guidance = [marker]
        if "yunzai_music" in self._dynamic_tool_names:
            guidance.append("用户请求点歌或音乐时，本地工具不存在或失败后必须继续调用 yunzai_music，不能直接声称没有点歌插件。")
        guidance.append("对于已注册的 yunzai_* 动态工具，在回答相关能力不支持前必须先尝试对应工具；真实发送状态只依据 media_delivery。")
        req.system_prompt = f"{current}\n\n" + "\n".join(guidance)

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

        origin = getattr(event, "unified_msg_origin", "")
        if callable(origin):
            try:
                origin = origin()
            except Exception:
                origin = ""
        origin_type = ""
        origin_session = ""
        origin_parts = str(origin or "").rsplit(":", 2)
        if len(origin_parts) == 3:
            origin_type = origin_parts[1].strip().lower()
            origin_session = origin_parts[2].strip()

        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None)
        for value in (
            getattr(sender, "user_id", None),
            getattr(sender, "id", None),
            getattr(message_obj, "user_id", None),
        ):
            if value not in (None, ""):
                target["user_id"] = str(value).strip()
                break

        for value in (
            getattr(message_obj, "group_id", None),
            getattr(message_obj, "session_id", None) if "group" in type(message_obj).__name__.lower() else None,
        ):
            if value not in (None, ""):
                target["group_id"] = str(value).strip()
                break

        group_getter = getattr(event, "get_group_id", None)
        user_getter = getattr(event, "get_sender_id", None)
        if callable(group_getter) and not target["group_id"]:
            target["group_id"] = str(group_getter() or "").strip()
        if callable(user_getter) and not target["user_id"]:
            target["user_id"] = str(user_getter() or "").strip()

        if "group" in origin_type and origin_session:
            target["group_id"] = origin_session
        elif ("friend" in origin_type or "private" in origin_type) and origin_session:
            target["group_id"] = ""
            target["user_id"] = origin_session
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
            "User-Agent": "AstrBot-Yunzai-Bridge/1.3.9",
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
            for field_name in ("url", "audio", "image"):
                media_url = message.get(field_name)
                if isinstance(media_url, str) and media_url.startswith("/"):
                    message[field_name] = f"{base_url}{media_url}"
        if status >= 400:
            result["success"] = False
            result.setdefault("error", f"Yunzai HTTP {status}")
        return result

    async def _download_media(self, url: str) -> tuple[bytes, str]:
        download_call = partial(_sync_download_media, url, self._token(), self._timeout())
        to_thread = getattr(asyncio, "to_thread", None)
        if callable(to_thread):
            return await to_thread(download_call)
        return await asyncio.get_running_loop().run_in_executor(None, download_call)

    async def _media_component(self, message: dict[str, Any], temporary_files: list[str]) -> Any:
        media_type = str(message.get("type") or "").lower()
        media_url = str(message.get("url") or "")
        media_prefix = f"{self._base_url()}/astrbot-bridge/v1/media/"
        is_bridge_media = bool(media_url and media_url.startswith(media_prefix))

        if media_type == "image" and Image is not None:
            if is_bridge_media:
                media_data, _ = await self._download_media(media_url)
                from_bytes = getattr(Image, "fromBytes", None)
                return from_bytes(media_data) if callable(from_bytes) else Image.fromBase64(base64.b64encode(media_data).decode("ascii"))
            if media_url.startswith(("http://", "https://")):
                return Image.fromURL(media_url)

        if media_type == "record" and Record is not None:
            if is_bridge_media:
                media_data, _ = await self._download_media(media_url)
                return Record.fromBase64(base64.b64encode(media_data).decode("ascii"))
            if media_url.startswith(("http://", "https://")):
                return Record.fromURL(media_url)

        if media_type == "video" and Video is not None:
            if is_bridge_media:
                media_data, _ = await self._download_media(media_url)
                return Video.fromBase64(base64.b64encode(media_data).decode("ascii"))
            if media_url.startswith(("http://", "https://")):
                return Video.fromURL(media_url)

        if media_type == "file" and File is not None:
            name = str(message.get("name") or "yunzai-media.bin")
            if is_bridge_media:
                media_data, _ = await self._download_media(media_url)
                suffix = os.path.splitext(name)[1][:16]
                with tempfile.NamedTemporaryFile(prefix="yunzai_bridge_", suffix=suffix, delete=False) as temp_file:
                    temp_file.write(media_data)
                    temp_path = temp_file.name
                temporary_files.append(temp_path)
                return File(name=name, file=temp_path)
            if media_url.startswith(("http://", "https://")):
                return File(name=name, url=media_url)

        if media_type == "music" and Music is not None:
            raw_id = message.get("id", 0)
            music_id = int(raw_id) if str(raw_id).isdigit() else 0
            return Music(
                _type=str(message.get("music_type") or "custom"),
                id=music_id,
                url=str(message.get("url") or ""),
                audio=str(message.get("audio") or ""),
                title=str(message.get("title") or ""),
                content=str(message.get("content") or ""),
                image=str(message.get("image") or ""),
            )

        if media_type == "json" and Json is not None:
            return Json(data=message.get("data") or {})

        if media_type == "share" and Share is not None:
            return Share(
                url=str(message.get("url") or ""),
                title=str(message.get("title") or ""),
                content=str(message.get("content") or ""),
                image=str(message.get("image") or ""),
            )

        raise ValueError(f"不支持或缺少可用内容的媒体类型: {media_type or 'unknown'}")

    async def _deliver_captured_media(
        self,
        event: AstrMessageEvent,
        result: dict[str, Any],
        delivery_mode: str,
    ) -> None:
        if delivery_mode == "capture_only":
            return
        if not hasattr(event, "make_result") or not hasattr(event, "send"):
            return

        supported_types = {"image", "record", "video", "file", "music", "json", "share"}
        components = []
        delivered_messages = []
        temporary_files: list[str] = []
        for message in result.get("messages", []):
            if not isinstance(message, dict) or str(message.get("type") or "").lower() not in supported_types:
                continue
            if delivery_mode == "yunzai_native":
                native_status = message.get("native_delivery")
                fallback_statuses = {"failed"}
                if self._bool_cfg("fallback_on_unconfirmed", True):
                    fallback_statuses.add("unconfirmed")
                if native_status not in fallback_statuses:
                    continue
            try:
                component = await self._media_component(message, temporary_files)
                components.append(component)
                delivered_messages.append(message)
            except Exception as error:
                message["delivered_to_astrbot"] = False
                message["delivery_error"] = f"{type(error).__name__}: {error}"
                logger.warning(f"[Yunzai Bridge] 媒体转发失败: {type(error).__name__}: {error}")

        if not components:
            for temp_path in temporary_files:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            return
        try:
            send_result = event.make_result()
            for component in components:
                send_result.chain.append(component)
            await event.send(send_result)
            for message in delivered_messages:
                message["delivered_to_astrbot"] = True
        except Exception as error:
            for message in delivered_messages:
                message["delivered_to_astrbot"] = False
                message["delivery_error"] = f"{type(error).__name__}: {error}"
            logger.warning(f"[Yunzai Bridge] AstrBot 媒体发送失败: {type(error).__name__}: {error}")
        finally:
            for temp_path in temporary_files:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    @staticmethod
    def _delivery_summary(messages: list[dict[str, Any]], empty_status: str) -> dict[str, Any]:
        if not messages:
            return {"status": empty_status, "total": 0, "sent": 0, "failed": 0, "unconfirmed": 0, "confirmed": False}
        sent = sum(1 for message in messages if message.get("native_delivery") == "sent" or message.get("delivered_to_astrbot") is True)
        failed = sum(1 for message in messages if message.get("delivered_to_astrbot") is False or (message.get("native_delivery") == "failed" and message.get("delivered_to_astrbot") is not True))
        unconfirmed = sum(1 for message in messages if message.get("native_delivery") == "unconfirmed" and message.get("delivered_to_astrbot") is not True)
        capture_only = all(message.get("native_delivery") == "capture_only" and message.get("delivered_to_astrbot") is not True for message in messages)
        status = "sent" if sent == len(messages) else "partial" if sent > 0 else "failed" if failed > 0 else "unconfirmed" if unconfirmed > 0 else "capture_only" if capture_only else "unknown"
        return {"status": status, "total": len(messages), "sent": sent, "failed": failed, "unconfirmed": unconfirmed, "confirmed": status == "sent"}

    @classmethod
    def _summarize_media_delivery(cls, result: dict[str, Any]) -> None:
        media_types = {"image", "record", "video", "file", "music", "json", "share"}
        media = [
            message
            for message in result.get("messages", [])
            if isinstance(message, dict) and str(message.get("type") or "").lower() in media_types
        ]
        images = [message for message in media if message.get("type") == "image"]
        result["media_delivery"] = cls._delivery_summary(media, "no_media")
        result["image_delivery"] = cls._delivery_summary(images, "no_image")

    def _rpc_payload(
        self,
        action: str,
        event: AstrMessageEvent,
        body: dict[str, Any],
        send_reply: bool = True,
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
        **legacy_kwargs: Any,
    ) -> str:
        """以当前真实会话用户身份执行远程 Yunzai 命令。

        Args:
            command(string): 完整 Yunzai 命令；主人全权限，普通用户可使用基础游戏、点歌、搜索、媒体和安全娱乐查询。
        Returns:
            JSON 字符串。success 仅表示命令执行；只有 media_delivery.status=sent 才表示媒体确认发出。不得编造 401、授权或待投递状态。
        """
        result_text = await self._execute_command(event, command)
        if not legacy_kwargs:
            return result_text
        result = json.loads(result_text)
        result["ignored_legacy_tool_args"] = sorted(legacy_kwargs)
        return _json_text(result)

    async def _execute_command(self, event: AstrMessageEvent, command: str) -> str:
        command = str(command or "").strip()
        if not command:
            return _json_text(self._error("command 不能为空"))
        if len(command) > MAX_COMMAND_LENGTH:
            return _json_text(self._error(f"command 长度不能超过 {MAX_COMMAND_LENGTH} 个字符"))
        delivery_mode = self._reply_delivery_mode()
        send_reply = delivery_mode == "yunzai_native"

        payload = self._rpc_payload(
            "command.execute",
            event,
            {"command": command},
            send_reply,
        )
        result = await self._request("POST", "/astrbot-bridge/v1/rpc", payload)
        await self._deliver_captured_media(event, result, delivery_mode)
        self._summarize_media_delivery(result)
        return _json_text(result)

    @_tool_decorator("yunzai_game_query")
    async def yunzai_game_query(
        self,
        event: AstrMessageEvent,
        game: str,
        action: str,
        keyword: str = "",
        uid: str = "",
        args: str = "",
        **legacy_kwargs: Any,
    ) -> str:
        """通过远程 Yunzai 游戏查询模板执行任意已注册游戏的结构化查询。

        Args:
            game(string): 游戏或插件标识，先从 yunzai_capabilities 的 game_queries 中选择。
            action(string): 查询动作，先从对应 game 的 game_queries 中选择。
            keyword(string): 查询关键词，例如角色名、图鉴名、攻略关键词。
            uid(string): 可选游戏 UID。
            args(string): 可选附加文本参数，会交给 Yunzai 侧模板渲染。
        Returns:
            JSON 字符串。success 仅表示命令执行；只有 media_delivery.status=sent 才表示媒体确认发出。不得编造 401、授权或待投递状态。
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
        delivery_mode = self._reply_delivery_mode()
        send_reply = delivery_mode == "yunzai_native"

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
        result = await self._request("POST", "/astrbot-bridge/v1/rpc", payload)
        await self._deliver_captured_media(event, result, delivery_mode)
        self._summarize_media_delivery(result)
        if legacy_kwargs:
            result["ignored_legacy_tool_args"] = sorted(legacy_kwargs)
        return _json_text(result)
