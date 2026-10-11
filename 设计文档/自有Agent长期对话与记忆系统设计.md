# 自有语音 Agent：多用户、长期对话与记忆设计

## 1. 设计目标

将当前“单个进程内会话 + 单轮 LLM 请求”演进为可持续使用的语音 Agent：多个用户能同时进入互不冲突的 RTC 房间；用户离开后可继续原对话；Agent 能结合系统提示词、当前对话上下文、用户画像、长期记忆与 RAG 知识回答。

本设计继续保持职责分离：RTC 负责实时音视频传输；本项目负责 Agent 编排、对话持久化和提示词组装；能力网关通过 HTTP API 提供 LLM、RAG、记忆和用户画像能力。Agent 不直接访问网关数据库。

## 2. 当前实现与关键问题

- `GET /getScenes` 在进程内修改全局场景配置：若场景缺少 RTC `RoomId`、`UserId` 或 Token，就生成并写回。进程运行期间，同一场景的后续用户会拿到相同 RTC 身份和房间信息。
- `POST /api/agent/sessions` 又要求请求中的 `room_id`、`user_id` 与该场景的固定值完全一致。这使多个用户共享身份，不能安全隔离并发语音会话。
- `AgentService` 将会话和消息历史放在 Python 进程内存；关闭会话会删除会话对象，服务重启也会丢失历史，无法跨房间续聊。
- 当前 RAG 是空结果 Mock；Agent 每轮调用 RAG 后再调用 LLM，但没有独立的提示词组装器、长期记忆或用户画像流程。

因此需要明确区分 **用户、对话、语音会话、对话轮次**，不能继续把 RTC 房间或当前 WebSocket 当作长期对话标识。

## 3. 核心领域对象

| 对象 | 生命周期与用途 | 关键标识 |
| --- | --- | --- |
| 用户 `User` | 跨设备、跨会话的业务身份；用户画像和长期记忆以此为作用域 | `user_id`（来自已认证身份） |
| 对话 `Conversation` | 可跨多次通话继续的持久化逻辑对话 | `conversation_id` |
| 语音会话 `AgentSession` | 一次浏览器/RTC 加入过程；断线、离开后结束或进入短暂重连状态 | `session_id` |
| RTC 房间 `RtcRoom` | 一次语音会话的媒体传输空间；默认每个会话独立创建 | `rtc_room_id` |
| 轮次 `Turn` | 一次用户输入及其 Agent 回复；用于幂等、历史和耗时追踪 | `turn_id` / `client_turn_id` |

业务 `user_id` 与 RTC `user_id` 分开。RTC 用户标识可以每个会话随机生成，不暴露业务账号，也不作为长期记忆键。

## 4. 多用户 RTC 房间设计

### 4.1 设计决策

- 移除场景配置中的固定 `RoomId`、`UserId` 和预生成 Token。场景只保留 RTC App 配置引用、Agent Profile 等静态配置。
- 用户身份通过登录态或服务端签名的访客身份解析；不信任客户端任意提交的 `user_id`。未接入登录的开发阶段可用随机 `guest_id`，但它只支持同一浏览器续聊，不支持可靠的跨设备续聊。
- 每次创建 `AgentSession` 时由后端生成新的 UUID 房间 ID 和会话级 RTC User ID，并为这对 ID 签发短期 Token。AppKey 仅由服务端读取。
- 并发用户默认各自使用独立房间；只有产品明确需要多人共同参与同一对话时，才建立显式的共享房间/参与者模型。
- 刷新或重连可以在短暂恢复窗口内重新签发 Token 并重新加入同一个活动 Session；新一次通话创建新 RTC 房间，但可继续同一个 Conversation。

### 4.2 建房流程

```text
浏览器已认证用户
  → POST 创建/选择 conversation
  → POST conversations/{id}/sessions
  → 后端校验用户拥有该 conversation
  → 创建唯一 session_id、rtc_room_id、rtc_user_id
  → 服务端签发短期 RTC Token
  → 返回 RTC 加房参数
  → 浏览器加入房间并建立 ASR/Agent 流
```

`GET /getScenes` 只返回场景和 Agent 展示配置，不再分配房间或签发 RTC Token。RTC Token 的权限、有效期和身份绑定在服务端统一处理。

## 5. 关房与继续上次对话

离开电话/RTC 房间表示结束一次 **语音会话**，不代表删除 **逻辑对话**。

- 用户主动离开：结束 ASR/TTS/媒体任务，写入 Session `ended_at`，保存已完成轮次，对话保留。
- 网络意外断开：Session 进入 `reconnecting`，例如保留 60 秒恢复窗口；窗口内允许同一用户恢复。超时后标记 `ended`，但不删除 Conversation。
- 用户稍后回来：读取最近对话列表，选中一个 `conversation_id`，创建新的 RTC Session 和新房间，然后加载该对话的摘要及近期轮次。
- 删除对话：单独提供明确的删除操作；删除对话历史与对话级记忆。用户级画像/长期记忆是否同步删除，按来源和用户隐私设置处理。

前端需要区分“结束通话”和“删除对话”，并提供“新对话”“继续上次”入口。若用户未登录，可将访客标识保存在浏览器，但需明确清理浏览器数据后无法找回。

## 6. 长短期记忆与用户画像

### 6.1 所有权建议

- **本项目 Agent 数据库**：对话、轮次、语音 Session、短期历史/摘要、轮次状态和幂等键。它们是恢复对话与审计 Agent 编排的基础数据。
- **能力网关**：RAG 知识库、用户画像、跨对话长期记忆（如果网关提供正式 API）。本项目只通过服务适配器调用，不直连或复用网关数据库。
- 每一种资料只有一个权威来源。可以有短时缓存，但不得在 Agent DB 与网关间双写出两个互相冲突的用户画像/记忆副本。
- 若网关暂时没有记忆/画像 API，开发期可使用本项目的 Mock Provider；正式接入前决定唯一数据所有方并迁移，不能把 Mock 内存当作持久实现。

### 6.2 记忆层级

1. **短期上下文**：当前 Conversation 的最近若干轮 + 滚动摘要。逐轮保存在 Agent DB；接近模型上下文预算时更新摘要，并记录摘要覆盖到的最大 `turn_seq`。重新开房后从数据库恢复。
2. **长期记忆**：跨 Conversation、对未来回答有帮助且相对稳定的事实、偏好、目标、约束，例如“偏好简洁回答”“正在学习 Python”。以 `user_id` 为作用域，通过网关记忆接口召回和更新。
3. **用户画像**：结构化的稳定用户属性，例如称呼、语言、专业领域、偏好。区分用户主动提供与模型推断；用户可查看、更正和删除。画像不能等同于模型从一句话里的临时推测。
4. **对话记录**：用户和 Agent 的完整文本轮次，按产品留存策略持久化。原始音频默认不存；若未来要存音频，需单独告知、授权和设置期限。

长期记忆提取只针对已完成的用户轮次，使用 `turn_id` 幂等。失败可重试，不影响当轮已返回的回答。避免把密码、密钥、健康/财务等敏感信息自动写入长期记忆；支持用户关闭记忆和清除记忆。

## 7. Agent 编排与提示词/RAG 边界

所有调用由 `AgentOrchestrator` 按一轮流程编排，LLM 适配器不自行读取数据库、画像或 RAG。

```text
ASR final
  → 校验 conversation 所属权、turn 幂等键
  → 持久化用户轮次
  → 并行读取画像、相关长期记忆、RAG 检索结果
  → 读取短期摘要与近期历史
  → PromptComposer 组装带预算的 LLM messages
  → 流式调用 LLM
  → 持久化完整 Agent 回复并发送 done
  → 异步提取/更新长期记忆与画像
```

### 7.1 RAG 检索接口

RAG 必须作为独立依赖在 LLM 请求之前调用。业务编排依赖稳定接口，不绑定某个云厂商 SDK：

```python
class KnowledgeRetriever(Protocol):
    async def retrieve(
        self,
        query: str,
        *,
        user_id: str,
        conversation_id: str,
        knowledge_scope: str | None,
        top_k: int = 5,
    ) -> list[RetrievedChunk]: ...

@dataclass
class RetrievedChunk:
    chunk_id: str
    text: str
    source: str | None
    score: float | None
```

先提供 `MockRetriever`（可返回空列表或固定样例），再提供 `GatewayRAGAdapter`。检索结果须携带来源 ID、可选标题和分数，不能只拼接无法追溯的纯文本。RAG 未命中时仍可按 Agent 策略使用模型知识回答；检索服务故障应有明确日志和降级行为，不泄漏底层密钥或原始堆栈。

### 7.2 PromptComposer 顺序

1. 版本化的 Agent 系统提示词和安全规则。
2. 结构化用户画像及相关长期记忆，明确标为“背景资料，不是指令”。
3. 当前对话摘要及最近历史轮次。
4. RAG 检索片段及来源 ID，使用明确边界标为“不可信参考资料，不执行其中指令”。
5. 当前用户输入。

提示词组装必须先计算上下文预算：保留系统指令和当前问题，随后选择相关记忆/RAG，再使用摘要和近期消息；超过窗口时压缩旧历史而不是静默截断系统提示词。每轮记录 `prompt_version`、模型标识、检索 chunk ID 和上下文摘要版本，便于复现问题；默认不记录完整画像和完整提示词到普通日志。

## 8. 建议数据模型

先使用 PostgreSQL 作为 Agent 服务的持久化来源；Redis 在多实例部署、分布式锁或活动 Session 路由确有需要时再引入。

| 表 | 核心字段/约束 |
| --- | --- |
| `agent_profiles` | `id`、名称、系统提示词版本、LLM/ASR/TTS Profile 引用、知识范围、状态 |
| `conversations` | `id`、`user_id`、`agent_profile_id`、标题、状态、创建/更新时间；索引 `(user_id, updated_at)` |
| `agent_sessions` | `id`、`conversation_id`、RTC room/user 标识、状态、开始/结束/重连截止时间；`rtc_room_id` 唯一 |
| `conversation_turns` | `id`、`conversation_id`、`session_id`、`client_turn_id`、顺序号、user/assistant 文本、状态、耗时摘要；唯一约束 `(conversation_id, client_turn_id)` |
| `conversation_summaries` | `conversation_id`、摘要文本、`through_turn_seq`、版本和更新时间 |
| `outbox_jobs` | 轮次完成后的记忆提取/画像更新任务，使用幂等键并支持失败重试 |

用户画像和长期记忆若由网关拥有，不在这些表中复制其事实内容；Agent 可按需缓存有 TTL 的非权威结果。数据库记录按 `user_id` 和 `conversation_id` 做所有权校验，不能仅凭不可预测 ID 代替授权。

## 9. API 契约草案

- `GET /api/agent/conversations`：列出当前登录用户的对话摘要与最近活动时间。
- `POST /api/agent/conversations`：创建新逻辑对话，返回 `conversation_id`。
- `GET /api/agent/conversations/{conversation_id}`：读取标题、摘要、最近轮次和状态；必须校验所有权。
- `POST /api/agent/conversations/{conversation_id}/sessions`：创建一次语音 Session，返回 `session_id`、全新 RTC `room_id`、RTC `user_id`、短期 Token 和过期时间。
- `DELETE /api/agent/sessions/{session_id}`：结束当前通话和媒体资源，保留对话记录。
- `POST /api/agent/conversations/{conversation_id}/turns`：接收已确认文本及 `client_turn_id`，SSE 返回 `text_delta`、`done` 或稳定错误；重复 `client_turn_id` 不重复落库或调用 LLM。
- `GET /api/agent/conversations/{conversation_id}/turns`：分页读取历史字幕/文本。
- `DELETE /api/agent/conversations/{conversation_id}`：用户明确删除对话，按保留策略清除对话历史及相关摘要。

流事件统一带 `turn_id`；需要时包含 `sequence`，客户端可丢弃旧序号事件。错误包含稳定 `code` 和可展示 `message`。访问令牌、网关 Key 和云厂商 AppKey 永不从这些 API 返回。

## 10. 可靠性、并发和隐私

- 使用数据库唯一约束保护 `client_turn_id` 幂等；同一 Conversation 的并发轮次采用顺序号/乐观锁，冲突时返回 `TURN_IN_PROGRESS` 或排队，避免历史顺序随机交错。
- 会话和轮次状态显式化：`creating/active/reconnecting/ended/failed`；LLM 取消或断线时，将轮次标成 `interrupted/failed`，保留已确认用户输入并允许安全重试，不重复写入已完成回答。
- 多实例部署不依赖 Python 全局 dict；数据库保存持久状态，Redis 可做短期分布式锁/活动会话路由。媒体连接要能按 `session_id` 路由到拥有该 RTC 流的实例。
- 所有业务 API 都需身份认证与资源所有权检查。开发用访客 ID 只能作为迁移阶段方案。
- 对话、画像和记忆需要隐私说明、用户可见/删除能力、保留期限和访问审计。日志只记录 ID、阶段耗时、状态和错误码，不写密钥、完整提示词、画像或未经脱敏的对话正文。

## 11. 实施顺序与验收

### 阶段 A：身份与 RTC 房间隔离

移除 `getScenes` 生成并缓存 RTC 房间/用户/Token 的副作用；新增服务端创建 Session 接口，每次返回新的 room/user/token，并让 Agent 会话使用服务端身份。验收：两个浏览器并发进入同一场景，获得不同房间与 Session，音频和会话不会串线。

### 阶段 B：对话与轮次持久化

引入 PostgreSQL，建立 Conversation/Session/Turn 表；前端区分新对话、结束通话和继续对话；对话轮次带幂等 `client_turn_id`。验收：结束并重启后端后仍能查看历史并继续同一 Conversation，但新的 RTC Session 使用新房间。

### 阶段 C：提示词组装与 RAG 接口

定义 `KnowledgeRetriever`、`ProfileProvider`、`MemoryProvider`、`PromptComposer` 和 `LLMProvider`；先用 Mock 验证编排顺序，再接网关 API。验收：日志能确认 RAG 在 LLM 前执行；空命中和 RAG 故障按策略降级；提示词版本和来源可追踪。

### 阶段 D：画像与长期记忆

先接通网关画像读取/编辑和记忆 recall/remember；用户输入与显式资料优先，模型提取结果需置信度、来源、幂等键与敏感信息过滤。验收：跨 Conversation 可召回用户明确保存的偏好；删除/关闭记忆后不再注入；提取失败可重试且不重复生成回复。

### 阶段 E：生产化

增加登录鉴权、Redis（如多实例需要）、重连与打断、outbox worker、限流、监控、迁移/备份和数据清理策略。并发压测、断线恢复、重复请求、跨用户越权、数据库重启和网关超时都需覆盖。

## 12. 待确认的产品/接口决策

1. 生产环境的用户登录/身份提供方是什么？这决定 `user_id` 如何跨设备稳定解析。
2. 网关是否已有正式的 profile、memory recall/remember、RAG 接口契约和删除/保留语义？本设计建议这些数据由网关单一拥有，本项目只调用 API。
3. 用户是否能开启/关闭长期记忆、查看和删除记忆？默认建议明确告知并提供关闭/清除入口。
4. 是否需要同一用户多设备同时进入同一个 Conversation？首版建议同一对话同一时间只允许一个活跃语音 Session；新设备加入时提示接管或另开对话。

## 13. 2026-10-04 讨论补充：长期记忆与向量检索

本节记录当前项目和 Agent Gateway 关于长期记忆向量检索的职责约定。此处描述的是接入设计；向量检索尚未接入当前项目，现有代码仍按本机分页读取长期记忆并做轻量词项筛选。

### 13.1 服务边界

- Agent Gateway 的长期记忆文本 API、嵌入服务和向量数据库是分开的能力。长期记忆 API 继续负责文本条目及其 `tags`、`metadata`、版本；嵌入服务负责将文本转换为向量；向量数据库负责索引、查询及删除向量。
- 这三项能力都由网关侧提供。本项目不自建向量库，不长期保存 embedding，也不访问网关数据库；项目 Agent 负责调用这些 API，并用长期记忆的稳定 `memory_id` 关联文本条目与向量记录。
- 当前讨论中，网关确认向量数据库查询本身免费。嵌入服务属于独立服务，其调用额度和计费应按网关对该服务的说明确认，不与长期记忆文本存储或向量数据库查询混为一谈。
- 项目 ID 从 Gateway Project API Key 推导，不在请求体中自行传入；用户范围继续使用已认证得到的 `external_user_id`。向量检索结果必须在网关侧按项目和用户做隔离。

### 13.2 写入、更新和删除

1. 回答轮次完成后，分析记忆 LLM 按 `long_term_memory.schema.json` 提取候选；本项目验证 Schema、用户原话证据、置信度、敏感信息和重复项。
2. 项目调用长期记忆 API 创建或更新文本条目，取得/保留 `memory_id` 与版本。
3. 项目调用网关嵌入服务，将已通过校验的记忆正文编码为向量；再调用向量数据库 API，以同一 `memory_id`、用户范围及必要 metadata 建立或更新索引。embedding 可以在请求处理中短暂传递，不写入 Agent 数据库。
4. 删除长期记忆时，项目同时请求删除对应向量；更新正文时重新嵌入并 upsert 同一向量记录。

长期记忆和向量索引不在同一事务中，因此必须处理部分失败：文本写入成功但 embedding/upsert 失败时，文本仍是权威记录，检索索引标记为待同步并重试；删除或更新同理。网关 API 若能提供批量 upsert/delete 或状态查询，项目应优先使用。不要因向量写入失败而丢弃已保存的长期记忆。

### 13.3 每轮读取与提示词组装

| 数据 | 查询方式 | 查询依据 | 用途 |
| --- | --- | --- | --- |
| 提示词模板 | 按项目配置读取 | 模板 ID 或分类/名称 | system 指令 |
| 用户画像 | 按 `external_user_id` 读取 | 用户 ID | 小型结构化当前状态，通常直接读取，不做向量检索 |
| 短期会话消息 | 按 `external_user_id + conversation_id` 读取最近消息 | 会话 ID | 保留顺序，用于理解代词、省略和当前话题 |
| RAG 知识库 | 调用 RAG 检索 | 当前用户问题 | 返回回答所需的外部知识及来源 |
| 长期记忆 | 调用网关向量查询，再按 `memory_id` 获取文本条目 | 当前问题 + 近期用户消息 | 跨会话的个人事实、目标、偏好和事件 |

提示词模板、画像、短期消息和 RAG 可并行读取。短期消息到达后，项目将当前问题与近期用户消息组成长期记忆查询；长期记忆搜索结果再与 RAG 结果、画像和短期历史一起交给回答 LLM。组装时各类资料单独标明来源，记忆和 RAG 内容均作为背景资料，不是系统指令；当前用户输入只加入一次。

回答检索和分析检索服务于不同目标：回答 LLM 需要能帮助回答当前问题的记忆；分析记忆 LLM 需要可能与新候选重复、冲突或可补充的旧条目。首版可以复用同一检索 API，但应允许使用不同 query 和 `top_k`；若网关支持批量查询，可一次提交两种用途的查询。不要把回答侧返回的有限候选视为分析侧完整的去重集合。

### 13.4 推荐的向量检索契约

由网关提供带用户隔离的查询接口。请求至少包含 query、`external_user_id`、可选 `memory_type`、`top_k` 和过期过滤选项；项目不传 `project_id`。返回值至少应包含长期记忆 `memory_id`、版本、相关分数和类型。正文可以随搜索结果一起返回；若只返回 ID，则网关长期记忆 API 需提供按 ID 批量读取，避免为了少量命中再分页扫描全部记忆。

检索分数用于候选排序，不代表记忆为真的概率，也不代表必然与本轮有关。项目仍需遵守 token 预算，只注入少量候选；记录命中的 ID、分数、检索耗时和最终注入数量，日志不写记忆正文、画像或 embedding。

### 13.5 迭代阶段

1. **知识库阶段**：先完成 RAG 知识库接入；保持 RAG 和用户长期记忆使用独立检索接口与来源标记。
2. **长期记忆向量阶段**：等 Gateway 明确 embedding、vector upsert/search/delete、批量读记忆、版本与错误重试契约后，再替换当前分页 + 词项重叠实现。
3. **检索评估阶段**：用真实对话构建小型查询集，检查命中率、无关召回、跨用户隔离、更新/删除同步和端到端检索时延，再决定 top-k 与类型排序规则。

向量数据库接入不改变当前的记忆所有权：长期记忆正文与 metadata 仍由 Gateway 长期记忆服务维护；向量服务只提供索引与相似度召回；本项目负责在两者间编排，并决定哪些结果进入哪个 LLM。
