import { AIGC_PROXY_HOST } from '@/config';

export interface AgentEvent {
  type: string;
  text?: string;
  code?: string;
  message?: string;
  turn_id?: string;
  stage?: string;
  status?: string;
  summary?: string;
  details?: Record<string, unknown>;
  at?: number;
  sequence?: number;
  chunk_index?: number;
  audio_base64?: string;
  sample_rate?: number;
}

/** Create a server-owned authenticated Agent session for voice or text turns. */
export async function createAgentSession(
  sceneId: string,
  roomId: string,
  userId: string
): Promise<string> {
  const response = await fetch(`${AIGC_PROXY_HOST}/api/agent/sessions`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ scene_id: sceneId, room_id: roomId, user_id: userId }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || typeof data.session_id !== 'string') {
    throw new Error(data?.error?.code || `SESSION_CREATE_FAILED_${response.status}`);
  }
  return data.session_id;
}

/** Stream the same Agent turn contract used by ASR; callers decide whether to synthesize audio. */
export async function streamAgentTurn(
  sessionId: string,
  text: string,
  turnId: string,
  onEvent: (event: AgentEvent) => void,
  synthesizeSpeech: boolean,
  onResponseHeaders?: (elapsedMs: number) => void
): Promise<void> {
  const startedAt = performance.now();
  const response = await fetch(`${AIGC_PROXY_HOST}/api/agent/sessions/${sessionId}/turns`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ text, turn_id: turnId, synthesize_speech: synthesizeSpeech }),
  });
  if (!response.ok || !response.body) {
    throw new Error(`LLM_REQUEST_FAILED_${response.status}`);
  }
  onResponseHeaders?.(Math.round(performance.now() - startedAt));

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  // eslint-disable-next-line no-constant-condition
  while (true) {
    // SSE frames are yielded as they arrive; do not buffer the full response.
    // eslint-disable-next-line no-await-in-loop
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const frames = buffer.split('\n\n');
    buffer = frames.pop() || '';
    frames.forEach((frame) => {
      const line = frame.split('\n').find((item) => item.startsWith('data:'));
      if (!line) return;
      const event = JSON.parse(line.slice(5).trim()) as AgentEvent;
      if (event.type === 'error') throw new Error(event.code || 'LLM_REQUEST_FAILED');
      onEvent(event);
    });
    if (done) break;
  }
}
