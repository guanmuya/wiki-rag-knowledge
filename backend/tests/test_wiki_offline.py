r"""离线 Wiki 页面拼装：表格块必须保留换行。

真实缺陷：`_offline_page()` 原来对所有片段做 `re.sub(r"\s+", " ")`，
把带 Markdown 表格的表格块压成一行竖线。Wiki 页面正文于是显示成
「表：表格 1（共 7 行）| 假期类型 | 天数 | …」这样一串管道符，前端
Markdown 渲染器再也识别不出表格。这里锁住「表格保留换行、正文仍折叠空白」。
"""
from __future__ import annotations

from app import models
from app.services import wiki


def _chunk(text: str, *, kind: str = "text", heading: str = "三、请假制度") -> models.Chunk:
    # 只用于拼装，不需要落库；外键 id 填任意值即可
    return models.Chunk(
        kb_id=1, doc_id=1, doc_version_id=1,
        text=text, heading=heading, kind=kind,
    )


def test_offline_page_keeps_markdown_table_rows():
    table_text = (
        "表：表格 1（共 2 行）\n"
        "| 假期类型 | 天数 |\n"
        "| --- | --- |\n"
        "| 年假 | 5 天 |\n"
        "| 病假 | 10 天 |"
    )
    body = wiki._offline_page("请假制度", "摘要", [_chunk(table_text, kind="table")])
    assert "| 假期类型 | 天数 |" in body
    assert "| --- | --- |" in body
    assert "| 年假 | 5 天 |\n| 病假 | 10 天 |" in body
    # 不能把两行压成同一行
    assert "| 年假 | 5 天 | | 病假" not in body


def test_offline_page_still_collapses_text_chunks():
    body = wiki._offline_page("主题", "摘要", [_chunk("第一段。\n\n第二段。")])
    assert body == "第一段。 第二段。"


def test_offline_page_truncates_long_text_chunks_only():
    long_text = "内容" * 500
    body = wiki._offline_page("主题", "摘要", [_chunk(long_text)])
    assert body.endswith("…")
    assert len(body) == 701

    table_text = "| 列 | 值 |\n| --- | --- |\n" + "".join(
        f"| 行{i} | {'x' * 40} |\n" for i in range(30)
    )
    table_body = wiki._offline_page("表格", "摘要", [_chunk(table_text, kind="table")])
    assert table_body == table_text.strip()
