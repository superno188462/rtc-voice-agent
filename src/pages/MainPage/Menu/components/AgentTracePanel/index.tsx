import { useEffect, useMemo, useState } from 'react';
import { Button, Drawer, Input, Message, Spin, Tag } from '@arco-design/web-react';
import { IconMessage, IconMindMapping } from '@arco-design/web-react/icon';
import { useDispatch, useSelector } from 'react-redux';
import { RootState } from '@/store';
import { createAgentSession, streamAgentTurn, AgentEvent } from '@/lib/agentApi';
import {
  appendAgentTrace,
  beginAgentTrace,
  setAgentSessionId,
  setHistoryMsg,
  updateAIThinkState,
  updateAgentTraceAnswer,
} from '@/store/slices/room';
import styles from './index.module.less';

const STAGE_LABELS: Record<string, string> = {
  context: '上下文读取',
  subquery: '检索问题改写',
  retrieval: 'Gateway 向量检索',
  answer: '回答生成',
  memory_analysis: '画像与长期记忆分析',
};

const STATUS_LABELS: Record<string, string> = {
  running: '进行中',
  completed: '完成',
  degraded: '降级继续',
  skipped: '直接检索',
  failed: '失败',
};

function asObject(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function formatValue(value: unknown): string {
  if (typeof value === 'string') return value;
  return JSON.stringify(value, null, 2) || '';
}

function AgentTracePanel() {
  const dispatch = useDispatch();
  const room = useSelector((state: RootState) => state.room);
  const [visible, setVisible] = useState(false);
  const [question, setQuestion] = useState('');
  const [sending, setSending] = useState(false);
  const [selectedTurnId, setSelectedTurnId] = useState('');
  const selectedTurn = useMemo(
    () =>
      room.agentTraceTurns.find((turn) => turn.turnId === selectedTurnId) ||
      room.agentTraceTurns.at(-1),
    [room.agentTraceTurns, selectedTurnId]
  );
  const latestTurnId = room.agentTraceTurns.at(-1)?.turnId;
  useEffect(() => {
    if (latestTurnId) setSelectedTurnId(latestTurnId);
  }, [latestTurnId]);

  const sendText = async () => {
    const text = question.trim();
    if (!text || sending) return;
    if (room.isJoined && !room.agentSessionId) {
      Message.info('语音会话正在初始化，请稍候。');
      return;
    }
    if (!room.scene) {
      Message.warning('场景仍在加载，请稍后再试。');
      return;
    }
    setSending(true);
    const turnId = crypto.randomUUID();
    const ownerId = room.localUser.userId || '我';
    const assistantName = room.sceneConfigMap[room.scene]?.botName || '助手';
    try {
      let sessionId = room.agentSessionId;
      if (!sessionId) {
        const sessionSuffix = crypto.randomUUID();
        sessionId = await createAgentSession(
          room.scene,
          `text-${sessionSuffix}`,
          `text-${sessionSuffix}`
        );
        dispatch(setAgentSessionId(sessionId));
      }
      dispatch(beginAgentTrace({ turnId, question: text }));
      setSelectedTurnId(turnId);
      dispatch(setHistoryMsg({ text, user: ownerId, paragraph: true, definite: true }));
      dispatch(updateAIThinkState({ isAIThinking: true }));
      let answer = '';
      await streamAgentTurn(
        sessionId,
        text,
        turnId,
        (event: AgentEvent) => {
          if (event.type === 'trace') {
            dispatch(
              appendAgentTrace({
                type: 'trace',
                turn_id: event.turn_id || turnId,
                stage: event.stage || 'unknown',
                status: event.status || 'running',
                summary: event.summary || '',
                details: event.details,
                at: event.at,
              })
            );
          } else if (event.type === 'text_delta' && event.text) {
            answer += event.text;
            dispatch(updateAgentTraceAnswer({ turnId, answer }));
            dispatch(
              setHistoryMsg({
                text: answer,
                user: assistantName,
                paragraph: false,
                definite: false,
              })
            );
          } else if (event.type === 'llm_done' && answer) {
            dispatch(updateAgentTraceAnswer({ turnId, answer }));
            dispatch(
              setHistoryMsg({ text: answer, user: assistantName, paragraph: true, definite: true })
            );
          }
        },
        false
      );
      setQuestion('');
    } catch (error) {
      Message.error(`文本提问失败：${String(error).replace(/^Error: /, '')}`);
      dispatch(
        appendAgentTrace({
          type: 'trace',
          turn_id: turnId,
          stage: 'answer',
          status: 'failed',
          summary: '本轮请求未完成',
          details: { error: String(error).replace(/^Error: /, '') },
        })
      );
    } finally {
      dispatch(updateAIThinkState({ isAIThinking: false }));
      setSending(false);
    }
  };

  const events = selectedTurn?.events || [];
  const retrieval = events.find((event) => event.stage === 'retrieval');
  const retrievalHits = retrieval?.details?.hits;
  const hits = Array.isArray(retrievalHits) ? retrievalHits : [];

  return (
    <>
      <Button
        className={styles.openButton}
        icon={<IconMindMapping />}
        onClick={() => setVisible(true)}
      >
        本轮处理链路
      </Button>
      <Drawer
        title="Agent 对话处理链路"
        visible={visible}
        placement="right"
        width={520}
        onCancel={() => setVisible(false)}
        footer={null}
      >
        <div className={styles.panel}>
          <div className={styles.inputArea}>
            <Input.TextArea
              value={question}
              onChange={setQuestion}
              placeholder="输入文本直接提问；也可以加入语音房间后提问"
              autoSize={{ minRows: 2, maxRows: 5 }}
              maxLength={4000}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey) {
                  event.preventDefault();
                  void sendText();
                }
              }}
            />
            <Button
              type="primary"
              icon={<IconMessage />}
              loading={sending}
              disabled={!question.trim()}
              onClick={() => void sendText()}
            >
              发送文本
            </Button>
          </div>
          <div className={styles.disclaimer}>
            这里展示实际执行阶段和检索资料，不展示模型隐藏推理内容。
          </div>

          {room.agentTraceTurns.length > 1 && (
            <div className={styles.turnList} aria-label="最近处理轮次">
              {room.agentTraceTurns.map((turn, index) => (
                <Button
                  key={turn.turnId}
                  size="mini"
                  type={turn.turnId === selectedTurn?.turnId ? 'primary' : 'secondary'}
                  onClick={() => setSelectedTurnId(turn.turnId)}
                >
                  第 {index + 1} 轮
                </Button>
              ))}
            </div>
          )}

          {selectedTurn ? (
            <>
              <div className={styles.question}>
                <span>用户问题</span>
                <p>{selectedTurn.question}</p>
              </div>
              <div className={styles.answer}>
                <span>回答 LLM 输出</span>
                <p>{selectedTurn.answer || (sending ? '正在生成…' : '暂无回答内容')}</p>
              </div>
              <div className={styles.timeline}>
                {events.map((event) => {
                  const details = asObject(event.details);
                  return (
                    <details className={styles.stage} key={event.stage}>
                      <summary>
                        <span className={styles.stageName}>
                          {STAGE_LABELS[event.stage] || event.stage}
                        </span>
                        <Tag
                          color={
                            event.status === 'failed'
                              ? 'red'
                              : event.status === 'degraded'
                              ? 'orange'
                              : event.status === 'completed'
                              ? 'green'
                              : event.status === 'skipped'
                              ? 'gray'
                              : 'blue'
                          }
                        >
                          {STATUS_LABELS[event.status] || event.status}
                        </Tag>
                      </summary>
                      <p>{event.summary}</p>
                      {typeof details.duration_ms === 'number' ? (
                        <div className={styles.duration}>耗时：{details.duration_ms} ms</div>
                      ) : null}
                      {event.stage === 'retrieval' && hits.length > 0 ? (
                        <div className={styles.hits}>
                          {hits.map((rawHit, index) => {
                            const hit = asObject(rawHit);
                            const metadata = asObject(hit.metadata);
                            return (
                              <details className={styles.hit} key={String(hit.id || index)}>
                                <summary>
                                  <strong>
                                    {String(hit.title || metadata.path || `命中 ${index + 1}`)}
                                  </strong>
                                  <span>
                                    {typeof hit.score === 'number'
                                      ? hit.score.toFixed(3)
                                      : '无分数'}
                                  </span>
                                </summary>
                                <div className={styles.hitMeta}>
                                  <div>切片 ID：{String(hit.id || '-')}</div>
                                  <div>
                                    父块 ID：{String(hit.parent_id || metadata.parent_id || '无')}
                                  </div>
                                  <div>来源：{String(metadata.path || metadata.source || '-')}</div>
                                  <div>切片序号：{String(metadata.chunk_index ?? '-')}</div>
                                </div>
                                <h4>命中子片</h4>
                                <pre>{String(hit.content || '')}</pre>
                                {hit.parent_content ? (
                                  <>
                                    <h4>父块上下文</h4>
                                    <pre>{String(hit.parent_content)}</pre>
                                  </>
                                ) : null}
                                <details className={styles.metadata}>
                                  <summary>metadata</summary>
                                  <pre>{formatValue(metadata)}</pre>
                                </details>
                              </details>
                            );
                          })}
                        </div>
                      ) : null}
                      {event.stage === 'context' && (
                        <>
                          <div className={styles.detailsGrid}>
                            <span>画像字段：{String(details.profile_fields ?? 0)}</span>
                            <span>长期记忆：{String(details.long_term_memories ?? 0)} 条</span>
                            <span>短期消息：{String(details.short_term_messages ?? 0)} 条</span>
                            <span>
                              系统提示词：{details.system_prompt_loaded ? '已加载' : '未加载'}
                            </span>
                          </div>
                          <details className={styles.contextPayload}>
                            <summary>查看本轮选中的画像、长期记忆和短期对话</summary>
                            <h4>用户画像</h4>
                            <pre>{formatValue(details.profile || {})}</pre>
                            <h4>长期记忆</h4>
                            {Array.isArray(details.selected_memories) &&
                            details.selected_memories.length ? (
                              <ul>
                                {details.selected_memories.map((rawMemory, index) => {
                                  const memory = asObject(rawMemory);
                                  return <li key={index}>{String(memory.content || '')}</li>;
                                })}
                              </ul>
                            ) : (
                              <p>本轮没有选中长期记忆。</p>
                            )}
                            <h4>短期对话</h4>
                            {Array.isArray(details.recent_messages) &&
                            details.recent_messages.length ? (
                              <ul>
                                {details.recent_messages.map((rawMessage, index) => {
                                  const message = asObject(rawMessage);
                                  return (
                                    <li key={index}>
                                      <b>{String(message.role || '')}：</b>
                                      {String(message.content || '')}
                                    </li>
                                  );
                                })}
                              </ul>
                            ) : (
                              <p>本轮没有已有短期对话。</p>
                            )}
                          </details>
                        </>
                      )}
                      {event.stage === 'subquery' && typeof details.query === 'string' && (
                        <div className={styles.query}>检索问题：{details.query}</div>
                      )}
                      {event.stage === 'memory_analysis' && (
                        <>
                          <div className={styles.detailsGrid}>
                            <span>
                              画像候选/接受/保存：{String(details.profile_candidates ?? 0)} /{' '}
                              {String(details.profile_accepted ?? 0)} /{' '}
                              {String(details.profile_saved ?? 0)}
                            </span>
                            <span>
                              记忆候选/接受/新增：{String(details.memory_candidates ?? 0)} /{' '}
                              {String(details.memory_accepted ?? 0)} /{' '}
                              {String(details.memories_saved ?? 0)}
                            </span>
                            <span>已更新记忆：{String(details.memories_updated ?? 0)}</span>
                            <span>
                              重复/校验拒绝：{String(details.memories_duplicate ?? 0)} /{' '}
                              {String(details.memories_rejected ?? 0)}
                            </span>
                            <span>
                              画像已涵盖：{String(details.memory_rejected_profile_overlap ?? 0)} 条
                            </span>
                          </div>
                          <details className={styles.contextPayload}>
                            <summary>查看分析 LLM 输出与保存/向量同步结果</summary>
                            <h4>分析 LLM 接收结果</h4>
                            <pre>{formatValue(details.llm_output || {})}</pre>
                            <h4>项目校验及 Gateway 写入结果</h4>
                            <pre>{formatValue(details.persistence_result || {})}</pre>
                          </details>
                        </>
                      )}
                      {event.status === 'failed' || event.status === 'degraded' ? (
                        <div className={styles.error}>
                          诊断：
                          {String(
                            details.error ||
                              (Array.isArray(details.errors) ? details.errors.join(', ') : '')
                          )}
                        </div>
                      ) : null}
                    </details>
                  );
                })}
              </div>
            </>
          ) : (
            <div className={styles.empty}>提交文本问题或语音提问后，这里会显示本轮各阶段。</div>
          )}
          {sending ? <Spin className={styles.sending} /> : null}
        </div>
      </Drawer>
    </>
  );
}

export default AgentTracePanel;
