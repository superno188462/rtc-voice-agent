# Python Server

这是原 `Server` 的 Python 迁移服务。自有 Agent 通过 RTC 房间运行，不启动原 Node Agent 流程：

- `POST /getScenes`
- `POST /proxy?Action=StartVoiceChat`
- `POST /proxy?Action=StopVoiceChat`
- `POST /api/chat_callback`（CustomLLM 回调）
- `POST /api/agent/sessions`（自有 Agent 会话基础接口）
- `POST /api/agent/sessions/{session_id}/turns`（独立 LLM 服务 SSE 文本生成入口）
- `WS /api/agent/sessions/{session_id}/asr`（RTC PCM 转发至豆包 Seed ASR 2.0，返回 partial/final/error JSON 事件）
- `POST /api/agent/sessions/{session_id}/mock-llm-turns`（保留的固定回复 Mock 接口）
- `POST /api/agent/sessions/{session_id}/interrupt`（取消当前生成）
- `DELETE /api/agent/sessions/{session_id}`（结束自有 Agent 会话）
- 默认端口 `3001`
- 相同的场景加载、RTC Token 生成、响应外壳和火山 OpenAPI 签名行为

## 后端能力目录

ASR、LLM、RAG、TTS 是被 Agent 编排层调用的平级能力，不相互包含：

```text
services/
├── agent/   # 会话生命周期、对话上下文与流程编排
├── asr/     # 语音输入转写；当前实现 volc_seed.py
├── llm/     # 文本生成；当前实现 openai_compatible.py
├── rag/     # 知识检索；当前使用空结果 mock.py，真实实现 volc_knowledge_base.py 暂不调用
└── tts/     # 文本转语音；当前只有供应商无关的 provider 契约
```

目标调用方向是 `Agent → ASR / RAG / LLM / TTS`。RTC 负责传输音频，不属于这四个 AI 能力服务。当前 ASR 和 LLM 已接入各自目录中的适配器；RAG 暂用返回空资料的 Mock，火山知识库实现保留但不调用；TTS 目前只有供应商无关的接口契约，尚未接入实际模型。

## 环境与依赖

项目使用 `uv` 管理 Python、虚拟环境和依赖：

```powershell
cd python_server
uv sync
```

## 场景配置

服务只读取 `python_server/scenes/*.json`。自有 Agent 场景按 `Custom.json.example` 配置：

- `SceneConfig`：名称和前端展示/交互能力。`isAvatarScene`、`avatarBgUrl` 等是当前前端场景模型的兼容字段，不代表本迁移接入数字人视频。
- `RTCConfig`：`AppId`、服务端 `AppKey`，以及可选的房间/用户 ID。Token 由服务端签发；`AppKey` 不返回浏览器。
- `AgentConfig`：网关侧的 Profile ID、机器人 RTC 用户名、打断能力开关。
- `MediaConfig`：自有语音链路所选 ASR/TTS Profile ID。本阶段只接入独立流式 ASR/TTS 模型，不包含数字人视频；这些配置不是火山 Agent 的 `VoiceChat.Config`。

新场景不需要 `AccountConfig` 或 `VoiceChat`。`AccountConfig` 只供旧 `/proxy` 调用火山 OpenAPI 时签名使用；`VoiceChat` 下的 `TaskId`、`AgentConfig.TargetUserId`、ASR/LLM/TTS/Avatar 配置只供火山 AI 音视频互动方案使用。真实场景文件默认被 `.gitignore` 忽略，不要把凭证提交到版本库。

## 启动

开发模式：

```powershell
uv run uvicorn app:app --host 0.0.0.0 --port 3001 --reload
```

如果原 Node 服务正在运行，先停止它，避免占用相同的 `3001` 端口。迁移版 Agent API 目前还未接入前端；当前前端仍走旧流程。

## 测试

```powershell
uv run pytest
```

## 接口文档

服务启动后访问：

- Swagger UI：`http://localhost:3001/docs`
- OpenAPI JSON：`http://localhost:3001/openapi.json`

## 旧版 CustomLLM 回调

`/api/chat_callback` 遵循火山 CustomLLM 的 OpenAI SSE 协议。火山回调地址应配置为：

```text
https://你的公网域名/api/chat_callback
```

启动 Python 服务前设置：

复制 `.env.example` 为 `.env`，再填写：

```text
CUSTOM_LLM_API_KEY=与 VoiceChat.LLMConfig.APIKey 相同的密钥
PUBLIC_BASE_URL=https://你的公网域名
CUSTOM_LLM_PATH=/api/chat_callback
LLM_API_BASE=https://你的模型服务/v1
LLM_API_KEY=你的模型服务 API Key
LLM_MODEL=你的模型名
```

该回调只用于保留的火山 Agent 旧流程。随后将旧场景中的 `VoiceChat.Config.LLMConfig.Mode` 设置为 `CustomLLM`。服务会将
`PUBLIC_BASE_URL` 与 `CUSTOM_LLM_PATH` 拼接为 `Url`；生产环境的前缀必须是公网 HTTPS
地址。`localhost` 只能供本机测试，火山云端无法回调本机地址。

## 自有 Agent 迁移入口

迁移设计见项目根目录 `设计文档/自有Agent与火山RTC迁移设计.md`。新场景的自有 Agent 会话接口不会读取 `VoiceChat`，也不会调用火山 `StartVoiceChat`。创建接口接收当前场景的 `scene_id`、`room_id` 和 `user_id`，返回不透明的 `session_id`。文本轮次先调用平级 RAG 服务（当前 Mock 返回空资料），再调用平级 LLM 服务，并以 SSE 返回 `text_delta`、`done` 或 `error` 事件。提示词允许知识库未命中时使用模型已有知识作答。

LLM 服务使用 OpenAI Chat Completions 兼容协议。将 `.env` 配置为：

```text
LLM_API_BASE=http://127.0.0.1:8000/v1
LLM_API_KEY=在本机 .env 中填写当前有效的服务端密钥
LLM_MODEL=volc/Doubao-Seed-2.0-lite
LLM_TEMPERATURE=2
LLM_MAX_TOKENS=200
```

`LLM_API_BASE` 填写到 `/v1`，SDK 会在其后请求 `/chat/completions`。Key 只由后端读取，不放入前端、场景 JSON 或版本库。截图已暴露 API Key，建议在服务端轮换后，把新 Key 填进本机 `.env`。

ASR 通过 `ASR_PROVIDER` 选择上游，当前默认 `volcengine`，因此可先保持直连火山测试：

```dotenv
ASR_PROVIDER=volcengine
ASR_MODEL=doubao-seed-asr-2.0
ASR_WS_URL=
ARK_ASR_API_KEY=本机后端密钥
ARK_ASR_WS_URL=wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_async
ARK_ASR_RESOURCE_ID=volc.seedasr.sauc.duration
ARK_ASR_END_WINDOW_SIZE=800
```

`ASR_WS_URL` 非空时优先使用它；为空时，火山适配器沿用 `ARK_ASR_WS_URL`。`ASR_MODEL` 是项目侧逻辑模型名，火山适配器会映射为火山请求中的 `bigmodel`。`ARK_ASR_END_WINDOW_SIZE` 默认 800 ms，用于 VAD 静音判停；适配器继续使用双向流式 + 二遍识别。API Key 必须与 Plan 专用接口匹配，只放在后端 `.env`。

切换到网关时设置 `ASR_PROVIDER=gateway`，并将 `ASR_WS_URL`（或 `ASR_GATEWAY_WS_URL`）设为网关 WebSocket 地址；网关认证 Key 可选放在 `ASR_GATEWAY_API_KEY`。网关适配器按当前草案发送 `start` JSON、原始 PCM 二进制帧和 `end` JSON，并把 partial/final/error 归一化。实际接入前需与网关项目确认消息字段和鉴权方式；若网关最终契约不同，只需调整 `services/asr/client.py` 中网关适配器。修改 `.env` 后重启 Python 服务。前端到本项目的 WebSocket 路径和 RTC 音频格式不变，LLM/RAG/TTS 不受此选择影响。TTS V3 尚未接入。

当前会话存储是单进程内存实现。RTC 本地音频帧会由浏览器转换为 16 kHz、16-bit、单声道 PCM，经本服务 ASR 适配器转发至选定上游；ASR 定稿文本交给 LLM 服务生成回复，RAG 提供可选检索上下文。ASR partial/final 事件沿后端 WebSocket 返回，LLM 文本增量通过 SSE 返回。它没有生产级用户鉴权、共享会话存储和跨实例路由，不能直接用于生产。

## 本地 Agent 语音链路

前端加入 RTC 房间并允许麦克风后，会建立自有 Agent 会话和 ASR WebSocket。说话内容由 Seed ASR 2.0 识别；识别完成后由独立 LLM 服务流式生成文本。本阶段不接入 TTS、不播放 AI 语音，也不调用火山 Agent。运行前需要在 `scenes/Custom.json` 配置有效 RTC AppId/AppKey，在 `.env` 配置 ASR 和 LLM 的服务端 Key 并启动 Python 服务。

### 语音链路耗时日志

Python 服务终端会输出 `[trace][turn]` 阶段日志及单行汇总，按同一 `turn_id` 关联前端 VAD 起止、ASR 首个 partial/final 和 LLM request/first token/stream end。浏览器开发者工具 Console 另有 `[trace][voice]` 客户端耗时日志。汇总中的 `speech_end_to_asr_final_ms` 表示说完到 ASR 定稿的估算耗时，`speech_end_to_first_token_ms` 表示说完到 LLM 服务端首 token；`llm_request_to_first_token_ms` 是 LLM 服务端首 token时间。要看真实客户端从开始说话到收到首段回复文本，使用浏览器日志 `speech_start_to_client_first_text_ms`；它包含 ASR、LLM 和网络传输时间。

语音起止由前端 PCM RMS 阈值（`0.012`）和 600 ms 静音门限估算，受麦克风噪声、增益和 RTC 帧大小影响；如 `speech_end_observed=false`，该轮无法可靠计算说完到定稿的间隔。当前未接入 TTS，日志会输出 `tts_not_enabled`，因此还没有合成首音或实际播放开始耗时。真正接入 TTS 后，应额外记录 TTS 请求开始、首音频块到达、播放开始和播放结束；端到端“听到回复”的时间以浏览器 `playback_start - speech_start` 为准。

## 独立 ASR 音频测试

项目内提供了一段中文测试语音，WAV 可用于文件转写，裸 PCM 可用于按 RTC 帧协议构造实时请求：

```powershell
uv run python scripts/test_seed_asr.py samples/asr_test_zh_16k_mono.wav --segment-ms 100
```

样例文件 `samples/asr_test_zh_16k_mono.wav` 是 16 kHz、16-bit、单声道 PCM WAV；`samples/asr_test_zh_16k_mono.pcm` 是去掉 WAV 头的裸 PCM。文件转写脚本使用独立的 `ARK_ASR_FILE_WS_URL`（默认 Plan `bigmodel_nostream`），不会改变 RTC 实时识别使用的 `ARK_ASR_WS_URL`（Plan `bigmodel_async`）。`--segment-ms 100` 对应每包 3200 字节；也可使用 200 ms。脚本会输出 final 文本或服务端错误及 `X-Tt-Logid`，不会打印 API Key。
