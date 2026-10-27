"""文档解析：把不同格式统一成「带位置信息的段落块 + 结构化表格」。

设计取舍（与分块策略直接相关）：
1. 表格不再逐行拆成段落。逐行会产生成百上千个碎片，检索命中后拿不到上下文；
   正确做法是保留成结构化 TableData，交给分块层按行组切分并重复表头。
2. 每个块都带 locator（页码/段落/工作表/行号），答案才能落回原文具体位置。
3. 图片暂不做内容理解（视觉模型/OCR 后续接入），只记录占位说明，
   避免静默丢内容却又不引入未完成的依赖。
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

PLAIN_SUFFIXES = {".txt", ".md", ".markdown", ".log", ".csv", ".json"}
HTML_SUFFIXES = {".html", ".htm"}

# 单行被识别为表格行的启发式：至少 3 个分隔片段
_PIPE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_MD_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]{5,}\|?\s*$")
_MULTISPACE = re.compile(r"\s{2,}")


@dataclass
class TableData:
    """结构化表格。rows 不含表头行。"""
    header: list[str]
    rows: list[list[str]]
    caption: str = ""            # 工作表名 / 表格序号说明
    index: int = 0               # 文档内第几张表
    locator_prefix: str = ""     # 第 3 页 / 表格 2 / Sheet:成本

    @property
    def total_rows(self) -> int:
        return len(self.rows)

    @property
    def col_count(self) -> int:
        return len(self.header) or (len(self.rows[0]) if self.rows else 0)

    def header_line(self) -> str:
        cols = [c or f"列{i+1}" for i, c in enumerate(self.header)]
        return " | ".join(cols)


@dataclass
class ParsedBlock:
    text: str
    locator: str = ""          # 第 3 页 / 第 12 段 / Sheet:成本
    heading: str = ""          # 最近的标题
    kind: str = "text"         # text | heading | table | image
    table: TableData | None = None


@dataclass
class ParsedDocument:
    blocks: list[ParsedBlock] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)   # {"name","mime","data"}
    meta: dict = field(default_factory=dict)

    @property
    def full_text(self) -> str:
        return "\n".join(b.text for b in self.blocks if b.kind != "table")

    @property
    def table_count(self) -> int:
        return sum(1 for b in self.blocks if b.kind == "table")


class ParseError(RuntimeError):
    pass


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_HTML_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\u00a0]+")

# 纯文本 / PDF 里的章节标题：这些文档没有样式信息，只能靠形态识别。
# 不识别的话整篇会被并成一个块，检索命中后分不清是哪一条流程。
_PLAIN_HEADING_PATTERNS = [
    re.compile(r"^第[一二三四五六七八九十百零〇\d]+[章节条款部分]\s*[\u4e00-\u9fff、，,．.\s]*$"),
    re.compile(r"^[一二三四五六七八九十]+[、．.]\s*\S{1,30}$"),
    re.compile(r"^[（(][一二三四五六七八九十\d]+[）)]\s*\S{1,30}$"),
    re.compile(r"^\d+(\.\d+){0,3}[、．.]?\s+[\u4e00-\u9fff][\u4e00-\u9fff\s]{0,28}$"),
]


def looks_like_plain_heading(line: str) -> bool:
    """判断一行是否是纯文本里的章节标题。

    只认「编号 + 短标题」这种强形态，且长度受限，
    避免把正文里的编号列表（如「1. 员工在试用期结束前提交申请。」）误判成标题。
    """
    text = line.strip()
    if not text or len(text) > 40:
        return False
    if text.endswith(("。", "；", ";", "，", ",", "：", ":")):
        return False
    return any(p.match(text) for p in _PLAIN_HEADING_PATTERNS)


def _clean(text: str) -> str:
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    text = _WS.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return _clean(str(value))


# ------------------------------------------------------------------ 表格识别
def _split_md_row(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [_clean(c) for c in body.split("|")]


def _looks_like_table(lines: list[str]) -> bool:
    """PDF/纯文本里的表格行：用 2+ 空格或 | 对齐，且列数一致。"""
    if len(lines) < 2:
        return False
    counts = []
    for line in lines[:12]:
        parts = [p for p in _MULTISPACE.split(line.strip()) if p]
        if "|" in line:
            parts = [p for p in _split_md_row(line) if p]
        counts.append(len(parts))
    return sum(1 for c in counts if c >= 3) >= max(2, len(counts) // 2)


def _rows_to_table(lines: list[str], *, caption: str, index: int,
                   locator_prefix: str) -> TableData | None:
    parsed: list[list[str]] = []
    for line in lines:
        if "|" in line:
            cells = _split_md_row(line)
        else:
            cells = [_clean(p) for p in _MULTISPACE.split(line.strip()) if p]
        if any(cells):
            parsed.append(cells)
    if len(parsed) < 2:
        return None
    width = max(len(r) for r in parsed)
    header = parsed[0] + [""] * (width - len(parsed[0]))
    body = [r + [""] * (width - len(r)) for r in parsed[1:]]
    return TableData(header=header, rows=body, caption=caption, index=index,
                     locator_prefix=locator_prefix)


# ------------------------------------------------------------------ 各格式解析
def _parse_plain(path: Path) -> ParsedDocument:
    raw = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.lower()
    doc = ParsedDocument(meta={"format": suffix})
    if suffix == ".csv":
        rows = list(csv.reader(io.StringIO(raw)))
        if len(rows) >= 2:
            width = max(len(r) for r in rows)
            header = [_cell(c) for c in rows[0]] + [""] * (width - len(rows[0]))
            body = [[_cell(c) for c in r] + [""] * (width - len(r)) for r in rows[1:]]
            doc.blocks.append(ParsedBlock(
                text="", locator="CSV", kind="table",
                table=TableData(header=header, rows=body, caption=path.name,
                                index=1, locator_prefix="CSV"),
            ))
        return doc

    if suffix == ".json":
        try:
            obj = json.loads(raw)
            pretty = json.dumps(obj, ensure_ascii=False, indent=2)
        except ValueError:
            pretty = raw
        return ParsedDocument(
            blocks=[ParsedBlock(_clean(pretty), locator="全文")], meta={"format": "json"}
        )

    heading = ""
    para_no = 0
    buf: list[str] = []
    table_buf: list[str] = []
    table_no = 0
    seen_heading = False

    def flush_text() -> None:
        nonlocal para_no, buf
        if not buf:
            return
        text = _clean("\n".join(buf))
        if text:
            para_no += 1
            doc.blocks.append(ParsedBlock(text, locator=f"第 {para_no} 段", heading=heading))
        buf = []

    def flush_table() -> None:
        nonlocal table_no, table_buf
        if not table_buf:
            return
        rows = [ln for ln in table_buf if not _MD_TABLE_SEP.match(ln)]
        table_no += 1
        table = _rows_to_table(rows, caption=f"表格 {table_no}", index=table_no,
                               locator_prefix=f"表格 {table_no}")
        if table:
            doc.blocks.append(ParsedBlock("", locator=table.locator_prefix,
                                          heading=heading, kind="table", table=table))
        else:
            buf.extend(table_buf)
        table_buf = []

    for line in raw.splitlines():
        stripped = line.strip()
        is_md_heading = bool(_MD_HEADING.match(stripped)) and suffix in {".md", ".markdown"}
        is_pipe = bool(_PIPE_ROW.match(line)) and suffix in {".md", ".markdown"}
        is_sep = bool(_MD_TABLE_SEP.match(line)) and suffix in {".md", ".markdown"}
        # 纯文本 / Markdown 里没有样式的章节标题（如「一、受理」）
        is_plain_heading = (
            not is_md_heading and not is_pipe and not is_sep
            and bool(stripped) and looks_like_plain_heading(stripped)
            and suffix not in {".csv", ".json"}
        )

        if is_md_heading or is_plain_heading:
            flush_table()
            flush_text()
            if is_md_heading:
                heading = _MD_HEADING.match(stripped).group(2).strip()
            else:
                heading = stripped
            seen_heading = True
            para_no += 1
            doc.blocks.append(ParsedBlock(heading, locator=f"第 {para_no} 段",
                                          heading=heading, kind="heading"))
            continue

        # 首个标题之前出现的短行，视为文档标题（如「客户投诉处理指引」）。
        # 不这样处理，它会被当成无标题正文块，后续章节又各带标题，
        # 导致同一篇文档里出现「有 heading」和「无 heading」混杂的块。
        if (not seen_heading and not doc.blocks and stripped
                and len(stripped) <= 40 and not stripped.endswith(("。", "；", "，", "："))):
            flush_table()
            flush_text()
            heading = stripped
            seen_heading = True
            para_no += 1
            doc.blocks.append(ParsedBlock(heading, locator=f"第 {para_no} 段",
                                          heading=heading, kind="heading"))
            continue
        if is_pipe or (is_sep and table_buf):
            flush_text()
            if not is_sep:
                table_buf.append(line)
            continue
        if table_buf:
            flush_table()
        if stripped == "":
            flush_text()
        else:
            buf.append(line)
    flush_table()
    flush_text()
    return doc


def _parse_html(path: Path) -> ParsedDocument:
    raw = path.read_text(encoding="utf-8", errors="replace")
    try:
        from lxml import html as lxml_html
        tree = lxml_html.fromstring(raw)
        for bad in tree.xpath("//script|//style"):
            bad.getparent().remove(bad)
        doc = ParsedDocument(meta={"format": "html"})
        heading = ""
        para = 0
        table_no = 0
        nodes = tree.xpath("//h1|//h2|//h3|//h4|//p|//li|//table")
        for node in nodes:
            tag = node.tag.lower()
            if tag == "table":
                table_no += 1
                rows = node.xpath(".//tr")
                parsed: list[list[str]] = []
                for tr in rows:
                    cells = [_clean(c.text_content() or "") for c in tr.xpath("./th|./td")]
                    if any(cells):
                        parsed.append(cells)
                if len(parsed) >= 2:
                    width = max(len(r) for r in parsed)
                    doc.blocks.append(ParsedBlock(
                        text="", locator=f"表格 {table_no}", heading=heading, kind="table",
                        table=TableData(
                            header=parsed[0] + [""] * (width - len(parsed[0])),
                            rows=[r + [""] * (width - len(r)) for r in parsed[1:]],
                            caption=f"表格 {table_no}", index=table_no,
                            locator_prefix=f"表格 {table_no}",
                        ),
                    ))
                continue
            text = _clean(node.text_content() or "")
            if not text:
                continue
            para += 1
            if tag in {"h1", "h2", "h3", "h4"}:
                heading = text
                doc.blocks.append(ParsedBlock(text, locator=f"第 {para} 段",
                                              heading=heading, kind="heading"))
            else:
                doc.blocks.append(ParsedBlock(text, locator=f"第 {para} 段", heading=heading))
        if not doc.blocks:
            doc.blocks.append(ParsedBlock(_clean(_HTML_TAG.sub(" ", raw)), locator="全文"))
        return doc
    except Exception as exc:
        raise ParseError(f"HTML 解析失败：{exc}") from exc


def _parse_pdf(path: Path) -> ParsedDocument:
    from pypdf import PdfReader
    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise ParseError(f"PDF 打开失败：{exc}") from exc
    doc = ParsedDocument(meta={"format": "pdf", "pages": len(reader.pages),
                               "table_detection": "heuristic"})
    table_no = 0
    for page_no, page in enumerate(reader.pages, start=1):
        try:
            raw = page.extract_text() or ""
        except Exception:
            raw = ""
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        if not lines:
            continue
        # PDF 没有表格结构信息，只能用「多列对齐」启发式识别；
        # 识别不到就按普通文本处理，不假装解析出了表格。
        buf: list[str] = []
        table_buf: list[str] = []

        def flush_table() -> None:
            nonlocal table_no, table_buf
            if not table_buf:
                return
            if len(table_buf) >= 3 and _looks_like_table(table_buf):
                table_no += 1
                table = _rows_to_table(table_buf, caption=f"第 {page_no} 页表格",
                                       index=table_no,
                                       locator_prefix=f"第 {page_no} 页 表格 {table_no}")
                if table:
                    doc.blocks.append(ParsedBlock(
                        text="", locator=table.locator_prefix, kind="table", table=table))
                    table_buf = []
                    return
            buf.extend(table_buf)
            table_buf = []

        for line in lines:
            if _looks_like_table([line]) or (_MULTISPACE.search(line.strip()) and
                                             len([p for p in _MULTISPACE.split(line.strip()) if p]) >= 3):
                if buf:
                    doc.blocks.append(ParsedBlock(_clean("\n".join(buf)),
                                                  locator=f"第 {page_no} 页"))
                    buf = []
                table_buf.append(line)
            elif looks_like_plain_heading(line):
                # PDF 没有样式信息，「第一章 总则」这类标题只能靠形态识别
                flush_table()
                if buf:
                    doc.blocks.append(ParsedBlock(_clean("\n".join(buf)),
                                                  locator=f"第 {page_no} 页"))
                    buf = []
                title = line.strip()
                doc.blocks.append(ParsedBlock(title, locator=f"第 {page_no} 页",
                                              heading=title, kind="heading"))
            else:
                flush_table()
                buf.append(line)
        flush_table()
        if buf:
            doc.blocks.append(ParsedBlock(_clean("\n".join(buf)), locator=f"第 {page_no} 页"))
    if not doc.blocks:
        raise ParseError("PDF 未提取到文本（可能是扫描件，需 OCR 支持）")
    return doc


def _parse_docx(path: Path) -> ParsedDocument:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        document = docx.Document(str(path))
    except Exception as exc:
        raise ParseError(f"DOCX 打开失败：{exc}") from exc

    doc = ParsedDocument(meta={"format": "docx"})
    heading = ""
    para = 0
    table_no = 0

    # 按文档真实顺序遍历段落与表格，避免表格位置错乱
    def iter_blocks(parent):
        body = parent.element.body
        for child in body.iterchildren():
            tag = child.tag.split('}')[-1]
            if tag == "p":
                yield Paragraph(child, parent)
            elif tag == "tbl":
                yield Table(child, parent)

    for item in iter_blocks(document):
        if isinstance(item, Paragraph):
            text = _clean(item.text or "")
            if not text:
                continue
            para += 1
            style = (item.style.name or "").lower()
            if style.startswith("heading") or style.startswith("标题"):
                heading = text
                doc.blocks.append(ParsedBlock(text, locator=f"第 {para} 段",
                                              heading=heading, kind="heading"))
            else:
                doc.blocks.append(ParsedBlock(text, locator=f"第 {para} 段", heading=heading))
        else:
            table_no += 1
            parsed: list[list[str]] = []
            for row in item.rows:
                cells = [_cell(c.text or "") for c in row.cells]
                if any(cells):
                    parsed.append(cells)
            if len(parsed) < 2:
                continue
            width = max(len(r) for r in parsed)
            doc.blocks.append(ParsedBlock(
                text="", locator=f"表格 {table_no}", heading=heading, kind="table",
                table=TableData(
                    header=parsed[0] + [""] * (width - len(parsed[0])),
                    rows=[r + [""] * (width - len(r)) for r in parsed[1:]],
                    caption=f"表格 {table_no}", index=table_no,
                    locator_prefix=f"表格 {table_no}",
                ),
            ))

    # 图片：暂不解析内容，只记录数量（视觉模型/OCR 后续接入）
    try:
        for rel in document.part.rels.values():
            if "image" in rel.reltype and not rel.is_external:
                doc.images.append({
                    "name": rel.target_part.partname.split("/")[-1],
                    "mime": rel.target_part.content_type or "image/png",
                    "data": b"",   # 不读入内存，避免大文件占内存
                })
    except Exception:
        pass

    if not doc.blocks:
        raise ParseError("DOCX 未提取到文本或表格")
    return doc


def _parse_xlsx(path: Path) -> ParsedDocument:
    """按工作表整表建模，而不是逐行拆段落。

    700 行数据逐行拆会产生 700 个碎片，既检索不准也浪费索引；
    整表交给分块层按行组切分，每组重复表头，才能既控制长度又保住列语义。
    """
    import openpyxl
    try:
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as exc:
        raise ParseError(f"XLSX 打开失败：{exc}") from exc

    doc = ParsedDocument(meta={"format": "xlsx"})
    table_no = 0
    try:
        for sheet in wb.worksheets:
            rows_iter = sheet.iter_rows(values_only=True)
            header: list[str] | None = None
            body: list[list[str]] = []
            for row in rows_iter:
                cells = [_cell(c) for c in row]
                if not any(cells):
                    continue
                if header is None:
                    header = cells
                    continue
                body.append(cells)
            if header is None or not body:
                continue
            table_no += 1
            width = max(len(header), max(len(r) for r in body))
            header = header + [""] * (width - len(header))
            body = [r + [""] * (width - len(r)) for r in body]
            doc.blocks.append(ParsedBlock(
                text="", locator=f"Sheet:{sheet.title}", heading=sheet.title, kind="table",
                table=TableData(header=header, rows=body, caption=f"工作表 {sheet.title}",
                                index=table_no, locator_prefix=f"Sheet:{sheet.title}"),
            ))
    finally:
        wb.close()
    if not doc.blocks:
        raise ParseError("XLSX 未提取到数据行")
    return doc


def _parse_pptx(path: Path) -> ParsedDocument:
    from pptx import Presentation
    try:
        prs = Presentation(str(path))
    except Exception as exc:
        raise ParseError(f"PPTX 打开失败：{exc}") from exc
    doc = ParsedDocument(meta={"format": "pptx"})
    table_no = 0
    for idx, slide in enumerate(prs.slides, start=1):
        lines: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    text = _clean("".join(r.text for r in para.runs))
                    if text:
                        lines.append(text)
            if getattr(shape, "has_table", False):
                table_no += 1
                parsed: list[list[str]] = []
                for row in shape.table.rows:
                    cells = [_cell(c.text or "") for c in row.cells]
                    if any(cells):
                        parsed.append(cells)
                if len(parsed) >= 2:
                    width = max(len(r) for r in parsed)
                    doc.blocks.append(ParsedBlock(
                        text="", locator=f"第 {idx} 页幻灯片 表格 {table_no}",
                        heading=f"第 {idx} 页", kind="table",
                        table=TableData(
                            header=parsed[0] + [""] * (width - len(parsed[0])),
                            rows=[r + [""] * (width - len(r)) for r in parsed[1:]],
                            caption=f"第 {idx} 页表格", index=table_no,
                            locator_prefix=f"第 {idx} 页 表格 {table_no}",
                        ),
                    ))
        if lines:
            doc.blocks.append(ParsedBlock("\n".join(lines), locator=f"第 {idx} 页幻灯片",
                                          heading=f"第 {idx} 页"))
    if not doc.blocks:
        raise ParseError("PPTX 未提取到文本或表格")
    return doc


def parse_document(path: str | Path) -> ParsedDocument:
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in PLAIN_SUFFIXES:
        return _parse_plain(p)
    if suffix in HTML_SUFFIXES:
        return _parse_html(p)
    if suffix == ".pdf":
        return _parse_pdf(p)
    if suffix == ".docx":
        return _parse_docx(p)
    if suffix == ".xlsx":
        return _parse_xlsx(p)
    if suffix == ".pptx":
        return _parse_pptx(p)
    if suffix in {".doc", ".ppt", ".xls"}:
        raise ParseError(f"旧版 {suffix} 格式不支持，请另存为 {suffix}x 后上传")
    raise ParseError(f"不支持的文件类型：{suffix}")


def sniff_suffix(filename: str) -> str:
    return Path(filename).suffix.lower()
