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
import { AIGC_PROXY_HOST } from '@/config';
import {
  setHistoryMsg,
  updateAIGCState,
  updateASRPartialText,
  updateASRStatus,
  updateAIThinkState,
  updateAITalkState,
} from '@/store/slices/room';

type AgentEvent = {
  type: string;
  text?: string;
  code?: string;
  utterance_id?: string;
  turn_id?: string;
};
type VoiceTurn = {
  turnId: string;
  speechStartAt: number;
  speechEndAt?: number;
  firstPartialAt?: number;
};

const VAD_RMS_THRESHOLD = 0.012;
const VAD_SILENCE_MS = 600;

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
  speechStartAt?: number
): Promise<void> {
  const clientStartedAt = performance.now();
  let firstTextAt: number | undefined;
  logVoiceTrace('llm_client_request_start', turnId, {
    asr_final_to_request_ms: Math.round(clientStartedAt - asrFinalAt),
  });
  const response = await fetch(
    `${AIGC_PROXY_HOST}/api/agent/sessions/${sessionId}/turns`,
    {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ text, turn_id: turnId }),
    }
  );
  if (!response.ok || !response.body) {
    throw new Error(`LLM request failed (${response.status})`);
  }
  logVoiceTrace('llm_client_response_headers', turnId, {
    elapsed_ms: Math.round(performance.now() - clientStartedAt),
  });

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  // The SSE stream remains open until the server emits its terminal event.
  // eslint-disable-next-line no-constant-condition
  while (true) {
    // SSE is a live stream, so each read intentionally waits for the next frame.
    // eslint-disable-next-line no-await-in-loop
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const frames = buffer.split('\n\n');
    buffer = frames.pop() || '';
    frames.forEach((frame) => {
      const line = frame.split('\n').find((item) => item.startsWith('data:'));
      if (line) {
        const event = JSON.parse(line.slice(5).trim()) as AgentEvent;
        if (event.type === 'error') throw new Error(event.code || 'LLM_REQUEST_FAILED');
        if (event.type === 'text_delta' && event.text && firstTextAt === undefined) {
          firstTextAt = performance.now();
          logVoiceTrace('llm_client_first_text', turnId, {
            elapsed_ms: Math.round(firstTextAt - clientStartedAt),
            speech_start_to_client_first_text_ms:
              speechStartAt === undefined ? null : Math.round(firstTextAt - speechStartAt),
          });
        }
        onEvent(event);
      }
    });
    if (done) break;
  }
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
    const pendingSpeechTurns: VoiceTurn[] = [];
    const disableAudioCallback = () => {
      if (RtcClient.engine) {
        RtcClient.engine.setAudioFrameCallback(StreamIndex.STREAM_INDEX_MAIN, undefined, undefined);
      }
    };

    const createSession = async () => {
      if (!room.isJoined || !room.roomId || !room.localUser.userId) return;
      dispatch(updateASRStatus('connecting'));
      try {
        const response = await fetch(`${AIGC_PROXY_HOST}/api/agent/sessions`, {
          method: 'POST',
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
        if (cancelled) {
          await fetch(`${AIGC_PROXY_HOST}/api/agent/sessions/${createdSessionId}`, {
            method: 'DELETE',
          });
          return;
        }

        sessionId.current = createdSessionId;
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
                    if (!activeSpeechTurn && rms >= VAD_RMS_THRESHOLD) {
                      const turnId =
                        typeof crypto !== 'undefined' && 'randomUUID' in crypto
                          ? crypto.randomUUID()
                          : `turn-${Date.now()}-${Math.random().toString(16).slice(2)}`;
                      activeSpeechTurn = { turnId, speechStartAt: now };
                      pendingSpeechTurns.push(activeSpeechTurn);
                      silenceMs = 0;
                      silenceStartedAt = undefined;
                      logVoiceTrace('speech_start', turnId, { vad_rms: Number(rms.toFixed(4)) });
                      sendTraceMarker('speech_start', turnId);
                    } else if (activeSpeechTurn && rms < VAD_RMS_THRESHOLD) {
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
                        activeSpeechTurn = null;
                        silenceMs = 0;
                        silenceStartedAt = undefined;
                      }
                    } else if (activeSpeechTurn) {
                      silenceMs = 0;
                      silenceStartedAt = undefined;
                    }
                    localSocket.send(pcm);
                    audioFramesSent += 1;
                    if (audioFramesSent === 1) {
                      if (audioWatchdog) window.clearTimeout(audioWatchdog);
                      audioWatchdog = undefined;
                      if (asrReady) dispatch(updateASRStatus('listening'));
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
            Message.error(`ASR 连接失败：${event.code || 'ASR_UPSTREAM_FAILED'}`);
            dispatch(updateASRStatus('error'));
            dispatch(updateAIGCState({ isAIGCEnable: false }));
            dispatch(updateASRPartialText(''));
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
            const pendingTurnIndex = event.turn_id
              ? pendingSpeechTurns.findIndex((turn) => turn.turnId === event.turn_id)
              : 0;
            const speechTurn =
              pendingTurnIndex >= 0 ? pendingSpeechTurns.splice(pendingTurnIndex, 1)[0] : undefined;
            const turnId = event.turn_id || speechTurn?.turnId || `asr-${Date.now()}`;
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
            turnQueue.current = turnQueue.current
              .then(async () => {
                llmBusy.current = true;
                let answer = '';
                dispatch(updateAIThinkState({ isAIThinking: true }));
                await readLlmTurn(turnSessionId, finalText, turnId, (llmEvent) => {
                  if (llmEvent.type === 'text_delta' && llmEvent.text) {
                    answer += llmEvent.text;
                    dispatch(
                      setHistoryMsg({ text: answer, user: botName, paragraph: false, definite: false })
                    );
                  }
                }, finalizedAt, speechTurn?.speechStartAt);
                logVoiceTrace('tts_not_enabled', turnId, { status: 'not_implemented' });
                if (answer) {
                  dispatch(
                    setHistoryMsg({ text: answer, user: botName, paragraph: true, definite: true })
                  );
                }
              })
              .catch((error) => {
                Message.error(`LLM 调用失败：${String(error)}`);
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
