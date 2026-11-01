"""把 docs/开题报告.md 转成可提交的 Word 文档（docs/开题报告.docx）。

只支持本项目用到的 Markdown 子集：标题、段落、有序/无序列表、表格、引用、分隔线、
行内 **加粗** 与 `代码`。这样正文只有一份源（.md），排版脚本可重复执行。

用法：
  python scripts/build_opening_report.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "开题报告.md"
OUT = ROOT / "docs" / "开题报告.docx"

BODY_FONT_CN = "宋体"
HEAD_FONT_CN = "黑体"
LATIN_FONT = "Times New Roman"
MONO_FONT = "Consolas"

INLINE_RE = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")


# ---------------------------------------------------------------- 基础排版工具
def set_run_font(run, *, cn=BODY_FONT_CN, latin=LATIN_FONT, size=12, bold=None,
                 color=None):
    run.font.name = latin
    run.font.size = Pt(size)
    if bold is not None:
        run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:ascii"), latin)
    rfonts.set(qn("w:hAnsi"), latin)
    rfonts.set(qn("w:eastAsia"), cn)


def add_runs(paragraph, text, *, size=12, cn=BODY_FONT_CN, latin=LATIN_FONT,
             bold=False):
    """写入一段带行内格式的文字：**加粗**、`代码`。"""
    for token in INLINE_RE.split(text):
        if not token:
            continue
        if token.startswith("**") and token.endswith("**"):
            run = paragraph.add_run(token[2:-2])
            set_run_font(run, cn=cn, latin=latin, size=size, bold=True)
        elif token.startswith("`") and token.endswith("`"):
            run = paragraph.add_run(token[1:-1])
            set_run_font(run, cn=cn, latin=MONO_FONT, size=size - 0.5, bold=bold)
        else:
            run = paragraph.add_run(token)
            set_run_font(run, cn=cn, latin=latin, size=size, bold=bold)


def shade_cell(cell, fill="F2F2F2"):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tc_pr.append(shd)


def add_page_number_footer(section):
    footer = section.footer
    p = footer.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run()
    set_run_font(run, size=9)
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    run._r.append(fld_begin)
    run._r.append(instr)
    run._r.append(fld_end)


def setup_document() -> Document:
    doc = Document()
    section = doc.sections[0]
    section.page_width = Cm(21)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.54)
    section.bottom_margin = Cm(2.54)
    section.left_margin = Cm(3.0)
    section.right_margin = Cm(2.5)
    add_page_number_footer(section)

    normal = doc.styles["Normal"]
    normal.font.name = LATIN_FONT
    normal.font.size = Pt(12)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT_CN)
    normal.paragraph_format.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(0)

    for level, size in ((1, 16), (2, 14)):
        style = doc.styles[f"Heading {level}"]
        style.font.name = LATIN_FONT
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.element.rPr.rFonts.set(qn("w:eastAsia"), HEAD_FONT_CN)
        style.paragraph_format.space_before = Pt(12 if level == 1 else 8)
        style.paragraph_format.space_after = Pt(6)
        style.paragraph_format.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
    return doc


# ---------------------------------------------------------------- Markdown 解析
def split_table(lines: list[str]) -> tuple[list[str], list[list[str]]]:
    rows = []
    for line in lines:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        rows.append(cells)
    header, body = rows[0], rows[1:]
    return header, body


def add_table(doc: Document, header: list[str], rows: list[list[str]]):
    table = doc.add_table(rows=1, cols=len(header))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    hdr = table.rows[0].cells
    for i, text in enumerate(header):
        hdr[i].text = ""
        p = hdr[i].paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        add_runs(p, text, size=10.5, cn=HEAD_FONT_CN, bold=True)
        shade_cell(hdr[i])
    # 行内不跨页断开（cantSplit 必须排在 tblHeader 之前，符合 CT_TrPr 顺序）
    for row in table.rows:
        tr_pr = row._tr.get_or_add_trPr()
        tr_pr.append(OxmlElement("w:cantSplit"))
    # 表头跨页重复：长表（进度安排）翻页后仍能看懂每列含义
    tr_pr = table.rows[0]._tr.get_or_add_trPr()
    tbl_header = OxmlElement("w:tblHeader")
    tbl_header.set(qn("w:val"), "true")
    tr_pr.append(tbl_header)
    for row in rows:
        cells = table.add_row().cells
        for i, text in enumerate(row[: len(header)]):
            cells[i].text = ""
            p = cells[i].paragraphs[0]
            p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
            add_runs(p, text, size=10.5)
    # 行内不跨页断开
    for row in table.rows:
        tr_pr = row._tr.get_or_add_trPr()
        cant_split = OxmlElement("w:cantSplit")
        tr_pr.append(cant_split)
    # 表后留一点空，避免与下一段贴在一起
    spacer = doc.add_paragraph()
    spacer.paragraph_format.space_after = Pt(6)
    return table


def build(doc: Document, md: str):
    lines = md.splitlines()
    i = 0
    first_h1 = True
    in_cover = True
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # 分隔线：封面之后分页
        if re.fullmatch(r"-{3,}", stripped):
            if in_cover:
                doc.add_page_break()
                in_cover = False
            i += 1
            continue

        # 标题
        m = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            if level == 1 and first_h1:
                p = doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p.paragraph_format.space_before = Pt(36)
                p.paragraph_format.space_after = Pt(18)
                add_runs(p, text, size=18, cn=HEAD_FONT_CN, bold=True)
                first_h1 = False
            else:
                heading = doc.add_heading(text, level=min(level - 1, 2))
                heading.paragraph_format.keep_with_next = True
            i += 1
            continue

        # 表格
        if stripped.startswith("|"):
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            header, rows = split_table(block)
            if in_cover:
                table = add_table(doc, header, rows)
                table.columns[0].width = Cm(3.2)
                table.columns[1].width = Cm(11.5)
            else:
                add_table(doc, header, rows)
            continue

        # 引用
        if stripped.startswith(">"):
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.74)
            p.paragraph_format.space_before = Pt(3)
            p.paragraph_format.space_after = Pt(3)
            add_runs(p, stripped.lstrip("> ").strip(), size=10.5)
            for run in p.runs:
                run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
            i += 1
            continue

        # 有序 / 无序列表
        m = re.match(r"^(\d+\.|[-*])\s+(.*)$", stripped)
        if m:
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.74)
            p.paragraph_format.first_line_indent = Cm(0)
            p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
            marker = m.group(1)
            prefix = f"{marker} " if marker[0].isdigit() else "• "
            add_runs(p, prefix + m.group(2), size=12)
            i += 1
            continue

        # 参考文献：悬挂缩进
        if re.match(r"^\[\d+\]", stripped):
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.9)
            p.paragraph_format.first_line_indent = Cm(-0.9)
            p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
            p.paragraph_format.space_after = Pt(3)
            add_runs(p, stripped, size=10.5)
            i += 1
            continue

        # 普通段落
        p = doc.add_paragraph()
        p.paragraph_format.first_line_indent = Pt(24)
        p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
        p.paragraph_format.space_after = Pt(3)
        add_runs(p, stripped, size=12)
        i += 1


def main() -> int:
    if not SRC.exists():
        print(f"缺少源文件：{SRC}")
        return 2
    doc = setup_document()
    build(doc, SRC.read_text(encoding="utf-8"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(OUT)
    print(f"已生成：{OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
