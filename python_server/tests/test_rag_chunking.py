"""项目侧 RAG 切片边界和长度契约测试。"""

from services.rag.chunking import split_text


def test_split_text_respects_limit_and_overlap() -> None:
    text = ("第一段内容。" * 20) + "\n\n" + ("第二段内容。" * 20)
    chunks = split_text(text, chunk_size=80, overlap=12)

    assert len(chunks) > 1
    assert all(0 < len(chunk) <= 80 for chunk in chunks)
    assert all(chunks[index - 1][-12:] == chunks[index][:12] for index in range(1, len(chunks)))
    reconstructed = chunks[0] + "".join(chunk[12:] for chunk in chunks[1:])
    assert reconstructed == text.strip()


def test_split_text_normalizes_newlines_and_handles_short_text() -> None:
    assert split_text("\r\n 简短文本 \r\n", chunk_size=100, overlap=10) == ["简短文本"]


def test_split_text_rejects_invalid_overlap() -> None:
    try:
        split_text("text", chunk_size=10, overlap=10)
    except ValueError as error:
        assert "overlap" in str(error)
    else:
        raise AssertionError("expected invalid overlap to fail")
