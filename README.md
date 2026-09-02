# QLinkBot

基于 QQ 官方机器人 API v2 的简易视频链接解析机器人，当前支持 Bilibili 视频解析（360P）。

## 功能

- WebSocket 事件接入（私聊 / 群@ / 群全量消息）
- 支持 BV 号、AV 号、b23.tv 短链接、完整视频链接
- 每条消息仅解析第一个目标；不支持直播、番剧等非视频内容
- 解析结果以纯文字信息 + 封面图片文件 + 视频文件被动回复原消息（群内 @机器人 的消息会先 @ 提问者，全量消息与私聊不带 @）
- 文件大小限制、解析超时、结果缓存、重复视频限速、全局限流均可配置
- `/管理` 指令：markdown 按钮卡片按会话配置解析输出（详见下文「解析管理」）

## 解析管理

发送 `/管理`（群聊需 @机器人）打开管理菜单卡片。消息范围为「获取群内全部消息」时，@机器人 与直接发送均可触发：

- 群聊仅群管理员/群主可触发指令与卡片按钮；私聊不受限制。`behavior.report_errors = false` 时，非管理员触发指令或按钮机器人完全不响应
- 菜单内「bilibili 解析设置」可开关：启用解析、发送封面、发送标题（含 UP 主）、发送简介、发送视频数据、发送原视频链接、发送视频
- 配置按会话隔离（群聊按 group_openid、私聊按 user_openid），默认全部开启，改动即持久化到 `admin.session_store_path` 指向的 JSON 文件，重启保留
- 关闭某项后解析结果不再包含对应内容；若封面/标题/简介/视频数据/原链接全部关闭，则解析成功只发送一条视频消息
- 启动时会通过 QQ「指令面板」接口注册 `/管理` 指令（`admin.register_panel` 可关闭）

## 使用

```bash
pip install -r requirements.txt
copy config.example.toml config.toml   # 填写 appid / secret
python -m qlinkbot.main
```

## Docker Compose 部署

```bash
cp config.example.toml config.toml   # 填写 appid / secret
docker compose up -d --build
docker compose logs -f               # 查看运行日志
```

镜像内置 ffmpeg（启用 B 站高画质 dash 合并流）。`config.toml` 以只读方式挂载进容器，`downloads/` 缓存目录与 `data/` 会话配置目录挂载到宿主机，配置修改后 `docker compose restart` 生效。机器人通过出站 WebSocket 连接 QQ 服务，无需暴露端口。

## 配置说明

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `limits.max_file_size_mb` | 30 | 超过该大小的文件不发送 |
| `limits.probe_real_size` | true | 下载前向 B 站 CDN 发 Range 请求探测实际文件大小用于预检；失败自动回落为 playurl API filesize 估算 |
| `limits.parse_timeout_seconds` | 300 | 解析超时时间 |
| `limits.error_retry_count` | 3 | yt-dlp 解析遇暂时性错误（HTTP 412/429/5xx）时的重试次数，指数退避间隔重试；`0` 不重试 |
| `cache.file_ttl_seconds` | 600 | 已下载视频与封面图片的缓存时长（共用） |
| `cache.metadata_ttl_seconds` | 1800 | 元数据缓存时长 |
| `cache.duplicate_window_seconds` | 600 | 会话内重复视频限速窗口 |
| `cache.cleanup_interval_seconds` | 60 | 缓存定时清理间隔，到期后删除过期视频/封面文件及残留；`<=0` 禁用 |
| `limits.rate_limit_count/window_seconds` | 10 / 60 | 全局解析次数限制 |
| `behavior.report_errors` | false | 解析失败/超时/重复时是否回复错误提示；同时控制非管理员触发 `/管理` 时的提示 |
| `behavior.media_with_text` | true | 尝试封面+文字同条发送；平台不支持时自动拆为两条 |
| `platforms.bilibili.enabled` | true | Bilibili 平台开关 |
| `admin.command` | "/管理" | 管理指令触发词，同时用于指令面板注册 |
| `admin.register_panel` | true | 启动时通过 QQ 指令面板接口注册该指令 |
| `admin.session_store_path` | "data/session_settings.json" | 会话级解析开关持久化文件；留空仅存内存 |

> 注意：QQ 富媒体接口对视频有约 30MB 的软限制，`max_file_size_mb` 设置过高可能导致上游报错。
