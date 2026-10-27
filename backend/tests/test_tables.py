"""表格分块与解析的回归测试。

覆盖的是「大表格怎么切」这一组真实约束，而不是「不抛异常就算过」：
- 100~700 行的 Word/Excel/PDF 表格必须切块，且每块都能独立检索（带表头）；
- 每块长度受 config.TABLE_CHUNK_BUDGET 硬约束（超了会被 embedding 静默截断）；
- 行区间必须连续无重叠无遗漏，拼起来正好覆盖 1..total；
- locator 必须能定位回原始行/工作表/页码；
- 图片不解析，也不得调用视觉模型；
- 文本/标题变化必须强制切块。

夹具在 demo_data/fixtures/，由 scripts/make_fixtures.py 生成；
文件缺失时跳过对应用例（而不是让整个测试文件失败），
但 skip 原因会写明需要跑哪个脚本。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import config
from app.services import chunking, llm, parser

# ---------------------------------------------------------------- 夹具路径
REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "demo_data" / "fixtures"

DOCX_120 = FIXTURES / "员工薪酬明细_120行.docx"
DOCX_700 = FIXTURES / "员工薪酬明细_700行.docx"
XLSX_200 = FIXTURES / "员工薪酬明细_200行.xlsx"
PDF_DOC = FIXTURES / "差旅与采购管理办法.pdf"
DOCX_SECTIONS = FIXTURES / "研发流程规范.docx"
TXT_DOC = FIXTURES / "客户投诉处理指引.txt"

# 表格块长度上限：表格走独立预算，但仍是硬约束。
# 不用 DEFAULT_CHUNK_SIZE：表格信息密度高，允许比正文块更长。
MAX_TABLE_CHARS = config.TABLE_CHUNK_BUDGET

# 大表渲染成「列名: 值」后每块都应带的列名，用来验证表头被重复写入。
HEADER_FIELDS = ["工号", "月薪"]


def _require(path: Path) -> Path:
    """夹具缺失时跳过，并说明怎么生成。"""
    if not path.exists():
        pytest.skip(f"缺少夹具 {path.name}，请先运行 python scripts/make_fixtures.py")
    return path


def _parse(name: str) -> parser.ParsedDocument:
    return parser.parse_document(_require(FIXTURES / name))


def _table_units(doc: parser.ParsedDocument, **kwargs) -> list[chunking.TextUnit]:
    return [u for u in chunking.split_blocks(doc.blocks, **kwargs) if u.kind == "table"]


def _row_ranges(units: list[chunking.TextUnit]) -> list[tuple[int, int]]:
    return [(u.meta["row_start"], u.meta["row_end"]) for u in units]


def _assert_ranges_complete(units: list[chunking.TextUnit], total: int, what: str) -> None:
    """行区间必须连续、无重叠、无遗漏，拼起来正好是 1..total。"""
    assert units, f"{what}：没有切出任何表格块"
    ranges = _row_ranges(units)
    assert ranges[0][0] == 1, f"{what}：首块应从第 1 行开始，实际是 {ranges[0][0]}"
    assert ranges[-1][1] == total, f"{what}：末块应结束于第 {total} 行，实际是 {ranges[-1][1]}"
    for prev, cur in zip(ranges, ranges[1:]):
        assert cur[0] == prev[1] + 1, (
            f"{what}：行区间不连续 —— 上一块结束于 {prev[1]}，下一块却从 {cur[0]} 开始"
            f"（重叠或遗漏）。完整区间：{ranges}"
        )
    covered = [r for start, end in ranges for r in range(start, end + 1)]
    assert covered == list(range(1, total + 1)), (
        f"{what}：行区间拼起来不等于 1..{total}，共 {len(covered)} 行，"
        f"重复 {len(covered) - len(set(covered))} 行"
    )
    for start, end in ranges:
        assert start <= end, f"{what}：行区间 {start}-{end} 起止颠倒"


def _assert_budget(units: list[chunking.TextUnit], what: str) -> None:
    over = [(len(u.text), u.locator) for u in units if len(u.text) > MAX_TABLE_CHARS]
    assert not over, (
        f"{what}：存在超过 {MAX_TABLE_CHARS} 字符的表格块（超长会被 embedding 截断），"
        f"超长块：{over[:5]}"
    )


# ================================================================ 700 行 Word
def test_docx_700_rows_splits_into_multiple_chunks():
    """700 行表格必须切块，不能整表一块。"""
    units = _table_units(_parse(DOCX_700.name))
    assert len(units) > 10, (
        f"700 行表格只切出 {len(units)} 块，"
        f"整表塞进一块会超出 embedding 上下文，必须按行组切分"
    )


def test_docx_700_rows_keeps_header_in_every_chunk():
    """每块都必须重复表头，否则单独命中一块时看不出列含义。"""
    units = _table_units(_parse(DOCX_700.name))
    missing = [
        (u.locator, [f for f in HEADER_FIELDS if f not in u.text])
        for u in units
        if not all(f in u.text for f in HEADER_FIELDS)
    ]
    assert not missing, f"有 {len(missing)} 个块丢了表头字段，前几个：{missing[:5]}"


def test_docx_700_rows_respects_char_budget():
    """每块字符数不得超过表格块预算。"""
    units = _table_units(_parse(DOCX_700.name))
    _assert_budget(units, "700 行 Word 表格")
    assert max(len(u.text) for u in units) > MAX_TABLE_CHARS * 0.5, (
        "块长普遍过小说明切得过碎，浪费索引与向量调用"
    )


def test_docx_700_rows_row_ranges_are_complete():
    """行区间连续无重叠无遗漏，拼起来正好是 1..700。"""
    doc = _parse(DOCX_700.name)
    table_blocks = [b for b in doc.blocks if b.kind == "table"]
    assert table_blocks, "700 行 Word 文档没有解析出表格块"
    total = table_blocks[0].table.total_rows
    units = _table_units(doc)
    _assert_ranges_complete(units, total, "700 行 Word 表格")
    assert total == 700, f"夹具应有 700 行数据，实际解析出 {total} 行"


# ================================================================ 120 行 Word
def test_docx_120_rows_row_ranges_are_complete():
    """120 行表格同样要保证行区间完整。"""
    doc = _parse(DOCX_120.name)
    table_blocks = [b for b in doc.blocks if b.kind == "table"]
    assert len(table_blocks) == 1, f"夹具应只有 1 张表，实际 {len(table_blocks)} 张"
    total = table_blocks[0].table.total_rows
    units = _table_units(doc)
    _assert_ranges_complete(units, total, "120 行 Word 表格")
    assert total == 120, f"夹具应有 120 行数据，实际解析出 {total} 行"


def test_docx_120_rows_budget_and_header():
    """120 行表格也要守住预算与表头。"""
    units = _table_units(_parse(DOCX_120.name))
    _assert_budget(units, "120 行 Word 表格")
    assert all("工号" in u.text for u in units), "每个块都应包含「工号」表头"


# ================================================================ 200 行 Excel
def test_xlsx_sheets_become_independent_tables():
    """两个工作表必须各自成为独立表格，heading 是工作表名。"""
    doc = _parse(XLSX_200.name)
    tables = [b for b in doc.blocks if b.kind == "table"]
    headings = {b.heading for b in tables}
    assert headings == {"薪酬明细", "部门汇总"}, (
        f"工作表应各自成为独立表格且 heading 为工作表名，实际 heading：{headings}"
    )
    assert len(tables) == 2, f"200 行 xlsx 应有 2 张表（薪酬明细/部门汇总），实际 {len(tables)} 张"

    detail = next(b for b in tables if b.heading == "薪酬明细")
    summary = next(b for b in tables if b.heading == "部门汇总")
    assert detail.table.total_rows == 200, f"薪酬明细应有 200 行，实际 {detail.table.total_rows}"
    assert summary.table.header[0] == "部门", f"部门汇总表头不对：{summary.table.header}"

    # 两张表的 locator 必须能区分工作表
    assert "薪酬明细" in detail.locator, f"薪酬明细表 locator 未带工作表名：{detail.locator}"
    assert "部门汇总" in summary.locator, f"部门汇总表 locator 未带工作表名：{summary.locator}"


def test_xlsx_detail_rows_complete_and_budgeted():
    """Excel 明细表切块后行区间完整、长度受控、每块带表头。"""
    doc = _parse(XLSX_200.name)
    units = [u for u in _table_units(doc) if "薪酬明细" in (u.meta or {}).get("caption", "")]
    assert units, "薪酬明细工作表没有切出表格块"
    _assert_ranges_complete(units, 200, "200 行 Excel 薪酬明细")
    _assert_budget(units, "200 行 Excel 薪酬明细")
    assert all("工号" in u.text for u in units), "Excel 表格块应重复表头「工号」"


# ================================================================ 小表不切
def _small_table(rows: int) -> parser.TableData:
    return parser.TableData(
        header=["部门", "人数", "备注"],
        rows=[[f"部门{i}", str(i * 3), "无"] for i in range(1, rows + 1)],
        caption="小表", index=1, locator_prefix="表格 1",
    )


@pytest.mark.parametrize("rows", [2, 6, 12, 24])
def test_small_table_is_not_split(rows):
    """能整块放进预算的小表不切分，保持完整上下文。"""
    table = _small_table(rows)
    units = chunking.split_table(table, size=config.DEFAULT_CHUNK_SIZE,
                                 ordinal_start=0, heading="小表")
    assert len(units) == 1, (
        f"{rows} 行小表整表能放下，应只切 1 块，实际切成 {len(units)} 块（切碎会丢上下文）"
    )
    unit = units[0]
    assert unit.meta["row_start"] == 1 and unit.meta["row_end"] == rows, (
        f"整表一块的行区间应为 1-{rows}，实际 {unit.meta['row_start']}-{unit.meta['row_end']}"
    )
    assert "部门" in unit.text and "人数" in unit.text, "整表块必须含表头"


def test_small_table_uses_full_table_locator():
    """整表一块时 locator 用「全表」，不用行区间。"""
    units = chunking.split_table(_small_table(6), size=config.DEFAULT_CHUNK_SIZE,
                                 ordinal_start=0, heading="小表")
    assert units[0].locator.endswith("全表"), (
        f"整表块 locator 应以「全表」结尾，实际：{units[0].locator}"
    )


# ================================================================ locator
def test_table_locator_points_back_to_original_rows():
    """locator 必须形如「表格 2 第 21-40 行」，能定位回原始行。"""
    table = parser.TableData(
        header=["工号", "月薪"],
        rows=[[f"E{i:04d}", str(10000 + i)] for i in range(1, 401)],
        caption="表格 2", index=2, locator_prefix="表格 2",
    )
    units = chunking.split_table(table, size=config.DEFAULT_CHUNK_SIZE,
                                 ordinal_start=0, heading="明细")
    assert len(units) > 1, "400 行表格应被切分"
    pattern = re.compile(r"^表格 2 第 (\d+)-(\d+) 行$")
    for unit in units:
        m = pattern.match(unit.locator)
        assert m, f"表格块 locator 格式不对（应形如「表格 2 第 21-40 行」），实际：{unit.locator!r}"
        start, end = int(m.group(1)), int(m.group(2))
        assert (start, end) == (unit.meta["row_start"], unit.meta["row_end"]), (
            f"locator 行区间 {start}-{end} 与 meta 不一致："
            f"{unit.meta['row_start']}-{unit.meta['row_end']}"
        )
        # 行区间必须真的能定位回原文：块首行内容应来自 start 那一行
        first_line = unit.text.split("\n", 1)[1].split("\n", 1)[0]
        assert f"E{start:04d}" in first_line, (
            f"块首行不是第 {start} 行的数据，locator 与内容错位：{first_line[:60]!r}"
        )


def test_table_locator_reaches_all_parts():
    """所有分块的 locator 拼起来应覆盖每一行，不允许有空档。"""
    table = parser.TableData(
        header=["工号", "月薪"],
        rows=[[f"E{i:04d}", str(10000 + i)] for i in range(1, 301)],
        caption="表格 1", index=1, locator_prefix="表格 1",
    )
    units = chunking.split_table(table, size=config.DEFAULT_CHUNK_SIZE,
                                 ordinal_start=0, heading="明细")
    seen: list[int] = []
    for unit in units:
        m = re.match(r"^表格 1 第 (\d+)-(\d+) 行$", unit.locator)
        assert m, f"locator 格式不对：{unit.locator!r}"
        seen.extend(range(int(m.group(1)), int(m.group(2)) + 1))
    assert seen == list(range(1, 301)), "locator 覆盖的行号必须正好是 1..300"


# ================================================================ meta
def test_table_chunk_meta_has_row_range():
    """表格 chunk 的 meta 必须含 row_start / row_end / total_rows。"""
    doc = _parse(DOCX_120.name)
    units = _table_units(doc)
    assert units, "没有切出表格块"
    for unit in units:
        meta = unit.meta
        assert isinstance(meta, dict), f"表格块 meta 应是 dict，实际 {type(meta)}"
        for key in ("row_start", "row_end", "total_rows"):
            assert key in meta, f"表格块 meta 缺字段 {key}，实际字段：{sorted(meta)}"
        assert meta["total_rows"] == 120, f"total_rows 应为 120，实际 {meta['total_rows']}"
        assert 1 <= meta["row_start"] <= meta["row_end"] <= 120, (
            f"行区间越界：{meta['row_start']}-{meta['row_end']}"
        )
        assert meta.get("cols"), "meta 应记录列数 cols，便于检索时判断表宽"


def test_table_chunk_meta_part_numbers_are_consistent():
    """有 part/parts 时，编号必须从 1 连续到 parts。"""
    doc = _parse(DOCX_120.name)
    units = _table_units(doc)
    parts = [u.meta.get("part") for u in units]
    if any(p is None for p in parts):
        pytest.skip("当前分块实现未输出 part/parts")
    assert parts == list(range(1, len(units) + 1)), f"part 编号不连续：{parts}"
    assert all(u.meta["parts"] == len(units) for u in units), (
        f"parts 应统一为 {len(units)}，实际 {[u.meta['parts'] for u in units]}"
    )


def test_text_chunk_has_no_table_meta():
    """纯文本块不应带表格 meta，避免检索侧误判。"""
    doc = _parse(TXT_DOC.name)
    units = chunking.split_blocks(doc.blocks)
    for unit in units:
        assert unit.kind == "text", f"txt 不应产出 {unit.kind} 类型的块"
        assert not unit.meta, f"文本块不应带表格 meta：{unit.meta}"


# ================================================================ PDF
def test_pdf_extracts_text_blocks():
    """PDF 必须能提取出文本块，并带页码 locator。"""
    doc = _parse(PDF_DOC.name)
    text_blocks = [b for b in doc.blocks if b.kind == "text" and b.text.strip()]
    assert text_blocks, "PDF 未提取到任何文本块"
    joined = "\n".join(b.text for b in text_blocks)
    assert "差旅" in joined, f"PDF 文本块内容异常，未包含标题关键词：{joined[:80]!r}"
    assert all(re.search(r"第 \d+ 页", b.locator) for b in text_blocks), (
        f"PDF 文本块 locator 必须带页码，实际：{[b.locator for b in text_blocks]}"
    )


def _make_pdf_with_table(path: Path) -> Path:
    """生成一个「空格对齐」的表格 PDF，用于验证启发式表格识别与页码 locator。

    不依赖 reportlab 之外的字体：优先用 STSong-Light 写中文；
    该字体不可用时退回 ASCII 列名，断言随之放宽到只看结构与页码。
    """
    reportlab = pytest.importorskip("reportlab", reason="需要 reportlab 生成 PDF 夹具")
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    cjk = True
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        font = "STSong-Light"
    except Exception:
        font, cjk = "Helvetica", False

    c = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4
    c.setFont(font, 14)
    c.drawString(60, height - 60, "费用明细表" if cjk else "Expense Detail")
    c.setFont(font, 10)
    rows = (["工号", "部门", "月薪"] if cjk else ["ID", "Dept", "Salary"]) + [
        ["E0001", "研发中心", "12000"], ["E0002", "产品部", "13500"],
        ["E0003", "市场部", "14200"], ["E0004", "财务部", "12800"],
    ]
    y = height - 100
    for row in rows:
        for x, cell in zip((60, 200, 340), row):
            c.drawString(x, y, cell)
        y -= 16
    c.setFont(font, 10)
    c.drawString(60, y - 20, "以上为月度固定工资口径，不含奖金与补贴。" if cjk else "Monthly base salary only.")
    c.save()
    return path


def test_pdf_table_locator_contains_page_number(tmp_path):
    """PDF 里识别到表格时，表格块 locator 必须带页码。"""
    pdf_path = _make_pdf_with_table(tmp_path / "table.pdf")
    doc = parser.parse_document(pdf_path)
    tables = [b for b in doc.blocks if b.kind == "table"]
    if not tables:
        pytest.skip("当前 PDF 表格识别未命中该夹具（启发式识别，允许漏识别）")

    for block in tables:
        assert re.search(r"第 \d+ 页", block.locator), (
            f"PDF 表格块 locator 必须带页码，实际：{block.locator!r}"
        )
        assert re.search(r"第 \d+ 页", block.table.locator_prefix), (
            f"PDF 表格 locator_prefix 必须带页码，实际：{block.table.locator_prefix!r}"
        )
        assert block.table.total_rows >= 3, (
            f"识别出的表格应至少 3 行数据，实际 {block.table.total_rows} 行"
        )

    units = [u for u in chunking.split_blocks(doc.blocks) if u.kind == "table"]
    assert units, "PDF 表格块没有进入分块结果"
    for unit in units:
        assert "第 1 页" in unit.locator, f"PDF 表格分块 locator 丢了页码：{unit.locator!r}"


# ================================================================ 图片不解析
def test_docx_images_are_not_parsed_as_blocks(tmp_path, monkeypatch):
    """图片可以记录在 parsed.images 里，但不能产生 image 块，也不得调用视觉模型。"""
    docx = pytest.importorskip("docx", reason="需要 python-docx")
    PIL_Image = pytest.importorskip("PIL.Image", reason="需要 Pillow 生成测试图片")

    img_path = tmp_path / "shot.png"
    PIL_Image.new("RGB", (64, 48), (180, 40, 40)).save(img_path)

    docx_path = tmp_path / "with_image.docx"
    document = docx.Document()
    document.add_heading("带图文档", level=1)
    document.add_paragraph("这是正文段落，用于验证图片不参与解析。")
    document.add_picture(str(img_path))
    document.add_paragraph("图片之后的段落内容同样应该被解析成文本。")
    document.save(str(docx_path))

    called: list[tuple] = []
    monkeypatch.setattr(llm, "describe_image",
                        lambda *a, **k: called.append((a, k)) or "视觉模型输出")

    parsed = parser.parse_document(docx_path)
    assert parsed.images, "docx 含图片时 parsed.images 应非空（记录元信息）"

    image_blocks = [b for b in parsed.blocks if b.kind == "image"]
    assert not image_blocks, (
        f"当前阶段不做图片解析，不应产生 image 类型的内容块，实际 {len(image_blocks)} 个"
    )

    units = chunking.split_blocks(parsed.blocks)
    assert all(u.kind != "image" for u in units), "分块结果里不应出现 image 块"
    assert not called, f"不应调用视觉模型 llm.describe_image，实际调用 {len(called)} 次"

    # 图片前后的正文都必须保留，不能因为图片丢掉文字
    text = "\n".join(u.text for u in units)
    assert "这是正文段落" in text, "图片前的正文丢失"
    assert "图片之后的段落内容" in text, "图片后的正文丢失"


# ================================================================ 文本/标题
def test_markdown_heading_change_forces_new_chunk():
    """标题变化必须强制切块，同一块内不能混两个章节的标志性内容。"""
    md = (
        "# 制度汇编\n\n"
        "## 考勤\n\n"
        "迟到超过三次扣发当月全勤奖，考勤记录由行政部按月统计并公示。\n\n"
        "## 请假\n\n"
        "年假可顺延至次年 3 月 31 日，逾期未休视为放弃，请假需提前一个工作日提交。\n"
    )
    parsed = parser._parse_plain(_write(tmp_path_file := Path(__file__).parent / "_md_tmp.md", md))
    try:
        units = chunking.split_blocks(parsed.blocks)
        assert len(units) >= 2, f"两个二级标题至少切 2 块，实际 {len(units)} 块"
        for unit in units:
            if "迟到" in unit.text:
                assert "年假" not in unit.text, (
                    f"考勤内容混进了请假内容，块文本：{unit.text[:120]!r}"
                )
            if "年假" in unit.text:
                assert "迟到" not in unit.text, (
                    f"请假内容混进了考勤内容，块文本：{unit.text[:120]!r}"
                )
        assert all(u.heading for u in units), "每块都必须带所属标题"
    finally:
        tmp_path_file.unlink(missing_ok=True)


def test_plain_text_sections_do_not_merge():
    """纯文本里的「一、二、三」章节不能被并进同一个块。"""
    doc = _parse(TXT_DOC.name)
    units = chunking.split_blocks(doc.blocks)
    assert len(units) >= 3, (
        f"txt 有 3 个章节（受理/处理/回访），应至少切 3 块，实际 {len(units)} 块 —— "
        f"章节被合并会让检索命中后分不清是哪一条流程"
    )
    markers = {"受理": "登记", "处理": "回复客户", "回访": "满意度"}
    for unit in units:
        hit = [name for name, kw in markers.items() if kw in unit.text]
        assert len(hit) <= 1, f"同一块混了多个章节 {hit}：{unit.text[:120]!r}"
    # 章节块必须带章节标题。文档最前面的「客户投诉处理指引」是全文标题、
    # 不属于任何章节，它的块 heading 为空是允许的，这里只看章节块。
    section_units = [u for u in units if any(kw in u.text for kw in markers.values())]
    assert section_units, "没有识别出任何章节块"
    for unit in section_units:
        assert unit.heading, (
            f"纯文本章节块应带章节标题，实际 heading 为空，块文本：{unit.text[:60]!r}"
        )
    assert {u.heading for u in section_units} >= {"一、受理", "二、处理", "三、回访"}, (
        f"章节标题应能区分三个小节，实际：{[u.heading for u in section_units]}"
    )


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_docx_multi_heading_sections_stay_separate():
    """多级标题 docx：每块只属于一个章节。

    用「该章节独有」的词判定归属，不能选会跨章节出现的普通词。
    例如「迭代」在「测试阶段」的正文里也出现（"最近两个迭代的新功能"），
    拿它当开发章节的标志词会误报。
    """
    doc = _parse(DOCX_SECTIONS.name)
    units = chunking.split_blocks(doc.blocks)
    assert len(units) >= 4, f"四个二级标题应至少切 4 块，实际 {len(units)} 块"
    section_kw = {
        "需求": "验收标准",
        "开发": "单元测试覆盖率",
        "测试": "自测报告",
        "发布": "回滚方案",
    }
    for unit in units:
        hit = [name for name, kw in section_kw.items() if kw in unit.text]
        assert len(hit) <= 1, f"同一块混了多个章节 {hit}：{unit.text[:120]!r}"
