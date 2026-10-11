"""项目侧文本切片，所有输出均不超过 collection 的字符数上限。"""

from __future__ import annotations

def split_text(text: str, *, chunk_size: int, overlap: int) -> list[str]:
    """按段落优先切片，超长段落再按句子/字符切分，并添加字符级重叠。

    `chunk_size` 和 `overlap` 均以 Python Unicode 字符计数；函数会拒绝无效配置，
    并保证拼接输出覆盖清理后的完整正文。
    """

    if chunk_size < 1 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("chunk_size 必须大于 0，overlap 必须小于 chunk_size")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []

    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(start + chunk_size, len(normalized))
        hard_end = end
        if end < len(normalized):
            floor = start + max(1, chunk_size // 2)
            boundaries = [
                normalized.rfind(separator, floor, end)
                for separator in ("\n\n", "\n", "。", "！", "？", ". ", " ")
            ]
            boundary = max(boundaries)
            if boundary >= floor:
                end = min(hard_end, boundary + (2 if normalized.startswith("\n\n", boundary) else 1))
        piece = normalized[start:end]
        if piece:
            chunks.append(piece)
        if end >= len(normalized):
            break
        next_start = max(start + 1, end - overlap)
        start = next_start
    return chunks
