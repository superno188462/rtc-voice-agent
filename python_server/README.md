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
- 默认端口 `8002`（可用 `PORT` 环境变量或 `--port` 参数覆盖）
- 相同的场景加载、RTC Token 生成、响应外壳和火山 OpenAPI 签名行为

## 后端能力目录

ASR、LLM、RAG、TTS 是被 Agent 编排层调用的平级能力，不相互包含：

```text
services/
├── agent/   # 会话生命周期、对话上下文与流程编排
├── asr/     # 语音输入转写；当前默认使用 Gateway 适配器
├── llm/     # 文本生成；当前实现 openai_compatible.py
├── rag/     # 项目侧文档切片/持久化与 Gateway 向量存储适配器
└── tts/     # 文本转语音；火山方舟 Agent Plan 适配器
```

目标调用方向是 `Agent → ASR / RAG / LLM / TTS`。RTC 负责传输音频，不属于这四个 AI 能力服务。当前 ASR 通过 Gateway 适配器转发，LLM 使用独立服务，RAG 通过本项目切片服务和 Gateway 向量接口检索，TTS 通过火山方舟 Agent Plan 合成。

## 环境与依赖

项目使用 `uv` 管理 Python、虚拟环境和依赖：

```powershell
cd python_server
uv sync
```

## 统一身份中心登录

注册、登录、登出和当前用户校验由身份中心完成。本项目后端作为 BFF 服务端调用身份中心；浏览器只请求本项目的 `/api/auth/*`，不直接连接身份中心。身份中心返回的 opaque `identity_session` 由本项目后端设置为当前 API 域名下的 HttpOnly Cookie；每次受保护请求由后端将 Cookie 服务端转发到身份中心 `/api/auth/me` 验证。用户主键只采用身份中心验证后返回的 `user.id`，RTC 会话和本项目业务数据仍保存在本项目数据库。

在 `python_server/.env` 配置：

```dotenv
IDENTITY_CENTER_BASE_URL=http://119.45.48.180:8080/identity-center
IDENTITY_CENTER_TIMEOUT_SECONDS=3
AUTH_COOKIE_SECURE=false
AUTH_COOKIE_SAMESITE=lax
AUTH_COOKIE_DOMAIN=
CORS_ALLOW_ORIGINS=http://localhost:3002,http://127.0.0.1:3002
```

上面的 HTTP 地址只适合当前测试环境；生产必须通过 HTTPS。`AUTH_COOKIE_SECURE=true` 用于 HTTPS 部署。若前后端跨站部署，需使用 `SameSite=None`、Secure Cookie、HTTPS，并在 `CORS_ALLOW_ORIGINS` 精确列出前端完整 Origin。`AUTH_COOKIE_DOMAIN` 默认留空，让浏览器使用当前 API 域名的 host-only Cookie。身份中心当前不是 OIDC，也不签发 JWT/Bearer Token；本项目不自行解析或伪造 token。

确认数据库 URL 指向本项目后运行迁移：

```powershell
uv run alembic upgrade head
```

新增迁移 `20261007_06` 会删除本项目旧账号、密码哈希和本地认证会话表，并清空旧 `agent_conversations` / `agent_turns` 后重建空表，以解除对本地账号表的外键依赖。旧语音对话/轮次状态不会保留；知识库表不受该迁移影响。此处只提供迁移脚本，不会自动连接或修改数据库。

认证接口：

- `POST /api/auth/register`：后端代理身份中心注册，成功后设置本项目 HttpOnly Cookie。
- `POST /api/auth/login`：后端代理身份中心登录并设置本项目 HttpOnly Cookie。
- `GET /api/auth/me`：后端携带 opaque Cookie 向身份中心验证并返回规范用户。
- `POST /api/auth/logout`：后端请求身份中心撤销会话并清除本项目 Cookie。

密码策略、账号状态和登录限流由身份中心负责。本项目不保存明文密码、密码哈希或中心认证凭据；身份中心不可用时登录/注册返回明确服务错误，不伪装为未注册或空会话。

React 已接入登录/注册页、刷新后登录态恢复和路由保护；认证状态以服务端 `/api/auth/me` 为准，顶部显示账号并提供退出登录。登录后的 `/getScenes` 每次签发独立 RTC 房间、RTC user_id 和短期 Token，避免多个用户共用场景文件里的固定 RTC 身份。Agent 会话创建、ASR WebSocket、LLM 轮次、打断和关闭接口要求 HttpOnly Cookie 登录态，且会校验会话所属账号。旧调试接口和 RTC OpenAPI 代理也要求登录；遗留 CustomLLM callback 使用单独 Bearer Key。RTC `user_id` 与账号 ID 分开保存和使用。账号/登录方案、网关认证对照及后续对话持久化计划见根目录《登录与账号体系设计.md》。

## Agent Gateway 项目上下文

后端使用同级目录的统一异步 SDK `agent-gateway-client` 0.1.0（导入名 `gateway_client`），统一装配入口为 `services/gateway/client.py`。本地开发依赖相对路径 `../../gateway_api` 并以 editable 模式安装。Context 的画像、长期记忆读写和会话消息读取，以及 RAG/记忆向量集合、写入、删除、相似度检索均通过 SDK。当前 SDK 尚未封装提示词模板读写和短期消息追加；这三项仍由 `services/context_gateway/client.py` 内的兼容适配方法完成，不在业务调用点散布 HTTP 请求。

本地开发在 `python_server` 运行 `uv sync`。部署环境不能依赖开发机上的 `../../gateway_api` 路径；发布时应取得 SDK wheel，或将 `pyproject.toml` 中的本地 source 替换为固定 Git 提交/标签（当前本地 SDK 提交 `b3bdf41e77575f34a8ad440529987fe7e4594971`），然后重新生成并提交部署用 `uv.lock`。不要用浮动 `main` 作为部署依赖。

向量与 Context 共用 `GATEWAY_BASE_URL` 和 `GATEWAY_API_KEY`，在后端 `.env` 各配置一次。地址填写 Gateway API 根路径，例如本地 `http://127.0.0.1:8000/v1`，部署时 `http://<网关域名>/gateway/v1`；SDK 和 Context 适配器会分别拼接所需 API 路径。服务启动时按代码内固定标识同步 `system/agent`（回答提示词）和 `system/memory_extraction`（记忆分析提示词）：缺失时创建，存在但内容不同时用网关版本号进行条件更新，内容相同则跳过。因此本项目中的提示词文件是权威来源，网关侧手动编辑会在项目下次启动时被本地版本覆盖。默认回答提示词位于 `prompts/answer_system.md`，分析提示词位于 `prompts/memory_extraction_system.md`。启动日志只记录模板名和同步状态，不记录提示词或密钥。项目 API Key 需有 owner/editor 权限。Key 只由 Python 后端读取，Agent Gateway 项目 ID 从 Key 推导，本项目不向接口传 `project_id`。

每个身份中心用户有一个稳定 `conversation_id`，由本项目 PostgreSQL 保存 ID 与身份中心 `user.id` 关联；Agent Gateway 的 `external_user_id` 使用服务端验证得到的身份中心 `user.id`，其短期会话 `session_id` 使用此 `conversation_id`。RTC 房间及 AgentSession 每次开房都重新创建并关联同一对话，因此离开或重开房间不会清除对话。新房间创建后前端会读取最近 50 条网关消息恢复字幕；LLM 每轮也从网关读取模板、画像、短期历史及最多 5 页长期记忆。长期记忆通过独立 Gateway 向量集合按语义检索；当前项目仍将有效约束优先加入上下文。RAG 仍通过现有服务接口在调用 LLM 前加入。

回答调用仍按原流程流式生成，并在成功后将本轮 user/assistant 消息成对追加到网关，默认 TTL 24 小时（`GATEWAY_CONTEXT_TTL_SECONDS`，网关允许 60 秒至 30 天）。每轮还会并行发起一个独立、非流式的 LLM 记忆提取调用，共用 `LLM_API_BASE`、`LLM_API_KEY`、`LLM_MODEL`；`LLM_MEMORY_EXTRACT_MAX_TOKENS` 默认 300。该调用只返回候选，不直接写库。后端要求每条候选包含能在当前或最近用户原话中找到的证据，并校验字段、类型、长度、置信度及敏感信息，校验失败或提取服务失败不会影响回答。回答 LLM 不知道后台持久化结果，因此 system prompt 明确禁止它声称信息已经保存。

用户画像 JSON Schema 位于 `python_server/schemas/user_profile.schema.json`，当前使用 schema v4 的稳定特征记录：`traits` 每项含开放的 `domain`、标准 `relation`（likes/dislikes/prefers/avoids/proficient_in/tends_to）、`subject` 和可选 `value`。可记录跨项目稳定的偏好、能力和一般倾向；短期目标、经历和项目内容仍进入长期记忆。读取 v3 时会把旧 `preferences` 转成 `traits`，首次写入时完成迁移。明确的一人称偏好/能力句式由程序补充候选，仍须通过用户原话证据、敏感信息及字段校验；其余表达由分析 LLM 处理，候选置信度阈值为 `AGENT_MEMORY_MIN_CONFIDENCE`（默认 0.75）。同一对象的 likes/dislikes、prefers/avoids 会互相替换，能力和倾向则与偏好分别保存。

当前短期记忆读取依赖网关返回按顺序排列的 `messages`，画像读取依赖 `profile` 和可选 `version`，模板响应支持 `content`、`template_content` 或 `prompt` 字段。长期记忆 metadata 中，`created_at` 由项目在创建时生成 UTC 时间，`updated_at` 在更新时生成；`occurred_at` 表示记忆描述的事件时间，只从用户原话中的明确日期确定性解析，无法可靠解析时为 `null`，不会用写入时间替代。首次使用时，短期消息或画像资源 GET 返回 404 会按“尚未初始化”处理为空；模板、长期记忆、鉴权及网络错误不会伪装成空数据。后端只记录错误类别和 HTTP 状态，不记录 Key、对话、画像或长期记忆正文。`agent_turns` 表只记录轮次 ID、状态和 SHA-256 摘要，不存正文。网关追加超时后会读取最近消息核对；若仍无法确认，会阻止同一逻辑会话的后续追加，避免盲目重复，需先排查网关状态。

## Agent Gateway 向量知识库

RAG 通过 `services/rag/knowledge_base.py` 编排虚拟文件目录、项目侧切片和 `services/rag/gateway_vector_store.py` SDK 适配器。PostgreSQL 保存虚拟路径、内容哈希、metadata 与稳定切片 ID/序号映射；新写入的正文和切片文本只交给 Gateway 保存，Embedding 向量也只存于 Gateway。迁移前已保存的旧正文不会由本迁移自动清除。默认 Agent RAG 检索 Gateway 切片，在调用 LLM 前按文档聚合并按 `chunk_index` 还原顺序。

在 `python_server/.env` 配置：

```dotenv
GATEWAY_BASE_URL=http://127.0.0.1:8000/v1
GATEWAY_API_KEY=服务端项目 API Key
GATEWAY_COLLECTION=rtc-agent-knowledge
RAG_TOP_K=5
```

`GATEWAY_BASE_URL` 填到 `/v1` 或部署前缀加 `/v1`。SDK 负责追加 API 路径、Bearer 认证、超时、Trace ID 和网关异常转换。API Key 仅后端读取；真实 `.env` 已由 `python_server/.gitignore` 忽略。集合由 API Key 自动归属到网关项目，集合名应使用本项目专用名称。Embedding 模型和单条记录字符上限在知识库页面创建集合时选择；Gateway 固定模型、维度和单条记录最大长度。`GATEWAY_VECTOR_TIMEOUT_SECONDS` 设置向量请求超时，默认 15 秒。

### 长期记忆向量检索

长期记忆文本仍以 Gateway Context 的记忆条目为事实源；独立向量 collection 保存一条记忆对应的一条向量，不切分短记忆文本。`GATEWAY_MEMORY_COLLECTION` 与知识库 collection 分开，`GATEWAY_MEMORY_EMBEDDING_MODEL` 必须填写 Gateway 已启用的 Embedding 模型。启动时后端创建或核对该 collection 的模型和 `chunk_size`；已有集合模型或长度配置不匹配时会记下脱敏错误，语音服务仍可启动，记忆读取回退到原关键词筛选。

```dotenv
GATEWAY_MEMORY_COLLECTION=rtc-agent-memory
GATEWAY_MEMORY_EMBEDDING_MODEL=<Gateway 已授权的 Embedding 模型名>
GATEWAY_MEMORY_CHUNK_SIZE=1000
GATEWAY_MEMORY_TOP_K=12
GATEWAY_MEMORY_CONTEXT_LIMIT=12
GATEWAY_MEMORY_VECTOR_TIMEOUT_SECONDS=20
```

每轮在检索前，项目读取该用户的 Context 长期记忆页，将新增或内容/metadata 已变化的条目按 Gateway 返回的稳定 `id` 写入向量集合；同 ID 更新会覆盖向量。metadata 包含身份中心 `user.id`、全局/项目范围、项目 ID、记忆类型/角色、状态、有效期及时间字段。检索按 `external_user_id` 分区，分别查全局记忆和当前项目记忆；本项目再用 Context 条目复核向量命中，并始终补入当前有效的约束类记忆。无效期已过、已删除或不属于当前项目的向量结果不会进入模型上下文。

用户首次打开长期记忆向量检索时，项目会从 Context 分页读取一次历史记忆并补建 Gateway 向量索引；后续检索直接查询 Gateway memory collection 并返回向量命中，不再读取 Context 记录来组装检索结果。Agent 对话中的记忆同步仍会按 Context 当前内容更新向量。迁移 `20261008_08` 保存单条记忆的索引哈希，迁移 `20261008_09` 标记用户/collection 的历史回填完成状态；均不保存记忆正文。配置完成后在 `python_server` 执行 `uv run alembic upgrade head`，再启动后端。向量检索依赖 Gateway Embedding 配额；缺少配置或 Gateway 临时不可用时，会记录操作类别和错误类型。

长期记忆检索预览接口为 `POST /api/agent/memories/search`，请求 `{ "query": "用户喜欢什么水果？", "top_k": 10 }`。后端只使用身份中心验证出的 `user.id`，不接受前端指定用户。接口返回当前用户的记忆 `id`、`content`、`score`、`tags` 和 `metadata`；知识库检索预览继续使用 `POST /api/knowledge-base/search`。这两个接口只返回向量命中，不调用回答 LLM。

登录后访问 `/knowledge-base` 管理文档：上传 UTF-8 `.txt`/`.md` 或粘贴文本，二者统一作为虚拟文件。`directory + name` 经过 NFC 和路径校验后构成当前 collection 内的来源键；同路径再次提交更新原文档，后端沿用内部 `document_id`。不同路径是不同条目，即使内容相同。项目对规范化文本计算全文 SHA-256，并为每个切片记录 `chunk_sha256`；同内容且 metadata 未变时跳过 Gateway，内容变化时使用稳定 ID `{sha256(document_id)}-{chunk_index}` upsert，再删除多余旧片。Gateway metadata 包含 `document_id`、`path`、`chunk_index`、`chunk_count`、哈希、来源字段及业务 metadata。删除时按项目目录映射中的切片 ID 删除 Gateway 记录。正文只在本次请求内解析/切片并发给 Gateway，不再写入新的 PostgreSQL 记录；旧版本已经保存的正文保留原状，后续如需彻底清除可单独安排数据清理。

Metadata 建议分层维护：文件名、扩展名、内容哈希等来源字段由系统自动生成；业务分类、产品/课程、适用版本等权威字段由上传者从受控选项中选择或填写；主题、关键词、摘要等推断字段可在将来由 LLM 提出并按 schema 校验。租户/权限字段必须由服务端可信身份生成。当前 Gateway metadata filter 使用 JSONB 包含语义，项目传入的过滤条件须与记录 metadata 值匹配；范围查询和复杂布尔条件尚未由当前契约提供。

后端管理 API：

- `GET /api/knowledge-base`：查看集合状态与模型/维度/单片上限。
- `POST /api/knowledge-base/initialize`：提交 `{description, embedding_model, chunk_size}` 创建或核对集合。
- `GET /api/knowledge-base/documents`：读取本地虚拟文件目录项，不返回正文。
- `POST /api/knowledge-base/documents`：提交 `{directory, name, source_type, title, content, metadata?}`；同一规范化路径会更新，最多 200,000 字符。
- `POST /api/knowledge-base/documents/batch`：批量提交虚拟文件；每篇最多 200,000 字符。
- `POST /api/knowledge-base/search`：提交 `{query, top_k?, metadata?}`，用于管理页预览；metadata 为网关精确匹配。
- `DELETE /api/knowledge-base/documents?document_id=...`：删除本地文档以及 Gateway 中关联的所有切片向量。

Python 中的实际调用：

```python
from services.rag.knowledge_base import get_knowledge_base_service

knowledge = get_knowledge_base_service()
await knowledge.initialize(description="产品文档", model="volc/doubao-embedding", chunk_size=1000)
saved = await knowledge.upsert_document(
    document_id="refund-policy-v1",
    title="退款规则",
    content="用户可以在订单完成后七天内申请退款……",
    metadata={"category": "售后", "version": 1},
)
hits = await knowledge.search("订单多久可以申请退款？", top_k=5, metadata={"category": "售后"})
await knowledge.delete_document(saved["id"])
```

统一 SDK 使用 `add_vectors()` 写入项目已经切好的文本；不会把整篇文档交给 Gateway 自动切片。文件仅支持 UTF-8 纯文本/Markdown，不含 PDF/Word 解析。向量请求会消耗 Gateway 配置的 Embedding 服务额度。旧版整篇文档向量在第一次迁移/覆盖时按 legacy 文档 ID 清理；没有本地映射且 ID 不可推导的旧记录需在 Gateway 管理端人工清理。

## 场景配置

服务只读取 `python_server/scenes/*.json`。自有 Agent 场景按 `Custom.json.example` 配置：

- `SceneConfig`：名称和前端展示/交互能力。`isAvatarScene`、`avatarBgUrl` 等是当前前端场景模型的兼容字段，不代表本迁移接入数字人视频。
- `RTCConfig`：`AppId`、服务端 `AppKey`，以及可选的房间/用户 ID。Token 由服务端签发；`AppKey` 不返回浏览器。
- `AgentConfig`：网关侧的 Profile ID、机器人 RTC 用户名、打断能力开关。
- `MediaConfig`：自有语音链路所选 ASR/TTS Profile ID。本阶段只接入独立流式 ASR/TTS 模型，不包含数字人视频；这些配置不是火山 Agent 的 `VoiceChat.Config`。

新场景不需要 `AccountConfig` 或 `VoiceChat`。`AccountConfig` 只供旧 `/proxy` 调用火山 OpenAPI 时签名使用；`VoiceChat` 下的 `TaskId`、`AgentConfig.TargetUserId`、ASR/LLM/TTS/Avatar 配置只供火山 AI 音视频互动方案使用。真实场景文件默认被 `.gitignore` 忽略，不要把凭证提交到版本库。

## 启动

开发模式（默认监听 `8002`）：

```powershell
uv run python app.py
```

前端使用 Create React App，默认监听 `3002`。首次访问会进入登录页；注册或登录后进入 RTC 页面。

需要指定其他端口时：

```powershell
# 后端：命令行参数优先级高于 PORT 环境变量
uv run python app.py --port 8001

# 后端：也可以设置环境变量
$env:PORT = "8001"
uv run python app.py

# 前端：PowerShell
$env:PORT = "3003"
$env:REACT_APP_API_HOST = "http://localhost:8002"
npm run dev
```

Git Bash 中可分别运行 `PORT=8001 uv run python app.py` 和 `PORT=3003 REACT_APP_API_HOST=http://localhost:8002 npm run dev`。若后端端口改为 `8001`，前端要相应将 `REACT_APP_API_HOST` 设为 `http://localhost:8001`。若前端改用非默认端口，需同步在 `python_server/.env` 的 `CORS_ALLOW_ORIGINS` 中加入对应来源，例如 `http://localhost:3003`。

## 测试

```powershell
uv run pytest
```

## 接口文档

服务启动后访问：

- Swagger UI：`http://localhost:8000/docs`
- OpenAPI JSON：`http://localhost:8000/openapi.json`

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

ASR 使用 Gateway，TTS 使用火山方舟 Agent Plan。两者独立配置，密钥都只放在后端 `.env`：

```dotenv
ASR_PROVIDER=gateway
ASR_MODEL=volc/doubao-seed-asr-2.0
ASR_WS_URL=
ASR_GATEWAY_WS_URL=ws://119.45.48.180:8080/gateway/v1/audio/stream
ASR_GATEWAY_API_KEY=按网关实际配置填写；若 WebSocket 不需要鉴权则留空
ASR_IDLE_CLOSE_SECONDS=5
ASR_FINALIZE_TIMEOUT_SECONDS=3

VOLC_TTS_API_KEY=火山方舟语音服务 API Key
VOLC_TTS_URL=https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional
VOLC_TTS_RESOURCE_ID=seed-tts-2.0
VOLC_TTS_SPEAKER=控制台中已授权的音色 ID
VOLC_TTS_TIMEOUT_SECONDS=45
```

`ASR_PROVIDER=gateway` 时，后端把 RTC PCM 转发到 `ASR_WS_URL`（若为空则使用 `ASR_GATEWAY_WS_URL`）。Gateway 连接使用项目现有的 `start` JSON、PCM 二进制帧和 `end` JSON 协议；如果该 WebSocket 启用了 Bearer 鉴权，再配置 `ASR_GATEWAY_API_KEY`。`ASR_IDLE_CLOSE_SECONDS` 默认 5 秒：静默时后端结束当前上游 ASR 连接，下一段音频到来后重新连接。Gateway WebSocket 消息协议和鉴权应与部署中的 Gateway 配置一致。

TTS 由后端调用火山方舟 Agent Plan 的 HTTP Chunked 单向流式接口。LLM 文本仍通过 `/turns` SSE 增量返回；后端按句切分，依次调用火山 TTS，并把返回的 PCM 音频块编码后复用同一 SSE 流发送，浏览器用 Web Audio 按顺序调度播放。首句不需要等待整段回答生成完毕。`VOLC_TTS_RESOURCE_ID` 默认 `seed-tts-2.0`；`VOLC_TTS_SPEAKER` 必须填入账号可用的音色 ID。TTS 不经过 Gateway，火山 Key 不会暴露给前端。

用户认证由身份中心统一处理；本项目 PostgreSQL 仅保存业务数据和必要的身份中心用户 ID 关联。Agent 会话本身仍是单进程内存实现。RTC 本地音频帧会由浏览器转换为 16 kHz、16-bit、单声道 PCM，经本服务 ASR 适配器转发至选定上游；ASR 定稿文本交给 LLM 服务生成回复，RAG 提供可选检索上下文。ASR partial/final 事件沿后端 WebSocket 返回，LLM 文本增量通过 SSE 返回。多进程或多实例部署前仍需把 Agent 会话迁入共享存储并配置跨实例路由；认证 Cookie 使用 HTTPS 安全配置。

## 本地 Agent 语音链路

前端加入 RTC 房间并允许麦克风后，会建立自有 Agent 会话和 ASR WebSocket。说话内容由 Gateway ASR 识别；识别完成后由独立 LLM 服务流式生成文本，再由火山 TTS 合成并播放。运行前需要在 `scenes/Custom.json` 配置有效 RTC AppId/AppKey，在 `.env` 配置 Gateway、LLM 和火山 TTS 的服务端凭据及可用音色 ID。

### 语音链路耗时日志

Python 服务终端会输出 `[trace][turn]` 阶段日志及单行汇总，按同一 `turn_id` 关联前端 VAD 起止、ASR 首个 partial/final 和 LLM request/first token/stream end。浏览器开发者工具 Console 另有 `[trace][voice]` 客户端耗时日志。汇总中的 `speech_end_to_asr_final_ms` 表示说完到 ASR 定稿的估算耗时，`speech_end_to_first_token_ms` 表示说完到 LLM 服务端首 token；`llm_request_to_first_token_ms` 是 LLM 服务端首 token时间。要看真实客户端从开始说话到收到首段回复文本，使用浏览器日志 `speech_start_to_client_first_text_ms`；它包含 ASR、LLM 和网络传输时间。

语音起止由前端 PCM RMS 阈值（`0.012`）和 600 ms 静音门限估算，受麦克风噪声、增益和 RTC 帧大小影响；如 `speech_end_observed=false`，该轮无法可靠计算说完到定稿的间隔。浏览器 Console 的 `[trace][voice]` 会记录 `tts_playback_start`、`tts_playback_end` 或 `tts_failed`；端到端听到回复的时间可按 `tts_playback_start - speech_start` 计算。

## 独立 ASR 音频测试

项目内提供了一段中文测试语音，WAV 可用于文件转写，裸 PCM 可用于按 RTC 帧协议构造实时请求：

```powershell
uv run python scripts/test_seed_asr.py samples/asr_test_zh_16k_mono.wav --segment-ms 100
```

样例文件 `samples/asr_test_zh_16k_mono.wav` 是 16 kHz、16-bit、单声道 PCM WAV；`samples/asr_test_zh_16k_mono.pcm` 是去掉 WAV 头的裸 PCM。文件转写脚本使用独立的 `ARK_ASR_FILE_WS_URL`（默认 Plan `bigmodel_nostream`），不会改变 RTC 实时识别使用的 `ARK_ASR_WS_URL`（Plan `bigmodel_async`）。`--segment-ms 100` 对应每包 3200 字节；也可使用 200 ms。脚本会输出 final 文本或服务端错误及 `X-Tt-Logid`，不会打印 API Key。

## Agent 轮次处理与链路面板

语音 ASR 定稿和手动文本都提交到 `POST /api/agent/sessions/{session_id}/turns`，按同一条后端流程处理：读取系统模板、用户画像、向量相关长期记忆和最近会话消息；子查询 LLM 基于当前问题与少量对话改写向量检索问题；通过统一 `gateway_client` SDK 检索知识库，SDK 会按命中记录的 `parent_id` 补取父块；然后把上下文和检索资料交给回答 LLM，同时运行记忆分析 LLM 并写回画像/长期记忆。子查询失败时使用原问题检索，RAG 失败时按无检索资料继续回答。

SSE 仍输出 `text_delta`、`audio_chunk`、`llm_done`、`done` 和 `error`，并增加结构化 `trace` 事件。事件按 `stage` 区分 `context`、`subquery`、`retrieval`、`answer`、`memory_analysis`，含状态、耗时及必要的检索元数据。页面右侧“本轮处理链路”抽屉可以点开每个阶段和向量命中，查看子片、父块、分数及 metadata。记忆分析阶段可展开查看 LLM 的结构化提取结果、项目校验结果、Gateway 向量写入响应或安全错误码（含 HTTP 状态、Trace ID 和未确认记录 ID）。面板展示执行轨迹，不展示模型隐藏推理；链路只在当前浏览器内保留最近 10 轮，退出登录时清除。

检索问题改写采用按需调用：包含“这个/那个/刚才/之前/呢”等上下文指代，或“测试一下/多少钱”等依赖上下文的短问句才调用子查询 LLM；可独立检索的问题直接交给 Gateway，链路状态显示“直接检索”。子查询调用默认 5 秒超时（`LLM_SUBQUERY_TIMEOUT_SECONDS`，范围 2–30 秒），超时或服务失败时使用原问题继续检索。

回答、子查询、记忆分析可分别设置模型：`LLM_ANSWER_MODEL`、`LLM_SUBQUERY_MODEL`、`LLM_MEMORY_MODEL`。模型名必须是 `LLM_API_BASE` 对应服务已配置并授权的完整名称（例如 `volc/...`）；三者共用 `LLM_API_BASE` 与 `LLM_API_KEY`。留空时各自回退到旧配置 `LLM_MODEL`，因此旧 `.env` 无需立即迁移。可先只设置轻量的 `LLM_SUBQUERY_MODEL`，再通过链路面板观察改写耗时和检索结果。

抽屉中的手动文本提问复用同一个会话与 SSE 接口，发送 `synthesize_speech=false`，因此不触发 TTS。未加入 RTC 时首次提问会创建经身份中心会话鉴权的普通 Agent 会话；随后加入语音房间时会复用该会话。语音请求默认合成 TTS。前端只提交文本和会话句柄，Gateway API Key 仍由后端保存。
