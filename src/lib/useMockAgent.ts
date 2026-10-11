/**
 * RTC audio to the self-orchestrated Agent speech path.
 * RTC provides local PCM frames; the backend proxies Seed ASR 2.0 and returns
 * structured transcript events. Finalized text is sent to the configured LLM service.
 */
import { useEffect, useRef } from 'react';
import { Message } from '@arco-design/web-react';
import { useDispatch, useSelector } from 'react-redux';
import { StreamIndex } from '@volcengine/rtc';
import { RootState } from '@/store';
import RtcClient from '@/lib/RtcClient';
import { closeTtsAudioContext, getTtsAudioContext } from '@/lib/ttsAudio';
import { AIGC_PROXY_HOST } from '@/config';
import { AgentEvent as ApiAgentEvent, streamAgentTurn } from '@/lib/agentApi';
import {
  clearHistoryMsg,
  setHistoryMsg,
  updateAIGCState,
  updateASRPartialText,
  updateASRStatus,
  updateAIThinkState,
  updateAITalkState,
  beginAgentTrace,
  appendAgentTrace,
  setAgentSessionId,
  updateAgentTraceAnswer,
} from '@/store/slices/room';

type AgentEvent = ApiAgentEvent & {
  utterance_id?: string;
  request_id?: string;
  /** ASR 上游连接代次；静默回收后的新连接会递增。 */
  generation?: number;
  /** asr_ready 是否表示一次空闲回收后的重连。 */
  reconnected?: boolean;
  /** 错误已经提示时，用于静默清理未完成轮次，避免重复 toast。 */
  suppress_warning?: boolean;
};
type VoiceTurn = {
  turnId: string;
  speechStartAt: number;
  speechEndAt?: number;
  firstPartialAt?: number;
};

function getAgentErrorMessage(code: string): string {
  const messages: Record<string, string> = {
    CONTEXT_SERVICE_NOT_CONFIGURED: '对话上下文服务尚未配置，请联系管理员检查后端环境变量。',
    CONTEXT_AUTH_FAILED: '对话上下文服务鉴权失败，请检查项目 API Key。',
    CONTEXT_RESOURCE_NOT_FOUND: '网关中找不到配置的提示词模板或会话资源。',
    CONTEXT_SERVICE_TIMEOUT: '对话上下文服务响应超时，请稍后重试。',
    CONTEXT_APPEND_UNCERTAIN: '本轮回复已生成，但记忆写入状态未能确认；请稍后继续，避免重复提交。',
    CONTEXT_VERSION_CONFLICT: '用户画像正在被更新，请稍后重试。',
    TURN_ALREADY_COMPLETED: '这轮对话已经处理完成。',
    TURN_ALREADY_IN_PROGRESS: '这轮对话仍在处理中。',
    TURN_ID_REUSED_WITH_DIFFERENT_INPUT: '轮次标识冲突，请重新说一次。',
  };
  return messages[code] || `Agent 请求失败：${code}`;
}

const VAD_RMS_THRESHOLD = 0.012;
const VAD_SILENCE_MS = 600;
// 保留开口前及静音判停后的少量 PCM，避免 VAD 阈值切掉辅音或 ASR 判停尾音。
const VAD_PRE_ROLL_MS = 200;
const VAD_TRAILING_SILENCE_MS = 1000;

function logVoiceTrace(event: string, turnId: string, details: Record<string, unknown> = {}) {
  console.info(
    `[trace][voice] ${JSON.stringify({
      event,
      turn_id: turnId,
      at_ms: Math.round(performance.now()),
      ...details,
    })}`
  );
}

/** 计算 PCM16 音频块的 RMS 音量，用于估算本地语音起止时间。 */
function getPcmRms(pcm: ArrayBuffer): number {
  const samples = new DataView(pcm);
  const sampleCount = Math.floor(pcm.byteLength / 2);
  if (!sampleCount) return 0;
  let sumSquares = 0;
  for (let index = 0; index < sampleCount; index += 1) {
    const sample = samples.getInt16(index * 2, true) / 32768;
    sumSquares += sample * sample;
  }
  return Math.sqrt(sumSquares / sampleCount);
}

async function readLlmTurn(
  sessionId: string,
  text: string,
  turnId: string,
  onEvent: (event: AgentEvent) => void,
  asrFinalAt: number,
  speechStartAt?: number,
  speechEndAt?: number
): Promise<void> {
  const clientStartedAt = performance.now();
  let firstTextAt: number | undefined;
  logVoiceTrace('llm_client_request_start', turnId, {
    asr_final_to_request_ms: Math.round(clientStartedAt - asrFinalAt),
  });
  await streamAgentTurn(sessionId, text, turnId, (event) => {
    if (event.type === 'text_delta' && event.text && firstTextAt === undefined) {
      firstTextAt = performance.now();
      logVoiceTrace('llm_client_first_text', turnId, {
        elapsed_ms: Math.round(firstTextAt - clientStartedAt),
        asr_final_to_first_text_ms: Math.round(firstTextAt - asrFinalAt),
        speech_start_to_client_first_text_ms:
          speechStartAt === undefined ? null : Math.round(firstTextAt - speechStartAt),
        speech_end_to_client_first_text_ms:
          speechEndAt === undefined ? null : Math.round(firstTextAt - speechEndAt),
      });
    }
    onEvent(event as AgentEvent);
  }, true, (elapsedMs) => {
    logVoiceTrace('llm_client_response_headers', turnId, { elapsed_ms: elapsedMs });
  });
  logVoiceTrace('llm_client_stream_end', turnId, {
    elapsed_ms: Math.round(performance.now() - clientStartedAt),
  });
}

/** 将 RTC Float32 音频降混并转成 ASR 所需的 16 kHz PCM S16LE。 */
function toPcm16k(frame: {
  channels: Float32Array[];
  channelCount: number;
  sampleRate: number;
}): ArrayBuffer {
  const channels = frame.channels.slice(0, frame.channelCount);
  const frameLength = channels[0]?.length || 0;
  const sourceRate = frame.sampleRate || 48000;
  const targetLength = Math.floor((frameLength * 16000) / sourceRate);
  const output = new ArrayBuffer(targetLength * 2);
  const view = new DataView(output);
  for (let index = 0; index < targetLength; index += 1) {
    const sourceIndex = Math.min(frameLength - 1, Math.floor((index * sourceRate) / 16000));
    let sample = 0;
    channels.forEach((channel) => {
      sample += channel[sourceIndex] || 0;
    });
    sample /= Math.max(1, channels.length);
    const clipped = Math.max(-1, Math.min(1, sample));
    view.setInt16(index * 2, clipped < 0 ? clipped * 32768 : clipped * 32767, true);
  }
  return output;
}

export default function useMockAgent(): void {
  const dispatch = useDispatch();
  const room = useSelector((state: RootState) => state.room);
  const sessionId = useRef('');
  const socketRef = useRef<WebSocket | null>(null);
  const llmBusy = useRef(false);
  const processedFinalUtteranceIds = useRef(new Set<string>());
  const turnQueue = useRef<Promise<void>>(Promise.resolve());
  const publishAudioRef = useRef(Boolean(room.localUser.publishAudio));
  const botNameRef = useRef(room.sceneConfigMap[room.scene]?.botName || 'assistant');
  publishAudioRef.current = Boolean(room.localUser.publishAudio);
  botNameRef.current = room.sceneConfigMap[room.scene]?.botName || 'assistant';

  useEffect(
    () => () => {
      void closeTtsAudioContext();
    },
    []
  );

  useEffect(() => {
    let cancelled = false;
    let createdSessionId = '';
    let localSocket: WebSocket | null = null;
    let audioWatchdog: number | undefined;
    let audioFramesSent = 0;
    let asrReady = false;
    let audioWatchdogExpired = false;
    let activeSpeechTurn: VoiceTurn | null = null;
    let silenceMs = 0;
    let silenceStartedAt: number | undefined;
    let trailingSilenceMs = 0;
    let forwardingSpeechAudio = false;
    let forwardedAudioMs = 0;
    let forwardedAudioBytes = 0;
    let suppressedAudioMs = 0;
    let suppressedAudioBytes = 0;
    let lastSuppressedLogAt = 0;
    const preRollFrames: Array<{ pcm: ArrayBuffer; durationMs: number }> = [];
    let preRollMs = 0;
    const pendingSpeechTurns: VoiceTurn[] = [];
    if (!room.isJoined) void closeTtsAudioContext();
    const disableAudioCallback = () => {
      if (RtcClient.engine) {
        RtcClient.engine.setAudioFrameCallback(StreamIndex.STREAM_INDEX_MAIN, undefined, undefined);
      }
    };

    const createSession = async () => {
      if (!room.isJoined || !room.roomId || !room.localUser.userId) return;
      dispatch(updateASRStatus('connecting'));
      try {
        const reuseTextSession = Boolean(room.agentSessionId);
        if (reuseTextSession) {
          createdSessionId = room.agentSessionId;
        } else {
          const response = await fetch(`${AIGC_PROXY_HOST}/api/agent/sessions`, {
            method: 'POST',
            credentials: 'include',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({
              scene_id: room.scene,
              room_id: room.roomId,
              user_id: room.localUser.userId,
            }),
          });
          const data = await response.json();
          if (!response.ok) throw new Error(data?.error?.code || `HTTP ${response.status}`);
          createdSessionId = data.session_id;
        }
        if (cancelled) {
          if (!reuseTextSession) {
            await fetch(`${AIGC_PROXY_HOST}/api/agent/sessions/${createdSessionId}`, {
              method: 'DELETE',
              credentials: 'include',
            });
          }
          return;
        }

        sessionId.current = createdSessionId;
        dispatch(setAgentSessionId(createdSessionId));
        dispatch(clearHistoryMsg());
        try {
          const historyResponse = await fetch(
            `${AIGC_PROXY_HOST}/api/agent/sessions/${createdSessionId}/messages?limit=50`,
            { credentials: 'include' }
          );
          if (!historyResponse.ok) {
            const historyError = await historyResponse.json().catch(() => ({}));
            throw new Error(historyError?.detail?.code || `HTTP ${historyResponse.status}`);
          }
          const historyData = await historyResponse.json();
          const ownerId = room.localUser.userId || '';
          const assistantName = botNameRef.current;
          (historyData?.messages || []).forEach((message: { role: string; content: string }) => {
            if (message.role === 'user') {
              dispatch(
                setHistoryMsg({ text: message.content, user: ownerId, paragraph: true, definite: true })
              );
            } else if (message.role === 'assistant') {
              dispatch(
                setHistoryMsg({
                  text: message.content,
                  user: assistantName,
                  paragraph: true,
                  definite: true,
                })
              );
            }
          });
        } catch (error) {
          Message.error(`读取历史对话失败：${getAgentErrorMessage(String(error).replace(/^Error: /, ''))}`);
        }
        if (cancelled) {
          await fetch(`${AIGC_PROXY_HOST}/api/agent/sessions/${createdSessionId}`, {
            method: 'DELETE',
            credentials: 'include',
          });
          return;
        }
        const socketUrl = AIGC_PROXY_HOST.replace(/^http/, 'ws').replace(/\/$/, '');
        localSocket = new WebSocket(`${socketUrl}/api/agent/sessions/${createdSessionId}/asr`);
        socketRef.current = localSocket;
        localSocket.binaryType = 'arraybuffer';
        const sendTraceMarker = (event: string, turnId: string) => {
          if (localSocket?.readyState === WebSocket.OPEN) {
            localSocket.send(JSON.stringify({ type: 'trace', event, turn_id: turnId }));
          }
        };
        localSocket.onopen = () => {
          if (cancelled) {
            localSocket?.close();
            return;
          }
          try {
            // 8,192 samples at 48 kHz gives approximately 171 ms frames, within SDK limits.
            RtcClient.engine.setAudioFrameCallback(
              StreamIndex.STREAM_INDEX_MAIN,
              undefined,
              (frame) => {
                if (
                  !cancelled &&
                  publishAudioRef.current &&
                  localSocket?.readyState === WebSocket.OPEN
                ) {
                  const pcm = toPcm16k(frame);
                  if (pcm.byteLength) {
                    const now = performance.now();
                    const frameDurationMs = (pcm.byteLength / 2 / 16000) * 1000;
                    const rms = getPcmRms(pcm);
                    const sendPcm = (chunk: ArrayBuffer) => {
                      if (localSocket?.readyState !== WebSocket.OPEN) return;
                      localSocket.send(chunk);
                      audioFramesSent += 1;
                      const chunkDurationMs = (chunk.byteLength / 2 / 16000) * 1000;
                      forwardedAudioMs += chunkDurationMs;
                      forwardedAudioBytes += chunk.byteLength;
                      if (audioFramesSent === 1) {
                        if (audioWatchdog) window.clearTimeout(audioWatchdog);
                        audioWatchdog = undefined;
                        if (asrReady) dispatch(updateASRStatus('listening'));
                      }
                    };
                    let resumedSpeech = false;
                    if (!activeSpeechTurn) {
                      preRollFrames.push({ pcm, durationMs: frameDurationMs });
                      preRollMs += frameDurationMs;
                      while (preRollMs > VAD_PRE_ROLL_MS && preRollFrames.length > 1) {
                        const removed = preRollFrames.shift();
                        if (removed) preRollMs -= removed.durationMs;
                      }
                    }
                    if (!activeSpeechTurn && rms >= VAD_RMS_THRESHOLD) {
                      const turnId =
                        typeof crypto !== 'undefined' && 'randomUUID' in crypto
                          ? crypto.randomUUID()
                          : `turn-${Date.now()}-${Math.random().toString(16).slice(2)}`;
                      activeSpeechTurn = { turnId, speechStartAt: now };
                      pendingSpeechTurns.push(activeSpeechTurn);
                      resumedSpeech = !forwardingSpeechAudio;
                      forwardingSpeechAudio = true;
                      trailingSilenceMs = 0;
                      if (resumedSpeech) {
                        logVoiceTrace('speech_audio_resumed', turnId, {
                          forwarded_audio_ms: Math.round(forwardedAudioMs),
                          forwarded_audio_bytes: forwardedAudioBytes,
                        });
                      }
                      preRollFrames.forEach(({ pcm: bufferedPcm }) => sendPcm(bufferedPcm));
                      preRollFrames.length = 0;
                      preRollMs = 0;
                      silenceMs = 0;
                      silenceStartedAt = undefined;
                      logVoiceTrace('speech_start', turnId, { vad_rms: Number(rms.toFixed(4)) });
                      sendTraceMarker('speech_start', turnId);
                    } else if (activeSpeechTurn && rms < VAD_RMS_THRESHOLD) {
                      trailingSilenceMs += frameDurationMs;
                      if (silenceStartedAt === undefined) silenceStartedAt = now;
                      silenceMs += frameDurationMs;
                      if (silenceMs >= VAD_SILENCE_MS) {
                        activeSpeechTurn.speechEndAt = silenceStartedAt;
                        logVoiceTrace('speech_end', activeSpeechTurn.turnId, {
                          at_ms: Math.round(activeSpeechTurn.speechEndAt),
                          detected_at_ms: Math.round(now),
                          speech_duration_ms: Math.round(
                            activeSpeechTurn.speechEndAt - activeSpeechTurn.speechStartAt
                          ),
                          vad_hangover_ms: Math.round(now - silenceStartedAt),
                        });
                        sendTraceMarker('speech_end', activeSpeechTurn.turnId);
                        const completedTurnId = activeSpeechTurn.turnId;
                        activeSpeechTurn = null;
                        silenceMs = 0;
                        silenceStartedAt = undefined;
                        if (trailingSilenceMs >= VAD_TRAILING_SILENCE_MS) {
                          forwardingSpeechAudio = false;
                          logVoiceTrace('silence_audio_paused', completedTurnId, {
                            trailing_silence_ms: Math.round(trailingSilenceMs),
                            forwarded_audio_ms: Math.round(forwardedAudioMs),
                          });
                        }
                      }
                    } else if (activeSpeechTurn) {
                      silenceMs = 0;
                      silenceStartedAt = undefined;
                      trailingSilenceMs = 0;
                    } else if (forwardingSpeechAudio && rms < VAD_RMS_THRESHOLD) {
                      trailingSilenceMs += frameDurationMs;
                      if (trailingSilenceMs >= VAD_TRAILING_SILENCE_MS) {
                        forwardingSpeechAudio = false;
                        const lastTurnId = pendingSpeechTurns[pendingSpeechTurns.length - 1]?.turnId;
                        if (lastTurnId) {
                          logVoiceTrace('silence_audio_paused', lastTurnId, {
                            trailing_silence_ms: Math.round(trailingSilenceMs),
                            forwarded_audio_ms: Math.round(forwardedAudioMs),
                          });
                        }
                      }
                    }

                    if (resumedSpeech) {
                      // 活跃轮次分支在恢复语音时已发送预录音，避免当前帧重复发送。
                    } else if (activeSpeechTurn || forwardingSpeechAudio) {
                      sendPcm(pcm);
                    } else {
                      suppressedAudioMs += frameDurationMs;
                      suppressedAudioBytes += pcm.byteLength;
                      if (now - lastSuppressedLogAt >= 5000) {
                        lastSuppressedLogAt = now;
                        const lastTurnId = pendingSpeechTurns[pendingSpeechTurns.length - 1]?.turnId;
                        logVoiceTrace('silence_pcm_suppressed', lastTurnId || 'idle', {
                          suppressed_audio_ms: Math.round(suppressedAudioMs),
                          suppressed_audio_bytes: suppressedAudioBytes,
                          forwarded_audio_ms: Math.round(forwardedAudioMs),
                          forwarded_audio_bytes: forwardedAudioBytes,
                        });
                      }
                    }
                  }
                }
              },
              8192
            );
            audioWatchdog = window.setTimeout(() => {
              audioWatchdog = undefined;
              audioWatchdogExpired = true;
              if (!cancelled && audioFramesSent === 0) dispatch(updateASRStatus('no_audio'));
            }, 3000);
          } catch (error) {
            Message.error(`RTC 音频帧读取失败：${String(error)}`);
            dispatch(updateASRStatus('error'));
            localSocket?.close();
          }
        };
        localSocket.onmessage = (message) => {
          let event: AgentEvent;
          try {
            event = JSON.parse(String(message.data)) as AgentEvent;
          } catch {
            return;
          }
          if (event.type === 'error') {
            if (event.turn_id) {
              const failedTurnIndex = pendingSpeechTurns.findIndex(
                (turn) => turn.turnId === event.turn_id
              );
              if (failedTurnIndex >= 0) pendingSpeechTurns.splice(failedTurnIndex, 1);
            }
            logVoiceTrace('asr_upstream_error', event.turn_id || 'unassigned', {
              code: event.code || 'ASR_UPSTREAM_FAILED',
              request_id: event.request_id,
              generation: event.generation,
              message: event.message,
            });
            Message.error(
              `ASR 连接失败：${event.code || 'ASR_UPSTREAM_FAILED'}${
                event.message ? `（${event.message}）` : ''
              }`
            );
            dispatch(updateASRStatus('error'));
            dispatch(updateAIGCState({ isAIGCEnable: false }));
            dispatch(updateASRPartialText(''));
            return;
          }
          if (event.type === 'asr_turn_incomplete') {
            if (event.turn_id) {
              const pendingIndex = pendingSpeechTurns.findIndex(
                (turn) => turn.turnId === event.turn_id
              );
              if (pendingIndex >= 0) pendingSpeechTurns.splice(pendingIndex, 1);
              logVoiceTrace('asr_turn_incomplete', event.turn_id, {
                code: event.code,
                generation: event.generation,
              });
            }
            dispatch(updateASRPartialText(''));
            if (!event.suppress_warning) {
              Message.warning('这句话的语音识别未能完成，请再说一次。');
            }
            return;
          }
          if (event.type === 'asr_ready') {
            asrReady = true;
            dispatch(
              updateASRStatus(
                audioFramesSent > 0 ? 'listening' : audioWatchdogExpired ? 'no_audio' : 'connecting'
              )
            );
            dispatch(updateAIGCState({ isAIGCEnable: true }));
            return;
          }
          if (!event.text) return;
          const userId = room.localUser.userId || '';
          if (event.type === 'asr_partial') {
            const speechTurn = pendingSpeechTurns[0];
            if (speechTurn && speechTurn.firstPartialAt === undefined) {
              speechTurn.firstPartialAt = performance.now();
              logVoiceTrace('asr_first_partial', speechTurn.turnId, {
                elapsed_from_speech_start_ms: Math.round(
                  speechTurn.firstPartialAt - speechTurn.speechStartAt
                ),
              });
            }
            dispatch(updateASRPartialText(event.text));
          } else if (event.type === 'asr_final') {
            dispatch(updateASRPartialText(''));
            if (event.utterance_id && processedFinalUtteranceIds.current.has(event.utterance_id)) {
              return;
            }
            if (event.utterance_id) processedFinalUtteranceIds.current.add(event.utterance_id);
            const finalizedAt = performance.now();
            const reportedTurnIndex = event.turn_id
              ? pendingSpeechTurns.findIndex((turn) => turn.turnId === event.turn_id)
              : -1;
            const hasReportedTurn = reportedTurnIndex >= 0;
            const shouldUseOldestPending =
              !hasReportedTurn && pendingSpeechTurns.length > 0;
            const pendingTurnIndex = hasReportedTurn
              ? reportedTurnIndex
              : shouldUseOldestPending
                ? 0
                : -1;
            const speechTurn =
              pendingTurnIndex >= 0 ? pendingSpeechTurns.splice(pendingTurnIndex, 1)[0] : undefined;
            const turnId = speechTurn?.turnId || event.turn_id || `asr-${Date.now()}`;
            if (event.turn_id && speechTurn && event.turn_id !== speechTurn.turnId) {
              logVoiceTrace('asr_turn_id_mismatch', turnId, {
                reported_turn_id: event.turn_id,
                selected_turn_id: speechTurn.turnId,
                generation: event.generation,
                pending_turn_count: pendingSpeechTurns.length + 1,
              });
            }
            // Use the client-selected voice turn as the trace authority. The server
            // may have assigned a stale/unmatched upstream ID; this marker repairs
            // the backend trace without changing the ASR text or request payload.
            sendTraceMarker('asr_final', turnId);
            logVoiceTrace('asr_final', turnId, {
              utterance_id: event.utterance_id,
              speech_to_final_ms: speechTurn
                ? Math.round(finalizedAt - speechTurn.speechStartAt)
                : null,
              speech_end_to_final_ms: speechTurn?.speechEndAt
                ? Math.round(finalizedAt - speechTurn.speechEndAt)
                : null,
              speech_end_observed: Boolean(speechTurn?.speechEndAt),
            });
            dispatch(
              setHistoryMsg({ text: event.text, user: userId, paragraph: true, definite: true })
            );
            const botName = botNameRef.current;
            const finalText = event.text;
            const turnSessionId = createdSessionId;
            dispatch(beginAgentTrace({ turnId, question: finalText }));
            turnQueue.current = turnQueue.current
              .then(async () => {
                llmBusy.current = true;
                let answer = '';
                let answerFinalized = false;
                let ttsNextPlaybackAt = 0;
                let ttsHadAudio = false;
                let ttsErrorShown = false;
                let pcmCarry = new Uint8Array(0);
                dispatch(updateAIThinkState({ isAIThinking: true }));
                await readLlmTurn(turnSessionId, finalText, turnId, (llmEvent) => {
                  if (llmEvent.type === 'trace') {
                    dispatch(appendAgentTrace({
                      type: 'trace',
                      turn_id: llmEvent.turn_id || turnId,
                      stage: llmEvent.stage || 'unknown',
                      status: llmEvent.status || 'running',
                      summary: llmEvent.summary || '',
                      details: llmEvent.details,
                      at: llmEvent.at,
                    }));
                  } else if (llmEvent.type === 'text_delta' && llmEvent.text) {
                    answer += llmEvent.text;
                    dispatch(updateAgentTraceAnswer({ turnId, answer }));
                    dispatch(
                      setHistoryMsg({ text: answer, user: botName, paragraph: false, definite: false })
                    );
                  } else if (llmEvent.type === 'llm_done') {
                    if (answer) {
                      dispatch(updateAgentTraceAnswer({ turnId, answer }));
                      dispatch(
                        setHistoryMsg({ text: answer, user: botName, paragraph: true, definite: true })
                      );
                      answerFinalized = true;
                    }
                  } else if (llmEvent.type === 'audio_chunk' && llmEvent.audio_base64) {
                    try {
                      const binary = window.atob(llmEvent.audio_base64);
                      const incoming = new Uint8Array(binary.length);
                      for (let index = 0; index < binary.length; index += 1) {
                        incoming[index] = binary.charCodeAt(index);
                      }
                      const combined = new Uint8Array(pcmCarry.length + incoming.length);
                      combined.set(pcmCarry);
                      combined.set(incoming, pcmCarry.length);
                      const playableLength = combined.length - (combined.length % 2);
                      pcmCarry = combined.slice(playableLength);
                      if (!playableLength) return;

                      const sampleRate = llmEvent.sample_rate || 24000;
                      const sampleCount = playableLength / 2;
                      const audioContext = getTtsAudioContext();
                      void audioContext.resume().catch(() => undefined);
                      const audioBuffer = audioContext.createBuffer(1, sampleCount, sampleRate);
                      const channel = audioBuffer.getChannelData(0);
                      const view = new DataView(combined.buffer, combined.byteOffset, playableLength);
                      for (let index = 0; index < sampleCount; index += 1) {
                        channel[index] = view.getInt16(index * 2, true) / 32768;
                      }
                      const source = audioContext.createBufferSource();
                      source.buffer = audioBuffer;
                      source.connect(audioContext.destination);
                      const startAt = Math.max(audioContext.currentTime + 0.04, ttsNextPlaybackAt);
                      source.start(startAt);
                      ttsNextPlaybackAt = startAt + audioBuffer.duration;
                      if (!ttsHadAudio) {
                        ttsHadAudio = true;
                        dispatch(updateAITalkState({ isAITalking: true }));
                        logVoiceTrace('tts_playback_start', turnId, {
                          elapsed_ms: Math.round(performance.now() - finalizedAt),
                        });
                      }
                    } catch (error) {
                      logVoiceTrace('tts_audio_decode_failed', turnId, {
                        error: error instanceof Error ? error.name : 'UnknownError',
                      });
                    }
                  } else if (llmEvent.type === 'tts_error') {
                    logVoiceTrace('tts_failed', turnId, { code: llmEvent.code || 'TTS_FAILED' });
                    if (!ttsErrorShown) {
                      ttsErrorShown = true;
                      Message.warning('火山语音合成失败，文字回复已保留。');
                    }
                  }
                }, finalizedAt, speechTurn?.speechStartAt, speechTurn?.speechEndAt);
                if (answer && !answerFinalized) {
                  dispatch(
                    setHistoryMsg({ text: answer, user: botName, paragraph: true, definite: true })
                  );
                }
                if (answer) {
                  const audioContext = getTtsAudioContext();
                  if (ttsHadAudio && ttsNextPlaybackAt > audioContext.currentTime) {
                    await new Promise((resolve) => {
                      window.setTimeout(
                        resolve,
                        Math.ceil((ttsNextPlaybackAt - audioContext.currentTime) * 1000)
                      );
                    });
                    logVoiceTrace('tts_playback_end', turnId);
                  }
                }
              })
              .catch((error) => {
                const code = String(error).replace(/^Error: /, '');
                Message.error(getAgentErrorMessage(code));
              })
              .finally(() => {
                dispatch(updateAITalkState({ isAITalking: false }));
                llmBusy.current = false;
              });
          }
        };
        localSocket.onerror = () => {
          if (!cancelled) {
            dispatch(updateASRStatus('error'));
            dispatch(updateAIGCState({ isAIGCEnable: false }));
            Message.error('ASR WebSocket 连接失败，请检查后端和 ASR 配置。');
          }
        };
        localSocket.onclose = (event) => {
          disableAudioCallback();
          if (audioWatchdog) window.clearTimeout(audioWatchdog);
          if (socketRef.current === localSocket) socketRef.current = null;
          if (!cancelled && event.code !== 1000) {
            dispatch(updateASRStatus('error'));
            dispatch(updateAIGCState({ isAIGCEnable: false }));
            Message.error(`ASR 连接已关闭：${event.reason || event.code}`);
          }
        };
      } catch (error) {
        if (!cancelled) dispatch(updateASRStatus('error'));
        Message.error(`自有 Agent 会话创建失败：${String(error)}`);
      }
    };

    if (room.isJoined && !sessionId.current) void createSession();
    if (!room.isJoined && sessionId.current) {
      const previousSessionId = sessionId.current;
      sessionId.current = '';
      dispatch(setAgentSessionId(''));
      llmBusy.current = false;
      processedFinalUtteranceIds.current.clear();
      pendingSpeechTurns.length = 0;
      turnQueue.current = Promise.resolve();
      socketRef.current?.close(1000, 'room left');
      socketRef.current = null;
      disableAudioCallback();
      if (audioWatchdog) window.clearTimeout(audioWatchdog);
      dispatch(updateASRStatus('idle'));
      dispatch(updateASRPartialText(''));
      void fetch(`${AIGC_PROXY_HOST}/api/agent/sessions/${previousSessionId}`, {
        method: 'DELETE',
        credentials: 'include',
      });
      dispatch(updateAIGCState({ isAIGCEnable: false }));
    }
    return () => {
      cancelled = true;
      if (audioWatchdog) window.clearTimeout(audioWatchdog);
      if (localSocket) localSocket.close(1000, 'component changed');
      if (socketRef.current === localSocket) socketRef.current = null;
      disableAudioCallback();
    };
  }, [dispatch, room.isJoined, room.localUser.userId, room.roomId, room.scene]);
}
