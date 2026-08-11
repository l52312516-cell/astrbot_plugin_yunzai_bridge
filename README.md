# AstrBot Yunzai Bridge

AstrBot 侧联动插件。它向 AI Agent 注册四个工具，通过带 Bearer Token 的 HTTP RPC 调用远程 Yunzai，并把 Yunzai 插件回复转换为 Agent 可读取的 JSON。

- 作者：[l52312516-cell](https://github.com/l52312516-cell)
- 仓库：[l52312516-cell/astrbot_plugin_yunzai_bridge](https://github.com/l52312516-cell/astrbot_plugin_yunzai_bridge)
- 配套 Yunzai 插件：[l52312516-cell/yunzai_plugin_astrbot_bridge](https://github.com/l52312516-cell/yunzai_plugin_astrbot_bridge)
- 当前版本：`1.3.3`

## 功能

- 检查 Yunzai 桥接服务健康状态。
- 获取 Yunzai 的分级权限策略、结构化游戏模板和已加载插件规则。
- 以当前 AstrBot 消息发送者身份执行 Yunzai 命令。
- 通过统一的 `game/action/keyword/uid/args` 参数调用任意游戏模板。
- 捕获 Yunzai 返回的文字、图片和其他消息段。
- 自动下载 Yunzai 临时媒体，并通过 AstrBot 原生 `Image` 消息发送到当前会话。
- 默认沿用初版机制，让 Yunzai Bot 通过 `nativeReply` 立即发送回复。
- 不向 Agent 暴露 `user_id`、`group_id` 或 `bot_id` 覆盖参数。

## 前置条件

- AstrBot `4.x`。
- 已安装并启动配套 Yunzai 端插件。
- AstrBot 能访问 Yunzai 的 TCP `1145` 端口。
- 两端配置相同的共享 Token。

## 安装

### 使用插件包

将插件解压到：

```text
<AstrBot>/data/plugins/astrbot_plugin_yunzai_bridge/
```

最终目录至少包含：

```text
astrbot_plugin_yunzai_bridge/
  main.py
  metadata.yaml
  _conf_schema.json
  README.md
```

重启 AstrBot 或在插件管理页面重载插件。

### 使用 Git

在 AstrBot 的 `data/plugins/` 目录执行：

```bash
git clone https://github.com/l52312516-cell/astrbot_plugin_yunzai_bridge.git
```

## 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `yunzai_url` | 空 | Yunzai 桥接地址，例如 `http://127.0.0.1:1145` |
| `token` | 空 | 与 Yunzai 端完全一致的共享 Token |
| `request_timeout` | `30` | HTTP 请求超时秒数，范围 `1-120` |
| `allow_send_reply` | `true` | 是否允许默认的 Yunzai `nativeReply` 原生发送 |
| `deliver_captured_images` | `true` | 是否把捕获图片下载后发送到当前 AstrBot 会话 |

地址示例：

```text
同机:       http://127.0.0.1:1145
局域网:     http://192.168.1.100:1145
Docker:     http://yunzai:1145
```

跨设备或跨容器时不要填写 AstrBot 所在机器的 `127.0.0.1`。地址必须指向 Yunzai 所在主机或容器。

## Agent 工具

### `yunzai_health`

检查桥接服务是否可访问。

```text
参数: 无
返回: 服务名称、版本、监听地址和发送策略
```

建议安装后首先调用此工具。

### `yunzai_capabilities`

获取：

- 主人和普通用户权限策略。
- 当前配置的 `game_queries`。
- Yunzai 已加载插件及其规则正则。
- 规则原始 `permission` 和普通用户候选分类。
- 回复发送策略和插件发现状态。

自动发现只是候选目录，不代表普通用户可以执行所有发现命令。最终权限由 Yunzai 在收到完整命令后判定。

### `yunzai_execute`

执行完整 Yunzai 命令：

```text
command: 完整命令，最大 1000 字符
send_reply: 是否由 Yunzai Bot 原生发送回复，默认 true
```

示例：

```text
yunzai_execute(command="#星铁体力")
yunzai_execute(command="#星铁黄泉攻略")
yunzai_execute(command="#面板100000001")
```

工具会自动从当前 `AstrMessageEvent` 读取用户 ID 和群 ID。Agent 不能传入另一个用户、群或 Yunzai Bot 账号。

### `yunzai_game_query`

使用 Yunzai 端 `game_queries` 中的结构化模板：

| 参数 | 说明 |
| --- | --- |
| `game` | 游戏标识或别名，以能力接口返回值为准 |
| `action` | 模板动作名 |
| `keyword` | 角色、武器、图鉴或攻略关键词 |
| `uid` | 可选游戏 UID |
| `args` | 模板需要的其他文本参数 |
| `send_reply` | 是否发送到真实会话，默认 `false` |

示例：

```text
yunzai_game_query(game="bh3", action="panel", uid="100000001")
yunzai_game_query(game="bh2", action="catalog", keyword="索尔之锤")
yunzai_game_query(game="starrail", action="strategy", keyword="黄泉")
yunzai_game_query(game="genshin", action="panel", uid="100000001")
yunzai_game_query(game="zzz", action="panel", uid="100000001")
yunzai_game_query(game="wuthering", action="help")
```

游戏和动作不是写死在 AstrBot 插件中的。调用前先通过 `yunzai_capabilities` 读取 Yunzai 当前配置。

## 身份与权限

RPC 目标由插件内部从当前消息事件生成：

```json
{
  "target": {
    "group_id": "当前群号或空字符串",
    "user_id": "当前消息发送者"
  }
}
```

Yunzai 端忽略 RPC 中的 Bot ID，并使用自己的 `default_bot_id` 或默认账号。Yunzai 每次执行前重新读取主人配置，不信任 Agent 声明的角色。

| 角色 | 行为 |
| --- | --- |
| 主人 | 允许全部 Yunzai 命令 |
| 普通用户 | 仅自己的 UID、面板更新和基础游戏查询 |
| 身份缺失 | 按普通用户处理并拒绝执行 |

普通用户遇到高风险、插件特权、未知或无法分类命令时，会收到 HTTP `403` 和 `PERMISSION_DENIED`。

## 返回格式

成功示例：

```json
{
  "success": true,
  "request_id": "...",
  "role": "ordinary",
  "category": "note",
  "command": "#星铁体力",
  "messages": [
    { "type": "text", "text": "..." },
    { "type": "image", "url": "..." }
  ],
  "duration_ms": 42
}
```

Yunzai 插件直接返回图片 `Buffer` 时，图片消息会是紧凑引用：

```json
{
  "type": "image",
  "url": "http://127.0.0.1:1145/astrbot-bridge/v1/media/<随机ID>",
  "mime_type": "image/png",
  "size_bytes": 123456,
  "sha256": "...",
  "temporary": true,
  "expires_in_seconds": 300,
  "requires_bearer_token": true
}
```

媒体 URL 访问时仍需携带与 RPC 相同的 Bearer Token。它用于避免原始 PNG 数据污染 Tool Result 和 AstrBot 日志，不会把共享 Token 放进 URL。当调用显式使用 `send_reply=false` 且开启 `deliver_captured_images` 时，AstrBot 插件会下载图片并调用 `Image.fromBytes` 和 `event.send()`；成功后消息还会包含 `delivered_to_astrbot: true`。

权限拒绝示例：

```json
{
  "success": false,
  "error": "权限不足",
  "error_code": "PERMISSION_DENIED",
  "role": "ordinary",
  "reason": "普通用户不能执行管理或全局修改命令",
  "messages": []
}
```

连接错误、Token 缺失、空参数和超长参数也会返回结构化 JSON，不会把 Python 异常直接交给 Agent。

## 回复发送策略

默认 `send_reply=true`，沿用初版最快的 Yunzai 原生发送路径。需要同时满足：

1. AstrBot 配置 `allow_send_reply=true`。
2. Yunzai 配置 `allow_send_reply=true`。
3. Agent 调用工具时传入 `send_reply=true`。

上述配置控制的是“由 Yunzai Bot 发送”。现有配置文件中的 `false` 不会在升级时被覆盖，需要手动打开一次。图片另有 AstrBot 后备路径：

- `deliver_captured_images=true`：AstrBot 下载捕获图片并发送到当前 AstrBot 会话。
- `deliver_captured_images=false`：只在 Tool Result 保留临时媒体 URL。
- 调用使用 `send_reply=true` 时跳过 AstrBot 图片转发，防止两端重复发送。

注意：禁止发送回复不等于禁止命令副作用。主人执行配置修改、更新或重启命令时，即使 `send_reply=false`，命令本身仍可能生效。

## 安全建议

- 主人会话中的提示注入可能获得完整 Yunzai 命令权限，只在可信主人会话中使用 Agent。
- 使用足够长的随机 Token，不要把 Token 写入公开日志或聊天记录。
- Agent 工具无法覆盖会话身份，但拿到 Token 的直连客户端仍能手工构造 RPC `user_id`。
- Yunzai 优先监听 `127.0.0.1`；局域网模式配合防火墙或容器网络限制来源。
- 不要把 `1145` 直接暴露到公网。

## 故障排查

### `未配置 Yunzai 地址`

在 AstrBot 插件配置中填写完整 URL，必须包含 `http://` 和端口。

### `未配置共享 Token`

两端都必须配置 Token，并且内容完全一致。

### `连接 Yunzai 失败`

- 确认 Yunzai 日志显示桥接服务已监听。
- 检查 IP、端口、Docker 网络和防火墙。
- 跨机器时 Yunzai `host` 需要设置为 `0.0.0.0`。

### 返回 `401 Unauthorized`

两端 Token 不一致，或请求没有携带 Bearer Token。

### 返回 `403 PERMISSION_DENIED`

这不是连接故障。检查当前用户角色、插件规则权限和命令类别；普通用户不能执行管理命令或未知命令。

### Agent 找不到工具

- 确认插件已加载且没有 Python 导入错误。
- 当前 AstrBot 版本需支持 LLM Tool；插件为旧版装饰器保留了兼容导入逻辑。
- 重新加载插件并新建一次 Agent 会话。

### Tool Result 出现 `�PNG`、`IHDR` 或大量乱码

旧版 Yunzai 桥接把图片 `Buffer` 转成了字符串。将 AstrBot 和 Yunzai 两端都升级到 `1.3.3`；默认由 Yunzai 原生发送，捕获模式下才由 AstrBot 转发临时媒体。

### 图片 URL 正常但会话没有收到图片

- 确认两端都是 `1.3.3`。
- 追求初版发送速度时，确认两端 `allow_send_reply=true`。
- 确认 AstrBot 配置 `deliver_captured_images=true`。
- 查看 Tool Result 中的 `delivered_to_astrbot` 和 `delivery_error`。
- 确认 AstrBot 到 Yunzai 的媒体 URL 仍可访问，图片需要在 5 分钟内下载。

## 开发测试

本插件仅使用 Python 标准库发起 HTTP 请求，不需要额外 pip 依赖。Python 3.8 使用 `run_in_executor`，支持 `asyncio.to_thread` 的版本会自动使用后者。

```bash
python tests/test_astrbot_bridge.py
```

测试包含 AstrBot API Stub、工具装饰器、配置读取、身份继承、Payload、能力透传、错误返回及参数长度限制。

## 作者

- `l52312516-cell`
- GitHub：[https://github.com/l52312516-cell](https://github.com/l52312516-cell)
- 仓库：[https://github.com/l52312516-cell/astrbot_plugin_yunzai_bridge](https://github.com/l52312516-cell/astrbot_plugin_yunzai_bridge)
