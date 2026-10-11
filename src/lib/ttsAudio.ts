let sharedAudioContext: AudioContext | null = null;

export function getTtsAudioContext(): AudioContext {
  if (!sharedAudioContext || sharedAudioContext.state === 'closed') {
    sharedAudioContext = new AudioContext({ latencyHint: 'interactive' });
  }
  return sharedAudioContext;
}

/** 在加入房间的用户点击回调中调用，提前解锁浏览器音频播放权限。 */
export async function unlockTtsAudio(): Promise<void> {
  const context = getTtsAudioContext();
  if (context.state !== 'running') await context.resume();
}

export async function closeTtsAudioContext(): Promise<void> {
  const context = sharedAudioContext;
  sharedAudioContext = null;
  if (context && context.state !== 'closed') await context.close();
}
