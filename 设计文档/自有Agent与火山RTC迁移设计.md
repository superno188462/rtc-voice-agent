# 自有 Agent 与火山 RTC 迁移设计

## 1. 目标

将本项目从“火山 AI 音视频互动方案运行 Agent，项目通过 CustomLLM 回调接入自有 LLM/RAG”迁移为：

- 火山 RTC 只提供房间和实时音视频传输。
- Agent 由本项目服务编排。
- Agent 通过独立能力网关的 HTTP 流调用 LLM、RAG、记忆和用户画像。
- ASR、TTS 使用独立的语音模型服务；当前按火山豆包语音独立服务设计，与火山 Agent 无关。
- 自有媒体机器人负责把 RTC 音频接入 ASR/Agent/TTS 链路并将合成音频发布回房间。
- 当前迁移范围不包含数字人视频。
- 浏览器只持有 RTC 用户 Token，不持有网关 API Key。

本迁移分阶段进行。每阶段都保留可运行的回退点；在自有语音机器人通过端到端验证之前，不删除旧的火山 Agent 路径。

## 2. 当前实现与差距

当前浏览器使用 `@volcengine/rtc` 加入 RTC 房间并发布麦克风音频。加入房间后，`useJoin` 调用 `StartVoiceChat`；`RtcClient` 也通过 RTC OpenAPI 启停火山 Agent，并用房间二进制消息收取火山 Agent 的状态和字幕。

Python 服务当前负责生成 RTC Token、代理 `StartVoiceChat` / `StopVoiceChat`，并提供面向火山的 `CustomLLM` SSE 回调。回调内部调用项目里的 LLM 与 RAG 实现。这仍由火山服务负责语音识别、回合控制和语音合成，不能满足自有 Agent 编排目标。

项目目前没有服务端媒体机器人。Python RTC Token 生成器只签发入房凭证，不具备 RTC SDK 的音频订阅、PCM 帧处理、音频发布或视频发布能力。

## 3. 目标架构

```text
浏览器
  ├─ REST：创建/结束 Agent 会话、鉴权
  └─ RTC Web SDK：发布用户麦克风，订阅机器人媒体流
                  │
             火山 RTC 房间
                  │
Linux 媒体机器人（独立进程/容器，RTC 用户）
  ├─ 订阅用户音频并输出 PCM/音频帧
  ├─ 接收 TTS 音频并发布 RTC 音频流
  └─ 本阶段只发布合成音频流，不处理视频
                  │ 内网双向流式通道
Python Agent 编排服务
  ├─ ASR / VAD / 对话回合 / 打断 / 会话生命周期
  ├─ 记忆与用户画像的业务上下文装配
  └─ HTTP 流客户端（网关 URL 与 API Key 只在服务端）
                  │ HTTPS SSE/HTTP 流
             能力网关
       LLM / RAG / 记忆 / 用户画像
```

媒体机器人和 Agent 编排服务通过内部 WebSocket 或 gRPC 双向流通信。第一阶段实现时选择 WebSocket，先承载带序号、时间戳和会话 ID 的 PCM 帧及控制事件；压测后再决定是否需要 gRPC。RTC AppKey、网关 API Key 和 RAG 凭证仅保存在服务端密钥管理设施中。

## 4. 服务职责

### 浏览器

- 获取服务端签发的 RTC AppId、RoomId、UserId 和短期 Token。
- 加入 RTC 房间并发布麦克风音频。
- 订阅机器人 UserId 的音频流。
- 调用业务 API 建立/结束 Agent 会话，并展示 Agent 状态、字幕和错误。
- 不直接访问能力网关，不接触 AppKey、AK/SK 或网关密钥。

### Python Agent 编排服务

- 创建会话并校验当前用户对房间的权限。
- 管理会话状态、回合 ID、上下文、超时、取消和打断。
- 接收机器人发来的语音识别结果或音频帧；调用 ASR/VAD（部署位置可替换）。
- 将轮次输入、记忆标识和用户画像标识发送给能力网关。
- 消费网关 HTTP 流，转换为内部事件，调用 TTS，并把音频帧发送给机器人。
- ASR、TTS 通过独立适配器访问豆包语音服务；凭证只存服务端，模型/音色等非密钥参数按 Profile 配置。
- 将可展示的文本增量及状态事件推送给浏览器。
- 首阶段优先采用单进程内存会话以快速联调；正式部署前替换为 Redis/PostgreSQL 等可恢复存储，并配置多副本会话路由。

### 媒体机器人

- 使用火山 Linux RTC SDK 作为独立 RTC 用户进房。
- 订阅指定用户音频，按 SDK 支持方式向 Agent 服务传送音频帧。
- 接收 Agent 服务的 TTS 音频，按 SDK 自定义音频采集/发布接口发布到房间。
- 当前只接收/发布 RTC 音频，不实现数字人视频。
- 对房间重连、订阅失败、音频缓冲、时钟漂移和背压负责。

### 能力网关

- 提供约定版本化的 HTTP 流接口；服务端保存网关地址、API Key 和模型/应用标识。
- 负责网关自身提供的 LLM、RAG、记忆和画像能力。
- 不管理 RTC Token、房间生命周期或媒体帧。

## 5. HTTP 流协议约定

网关与 Agent 服务使用 HTTPS POST 和 SSE。迁移首版优先约定 OpenAI Chat Completions 兼容形式：

- 请求：`messages`、`conversation_id`、`turn_id`、`user_id`、可选 `memory_id` 与 `profile_id`。
- 当前 Python 适配器把会话、用户、房间、场景标识放在 `X-Agent-*` 请求头中；网关上线前需要确认这些扩展头是否符合网关约定，并由适配器映射为网关要求的字段。
- 响应：`text_delta` 增量事件，最后以 `done` 结束；错误为包含稳定 `code` 和可公开 `message` 的 `error` 事件。
- Agent 服务忽略未知事件；不记录 API Key、完整个人资料或未经脱敏的音频/文本。
- 客户端取消或房间离开时，Agent 服务取消网关请求及 TTS 任务，并清理该轮缓冲。
- 网关第一阶段若只能提供标准 OpenAI SSE，则由网关适配层封装上述上下文元数据；不要把具体模型供应商格式扩散到 RTC 或前端代码。

流式文本只用于字幕和语音合成输入。SSE 文本块不是 RTC 音频帧；TTS 音频须经媒体通道编码/缓冲后发布。

### ASR/TTS 模型选择

- ASR 首选实时流式识别模型 `bigmodel_async`，因为语音输入时需要增量识别以缩短首字延迟；媒体机器人提供的 RTC 音频帧转为服务要求的输入格式后，经独立 WebSocket ASR 接口发送。
- TTS 首选豆包语音 V3 双向流式接口 `wss://openspeech.bytedance.com/api/v3/tts/bidirection`，适合网关逐段返回文本后持续输入并接收音频片段。
- ASR 与 TTS 的应用 ID、访问令牌、资源 ID 和音色配置通过服务端密钥/配置管理；场景 JSON 只引用 `ASRProfileId` / `TTSProfileId`。
- 具体模型版本、音色和采样参数在媒体机器人 PoC 中按实测延迟与音质确认；当前不选型或配置 Avatar/数字人视频服务。

## 6. API 草案

业务 API（浏览器到 Python）：

- `POST /api/agent/sessions`：创建 Agent 会话，返回 `session_id`、RTC 加房配置和过期时间。
- `POST /api/agent/sessions/{session_id}/turns`：提交文本回合或后续约定的语音识别结果，返回 SSE。
- `POST /api/agent/sessions/{session_id}/interrupt`：取消当前输出并打断播放。
- `DELETE /api/agent/sessions/{session_id}`：结束会话、释放机器人和 RTC 房间资源。

内部机器人 API（只允许内网访问）：

- `WS /internal/agent/sessions/{session_id}/media`：带鉴权的双向音频帧和控制事件通道。
- 每条消息包含协议版本、会话 ID、轮次 ID、序号和媒体时间戳；连接断开后按会话状态重建。

API 具体鉴权方式、网关 SSE 字段和现有登录用户标识，需要结合部署环境在实施阶段确认。本地 Demo 可使用开发密钥；不得将其作为生产安全边界。

## 7. 分阶段迁移

### 阶段 1：保留 RTC 房间，隔离火山 Agent 控制

- 抽出“RTC 房间配置与短期 Token”接口，不再让普通房间 API 读取或依赖 VoiceChat 配置。
- 新增自有 Agent 会话 API 和状态枚举，前端生命周期改为显式创建/结束 Agent 会话。
- 暂时保留旧 VoiceChat API 和 UI 开关作为迁移回退路径。只有在机器人音频通道可用后，才切换前端默认流程，避免当前麦克风流没有 ASR/Agent 消费者。
- 完成验收：客户端可进出普通 RTC 房间；新旧 API 路径清晰隔离；浏览器网络请求中无任何服务端密钥。

### 阶段 2：媒体机器人 PoC

- 在 Linux 环境构建火山 RTC Linux SDK 命令行示例，单独验证 Token、进退房和流订阅。
- 验证 SDK 远端音频帧回调和自定义音频发布能力，再确定 PCM 格式、采样率、声道和帧长。
- 实现机器人服务及 Agent 内部双向流协议；先回放固定音频，再依次接入流式 ASR 和双向流式 TTS。
- 完成验收：真实用户音频可到 ASR 并得到增量文字；机器人可把 TTS 音频发布给浏览器；中断后 ASR/TTS/RTC 音频缓冲均能停止或清空。

### 阶段 3：自有 Agent + 网关流

- Agent 调用配置化网关 URL/API Key；网关完成 LLM、RAG、记忆和画像调用。
- 接通流式文本、TTS 分句、字幕、打断、超时、客户端断连取消和错误恢复。
- 完成验收：端到端语音交互成功；请求取消能停止下游调用；指标可观测首字延迟、首音延迟和轮次耗时。

### 阶段 4：前端切换与旧流程下线

- 自有语音链路验证后将前端默认模式切换为自有 Agent，保留一个迁移窗口供回滚。
- 迁移完成后移除 `StartVoiceChat`、`StopVoiceChat`、`VoiceChat` 场景配置、CustomLLM 回调和火山 Agent 专属控制消息。
- 完成验收：部署文档、回滚方案、密钥轮换、并发/弱网/房间退出验证完成。

## 8. 配置迁移

普通 RTC 房间和 `StartVoiceChat` 是两条不同的配置链。官方 RTC 概念文档定义了 AppId、AppKey、Token、RoomId、UserId；其中 AppKey 用于签发 Token，应只保留在服务端。RTC 客户端进房需要 AppId、RoomId、UserId 和与这些身份匹配的 Token。`StartVoiceChat` 额外要求 TaskId 及其 Agent/Config 结构。

| 配置 | 自有 Agent + 普通 RTC | 火山 StartVoiceChat |
| --- | --- | --- |
| `RTCConfig.AppId` | 需要，房间所有参与方相同 | 需要，且与 RTC Token 的 AppId 一致 |
| `RTCConfig.AppKey` | 仅服务端签发 RTC Token 时需要 | 仅服务端签发 RTC Token 时需要 |
| `RoomId` / `UserId` / `Token` | 需要用于 SDK 进房；可按会话生成 | 需要，且要与 StartVoiceChat 的目标用户对应 |
| `AccountConfig` AK/SK | 不需要 | 仅当业务服务端代调火山 OpenAPI（Start/Stop 等）时需要 |
| `VoiceChat.TaskId`、`AgentConfig.TargetUserId` | 不需要 | StartVoiceChat 必需/相关配置 |
| `VoiceChat.Config.LLMConfig` | 不需要；改用网关 Profile 和服务端网关配置 | 由火山 Agent 流程使用 |
| `VoiceChat.Config.ASRConfig` / `TTSConfig` | 不需要该火山 Agent 配置；语音产品仍需选择自有 ASR/TTS 能力 | 由火山 Agent 流程使用 |
| `BusinessId` | 可选 RTC 业务标识，用于质量/用量划分 | 可选 |

当前新场景样例改为 `SceneConfig`、`RTCConfig`、`AgentConfig`、`MediaConfig`。其中 `AgentConfig.GatewayProfileId` 是网关侧标识；`MediaConfig` 仅保留独立 ASR/TTS Profile ID，不复用火山 Agent 的 ProviderParams。数字人视频不在本次迁移范围。现有 `getScenes` 对 `VoiceChat` 保持可选兼容，迁移完成后再删除旧字段和 OpenAPI 代理。

新增服务端环境变量建议：

```text
GATEWAY_CHAT_URL=
GATEWAY_API_KEY=
GATEWAY_MODEL=
AGENT_SESSION_TTL_SECONDS=3600
MEDIA_BOT_WS_URL=
MEDIA_BOT_INTERNAL_TOKEN=
ARK_ASR_API_KEY=
ARK_ASR_WS_URL=wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async
ARK_ASR_RESOURCE_ID=volc.seedasr.sauc.duration
VOLC_TTS_APP_ID=
VOLC_TTS_ACCESS_TOKEN=
VOLC_TTS_RESOURCE_ID=
```

部署时由密钥管理系统注入真实值。不要将真实密钥写入场景 JSON、前端构建变量或版本库。

## 9. 风险与待决事项

1. Linux SDK 的具体 API/授权/分发方式及服务器环境需通过 SDK 示例和火山侧支持确认；官方有 Linux 桌面与命令行示例包，但 Python 服务并不因此自动具备媒体能力。
2. ASR/TTS 候选采用独立豆包语音模型（实时 ASR `bigmodel_async`、TTS V3 双向流式接口）；仍需在 PoC 中验证控制台开通权限、资源 ID、凭证格式和采样参数。数字人视频暂不考虑。
3. 需要确定能力网关的实际 SSE schema、鉴权头、取消语义和会话元数据字段；本文 OpenAI 兼容协议是首版建议。
4. 浏览器房间 API 必须有业务身份鉴权和房间授权；RTC Token 短期化、用户 ID 绑定、机器人 Token 限权。
5. 当前工作树存在既有未提交更改；迁移操作应只触碰本设计新增的文件和明确新增的实现文件，不覆盖现有场景改动。

## 10. 凭证处置

原 `python_server/scenes/Custom.json.example` 曾包含看起来像真实凭证的示例值；本次已替换为占位符并将其改成自有 Agent 场景格式。若旧示例中的值曾经有效，应在火山控制台轮换，并确认历史提交中没有有效密钥。本文不复述任何凭证值。

## 11. 本次迁移基线

- 前端已停止自动调用火山 `StartVoiceChat`，加入 RTC 房间后创建自有 Agent 会话；浏览器从 RTC SDK 取本地 PCM，经后端转发至豆包 Seed ASR 2.0，识别完成后调用固定 mock LLM SSE。本阶段不接真实网关/TTS，也不播放 TTS。

- 已新增自有 Agent 会话的创建、文本 SSE 回合、显式打断和结束 API。
- 已新增服务端网关适配器，网关地址、Bearer API Key 和模型标识从 Python 服务环境读取；采用 OpenAI Chat Completions SSE 作为暂定协议。
- `getScenes` 现在允许只含 RTC 和自有 Agent/媒体 Profile 的新场景，不会为新场景补写 `VoiceChat`；它只读取 `VoiceChat` 来兼容旧场景 UI，并且仍只向客户端返回去除 `AppKey` 的 RTC 配置。
- Python `Custom.json.example` 已改成自有 Agent 结构，不再包含火山 OpenAPI AK/SK、StartVoiceChat 的 TaskId/Config 或火山 Agent 专用 ASR/LLM/TTS/Avatar 参数；场景只引用独立 ASR/TTS Profile。
- RTC 音频由浏览器转换为 16 kHz、16-bit、单声道 PCM，通过自有后端 WebSocket 代理豆包 `bigmodel_async` 协议；ASR Key 只从后端 `.env` 读取。ASR 最终文本调用 `mock-llm-turns`，由固定 mock 回复替代网关 LLM。

### 当前实现：接入豆包 Seed ASR 2.0

### 链路

```text
RTC 本地音频采集
  → RTC SDK AudioFrameData（Float32 PCM）
  → 浏览器降混、重采样至 16 kHz mono、编码 PCM S16LE
  → WS /api/agent/sessions/{session_id}/asr
  → Python 后端使用服务端 API Key 连接 bigmodel_async
  → asr_partial / asr_final JSON 事件
  → asr_final 调用 mock-llm-turns
  → SSE text_delta / done 展示固定 LLM mock 回复
```

### ASR 配置

在 `python_server/.env` 中设置与 Plan 专用接口匹配的 `ARK_ASR_API_KEY`。默认 WebSocket 地址为 `wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_async`，默认资源 ID 为 `volc.seedasr.sauc.duration`；并发版账号可将资源 ID 改为 `volc.seedasr.sauc.concurrent`。可选覆盖 `ARK_ASR_WS_URL`。直连语音服务的 Key 不一定能用于 Plan 路径；ASR API Key 不得放入浏览器配置或场景 JSON。

### API 事件契约

- 客户端到服务端：WebSocket 二进制帧；裸 PCM S16LE，16 kHz、单声道。
- 服务端到客户端：`{"type":"asr_partial","text":"..."}`、`{"type":"asr_final","text":"..."}` 或 `{"type":"error","code":"..."}`。
- 固定 LLM：`POST /api/agent/sessions/{session_id}/mock-llm-turns`，JSON `{ "text": "ASR 最终文本" }`；SSE 事件为 `text_delta` 和 `done`。
- `ASR_NOT_CONFIGURED` 表示服务端未配置 API Key；`ASR_UPSTREAM_FAILED` 表示上游连接或协议失败。服务端日志仅记录 request id 和错误类型。

### 当前边界

当前 LLM 仍为固定 mock 文本，不请求能力网关；TTS、数字人视频、真实网关编排尚未接入。ASR 协议已实现，但本次静态验证不会证明火山账号的资源授权、密钥有效性或真实麦克风链路可用；应使用账号开通的 Seed ASR 2.0 resource id 联调。会话存储仍为单进程内存实现。
- Linux 媒体机器人、真实 ASR/TTS、TTS 音频发布、共享会话存储和生产鉴权仍属后续迁移工作。

## 12. 参考

- [RTC 基础概念](https://docs.volcengine.com/docs/real_time_communication/Basicconcepts-5?lang=zh)
- [获取 RTC SDK 与 Linux 示例工程](https://docs.volcengine.com/docs/real_time_communication/Getsampleproject?lang=zh)
- [RTC Web SDK](https://docs.volcengine.com/docs/real_time_communication/WebSDK?lang=zh)
- [AI 音视频互动方案接入流程（用于说明现状/迁移边界）](https://docs.volcengine.com/docs/real_time_communication/IntegratedAIaudiovideointeractivesolution?lang=en)
- [豆包语音流式 ASR 模型说明](https://docs.volcengine.com/docs/DoubaoVoice/Accessmust-read_1?lang=zh)
- [豆包语音实时 TTS API 选择说明](https://docs.volcengine.com/docs/DoubaoVoice/HTTPone-timecomposition-non-streaminginterface-V1notrecommended?lang=zh)
