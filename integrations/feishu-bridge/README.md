# feishu-bridge：飞书 ↔ OpenKimo 直连桥

把飞书消息直接桥接给本机 OpenKimo Web API，**不依赖 AstrBot**。

```
飞书用户 ──wss 长连接──▶ feishu-bridge ──HTTP/WS──▶ OpenKimo (127.0.0.1:5494)
飞书用户 ◀──回复────── feishu-bridge ◀──回复────── OpenKimo
```

每个飞书用户对应一个独立 OpenKimo 会话（上下文持久），支持文本、图片、
视频、语音、文件附件的多模态转发。

## 快速开始

```bash
pip install -r requirements.txt
cp config.example.json config.json   # 填入飞书凭据
python main.py
```

配置文件字段见 `config.example.json`。`data/` 目录存放会话映射与媒体缓存。

## 前置条件（飞书开放平台）

1. 创建企业自建应用，获取 App ID / App Secret；
2. 权限范围：开通 `im:message`（读取消息）、`im:resource`（获取消息资源）；
3. 事件订阅：添加 `im.message.receive_v1`，连接方式选**长连接**；
4. 发布应用版本（未发布收不到消息）。

## 消息处理策略

| 飞书消息 | 处理方式 |
|---|---|
| 文本 | 直发 |
| 图片 | 小图（<1.9MB）转 data URI 直发，模型直接看图；大图存 `data/media/` 并给路径，OpenKimo 用 ReadMediaFile 查看 |
| 视频（含 post 附件） | 存 `data/media/`，提示 OpenKimo 用 ffmpeg 抽帧 + ReadMediaFile 分析 |
| 语音 | 存 `data/media/`，提示转文字/分析 |
| 文件（PDF 等） | 存 `data/media/`，提示用工具读取 |

要点（2026-11 实测踩坑，勿删）：
- 新版飞书发送视频/文件时是 **post 消息，附件挂在 content 顶层 `files` 数组**，
  正文 content 里只有文字；只解析 content 内的 tag 会静默丢附件。
- OpenKimo 侧需 `approval.yolo=True`（web 会话默认），否则工具审批会卡死。
- LLM API 无法加载本地路径形式的 `image_url`，历史里混入会使整个 turn 报错
  （发 `/ok_reset` 重置会话可解）。

## 指令

| 指令 | 作用 |
|---|---|
| `/ok_reset` `/reset` | 重置当前用户对应的 OpenKimo 会话 |

## 运行与排障

- 日志：`data/bridge.log`（stdout）、`data/bridge_err.log`（异常）；
- 单实例运行：飞书长连接同一应用多实例会互踢，重启前确认旧进程已退出；
- 同用户并发保护：上一条未处理完时提示稍候，不排队。

## 依赖版本说明

`lark-oapi` 的 ws 客户端在 import 时创建全局事件循环，本桥复用该循环
（`asyncio.get_event_loop()`），不要新建 loop，否则收消息循环不会运行。
