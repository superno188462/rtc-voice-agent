"""将同一轮语音的前端 VAD、ASR 与 LLM 阶段耗时关联并输出日志。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class TurnTrace:
    """仅保存单轮阶段时间，不保存音频或转写文本。"""

    stages: dict[str, float] = field(default_factory=dict)
    summary_logged: bool = False


_traces: dict[str, TurnTrace] = {}
_MAX_TRACES = 2000


def mark_turn_event(turn_id: str | None, event: str) -> None:
    """记录一个语音阶段并在 LLM 流结束后输出端到端耗时汇总。"""

    if not turn_id:
        return
    now = time.perf_counter()
    trace = _traces.setdefault(turn_id, TurnTrace())
    trace.stages.setdefault(event, now)
    print(f"[trace][turn] event={event} turn_id={turn_id} t={time.time():.3f}")
    if len(_traces) > _MAX_TRACES:
        oldest_key = next(iter(_traces))
        if oldest_key != turn_id:
            _traces.pop(oldest_key, None)
    _log_summary(turn_id, trace)


def _log_summary(turn_id: str, trace: TurnTrace) -> None:
    """阶段齐备时打印耗时；ASR 计时由前端 PCM 能量 VAD 起止点估算。"""

    stages = trace.stages
    if trace.summary_logged or "llm_stream_end" not in stages:
        return

    def elapsed(start: str, end: str) -> str:
        if start not in stages or end not in stages:
            return "unavailable"
        return f"{(stages[end] - stages[start]) * 1000:.1f}"

    print(
        f"[trace][turn] summary turn_id={turn_id} "
        f"speech_start_to_asr_final_ms={elapsed('speech_start', 'asr_final')} "
        f"speech_end_to_asr_final_ms={elapsed('speech_end', 'asr_final')} "
        f"asr_final_to_llm_request_ms={elapsed('asr_final', 'llm_request_start')} "
        f"llm_request_to_first_token_ms={elapsed('llm_request_start', 'llm_first_token')} "
        f"llm_request_to_stream_end_ms={elapsed('llm_request_start', 'llm_stream_end')} "
        f"speech_start_to_first_token_ms={elapsed('speech_start', 'llm_first_token')} "
        f"speech_end_to_first_token_ms={elapsed('speech_end', 'llm_first_token')} "
        "tts_status=not_implemented"
    )
    trace.summary_logged = True
