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

## 项目账号登录（首版后端）

认证由本项目管理，不依赖 AI 网关账号。设置 `.env` 中的 `DATABASE_URL` 后运行迁移：

```powershell
uv run alembic upgrade head
```

认证接口：

- `POST /api/auth/register`：用户名/密码注册，成功后自动登录。
- `POST /api/auth/login`：登录并设置 HttpOnly Cookie。
- `GET /api/auth/me`：读取当前登录用户。
- `POST /api/auth/logout`：撤销当前会话并清除 Cookie。

首版要求密码长度为 8–128 位；服务端使用 Argon2 哈希密码，并在数据库中保存会话令牌摘要。默认 Cookie 会话有效期 12 小时，可配置为 5 分钟至 30 天；登出撤销当前会话，不提供全端登出。登录失败按用户名和客户端 IP 做数据库限流，密码错误信息统一。跨源前端调用需要 `credentials: 'include'`，后端 `CORS_ALLOW_ORIGINS` 必须列出前端完整来源。`APP_ENV=production` 默认启用 Secure Cookie，生产必须使用 HTTPS。只有当前后端与前端处于跨站域名时，才设 `AUTH_COOKIE_SAMESITE=none`，并同时启用 Secure/HTTPS。`AUTH_ALLOW_REGISTRATION=false` 可关闭公开注册。

React 已接入登录/注册页、刷新后登录态恢复和路由保护；认证状态以服务端 `/api/auth/me` 为准，顶部显示账号并提供退出登录。登录后的 `/getScenes` 每次签发独立 RTC 房间、RTC user_id 和短期 Token，避免多个用户共用场景文件里的固定 RTC 身份。Agent 会话创建、ASR WebSocket、LLM 轮次、打断和关闭接口要求 HttpOnly Cookie 登录态，且会校验会话所属账号。旧调试接口和 RTC OpenAPI 代理也要求登录；遗留 CustomLLM callback 使用单独 Bearer Key。RTC `user_id` 与账号 ID 分开保存和使用。账号/登录方案、网关认证对照及后续对话持久化计划见根目录《登录与账号体系设计.md》。

## Agent Gateway 项目上下文

在后端 `.env` 配置 `GATEWAY_CONTEXT_BASE_URL`（本地示例 `http://127.0.0.1:8000/v1/context`）和 `GATEWAY_PROJECT_API_KEY`。生产可将地址设为 `https://<网关域名>/gateway/v1/context`。提示词模板优先通过 `GATEWAY_CONTEXT_TEMPLATE_ID` 按 ID 读取；该项留空时，才使用 `GATEWAY_CONTEXT_TEMPLATE_CATEGORY` 和 `GATEWAY_CONTEXT_TEMPLATE_NAME`。Key 只由 Python 后端读取，Agent Gateway 项目 ID 从 Key 推导，本项目不向接口传 `project_id`。

当前每个本地账号有一个稳定 `conversation_id`，由本项目 PostgreSQL 只保存 ID 与用户归属；Agent Gateway 的 `external_user_id` 使用服务端认证得到的 `User.id`，其短期会话 `session_id` 使用此 `conversation_id`。RTC 房间及 AgentSession 每次开房都重新创建并关联同一对话，因此离开或重开房间不会清除对话。新房间创建后前端会读取最近 50 条网关消息恢复字幕；LLM 每轮也从网关读取模板、画像、短期历史及最多 5 页长期记忆，并把与当前问题词项相关的记忆拼进 system context。RAG 仍通过现有服务接口在调用 LLM 前加入。

回答调用仍按原流程流式生成，并在成功后将本轮 user/assistant 消息成对追加到网关，默认 TTL 24 小时（`GATEWAY_CONTEXT_TTL_SECONDS`，网关允许 60 秒至 30 天）。每轮还会并行发起一个独立、非流式的 LLM 记忆提取调用，共用 `LLM_API_BASE`、`LLM_API_KEY`、`LLM_MODEL`；`LLM_MEMORY_EXTRACT_MAX_TOKENS` 默认 300。该调用只返回候选，不直接写库。后端要求每条候选包含能在当前或最近用户原话中找到的证据，并校验字段、类型、长度、置信度及敏感信息，校验失败或提取服务失败不会影响回答。回答 LLM 不知道后台持久化结果，因此 system prompt 明确禁止它声称信息已经保存。

用户画像 JSON Schema 位于 `python_server/schemas/user_profile.schema.json`，schema v1 包含 `preferred_language`（`zh-CN`/`en-US`）、`response_style`（`concise`/`balanced`/`detailed`）、`interests` 字符串数组，以及 `food_preferences.likes` / `food_preferences.dislikes` 字符串数组。每项变更必须有用户原话证据且置信度至少为 `AGENT_MEMORY_MIN_CONFIDENCE`（默认 0.75）；画像冲突时重读并合并，重复值不会重复写入。符合这些字段的稳定偏好写入画像；其他明确、稳定且未来有用的事实写入长期记忆。例如“我正在备考教师资格证”归入长期记忆，带受控标签 `学习目标`、`当前目标`、`稳定背景`、`项目经历` 或 `其他`。网关条目还带 `模型提取` 来源标签、`source=llm_extraction`、置信度和项目 schema 版本。一次性信息、推测及敏感信息不保存。记忆提取由独立任务执行，关闭 RTC 房间不会取消；同一用户下一轮开始前等待前一轮提取完成。提取调用 token 上限和超时分别由 `LLM_MEMORY_EXTRACT_MAX_TOKENS`（默认 300）与 `LLM_MEMORY_EXTRACT_TIMEOUT_SECONDS`（默认 20 秒，范围 3–60）控制。

当前短期记忆读取依赖网关返回按顺序排列的 `messages`，画像读取依赖 `profile` 和可选 `version`，模板响应支持 `content`、`template_content` 或 `prompt` 字段。首次使用时，短期消息或画像资源 GET 返回 404 会按“尚未初始化”处理为空；模板、长期记忆、鉴权及网络错误不会伪装成空数据。后端只记录错误类别和 HTTP 状态，不记录 Key、对话、画像或长期记忆正文。`agent_turns` 表只记录轮次 ID、状态和 SHA-256 摘要，不存正文。网关追加超时后会读取最近消息核对；若仍无法确认，会阻止同一逻辑会话的后续追加，避免盲目重复，需先排查网关状态。

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

如果原 Node 服务正在运行，先停止它，避免占用相同的 `3001` 端口。前端开发服务器默认运行在 `3000`，首次访问会进入登录页；注册或登录后进入 RTC 页面。

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

账号和可撤销登录会话已存入 PostgreSQL；Agent 会话本身仍是单进程内存实现。RTC 本地音频帧会由浏览器转换为 16 kHz、16-bit、单声道 PCM，经本服务 ASR 适配器转发至选定上游；ASR 定稿文本交给 LLM 服务生成回复，RAG 提供可选检索上下文。ASR partial/final 事件沿后端 WebSocket 返回，LLM 文本增量通过 SSE 返回。多进程或多实例部署前仍需把 Agent 会话迁入共享存储并配置跨实例路由；当前登录限流由数据库共享，认证 Cookie 使用 HTTPS 安全配置。

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
