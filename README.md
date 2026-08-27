# QLinkBot

基于 QQ 官方机器人 API v2 的简易视频链接解析机器人，当前支持 Bilibili 视频解析（360P）。

## 功能

- WebSocket 事件接入（私聊 / 群@ / 群全量消息）
- 支持 BV 号、AV 号、b23.tv 短链接、完整视频链接
- 每条消息仅解析第一个目标；不支持直播、番剧等非视频内容
- 解析结果以纯文字信息 + 封面图片文件 + 视频文件被动回复原消息（群内 @机器人 的消息会先 @ 提问者，全量消息与私聊不带 @）
- 文件大小限制、解析超时、结果缓存、重复视频限速、全局限流均可配置

## 使用

```bash
pip install -r requirements.txt
copy config.example.toml config.toml   # 填写 appid / secret
python -m qlinkbot.main
```

## 配置说明

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `limits.max_file_size_mb` | 30 | 超过该大小的文件不发送 |
| `limits.parse_timeout_seconds` | 300 | 解析超时时间 |
| `cache.file_ttl_seconds` | 600 | 已下载视频与封面图片的缓存时长（共用） |
| `cache.metadata_ttl_seconds` | 1800 | 元数据缓存时长 |
| `cache.duplicate_window_seconds` | 600 | 会话内重复视频限速窗口 |
| `limits.rate_limit_count/window_seconds` | 10 / 60 | 全局解析次数限制 |
| `behavior.report_errors` | false | 解析失败/超时/重复时是否回复错误提示 |
| `behavior.media_with_text` | true | 尝试封面+文字同条发送；平台不支持时自动拆为两条 |
| `platforms.bilibili.enabled` | true | Bilibili 平台开关 |

> 注意：QQ 富媒体接口对视频有约 30MB 的软限制，`max_file_size_mb` 设置过高可能导致上游报错。
