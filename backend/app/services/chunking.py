"""分块策略。这里的关键决策都写在注释里，因为它直接决定检索质量。

一、文本块
    按段落聚合到目标长度；**标题变化强制切分**，overlap 只在同一标题内生效。
    否则「考勤」和「请假」会被并进同一块，命中时无法区分，Wiki 也会串主题。

二、表格块（100~700 行的 Word/PDF/Excel 表格）
    逐行拆是最差的做法：700 行 → 700 个碎片，检索命中后拿不到上下文，
    且每行都短、embedding 表示极弱。整表一块也不行：远超 embedding 上下文，
    模型只能截断，等于只索引了前几十行。
    采用「行组切分 + 表头重复」：
      - 每组按字符预算装行，不按固定行数，避免长行溢出；
      - 每组都重复表头，保证每块自身可读、可被独立检索；
      - 表名/工作表名写进标题，检索时能区分是哪张表；
      - locator 记录行区间（第 21-40 行），证据可定位到原始行。
    行数少的小表（能整块放进预算）不切，保持完整上下文。

三、Wiki 章节
    按 Markdown 标题层级切分，标题路径带上页面名，避免同名小节混淆。

四、长度预算
    块长按字符控制，并预留 embedding 上下文余量。中文 1 字约 1 token，
    默认 700 字符对应远小于主流 embedding 的 512~8192 token 窗口。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .. import config
from .parser import ParsedBlock, TableData

_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*|\n+")
_EN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{1,}")
_CN_TOKEN = re.compile(r"[\u4e00-\u9fff]{2,}")

STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were", "will", "have",
    "has", "not", "but", "you", "your", "our", "their", "they", "them", "can", "may", "should",
    "的", "了", "和", "与", "及", "或", "在", "是", "有", "对", "为", "我们", "你们", "他们",
    "这个", "那个", "可以", "需要", "进行", "以及", "一个", "如果", "因为", "所以", "但是",
    "并且", "通过", "相关", "以下", "上述", "如下", "其中", "同时", "已经", "没有", "什么",
}


@dataclass
class TextUnit:
    text: str
    heading: str
    locator: str
    ordinal: int
    kind: str = "text"          # text | table
    meta: dict | None = None    # 表格：{"row_start","row_end","total_rows","cols"}


# ------------------------------------------------------------------ 表格切分
def _row_to_line(header: list[str], row: list[str]) -> str:
    parts = []
    for i, cell in enumerate(row):
        if not cell:
            continue
        key = header[i] if i < len(header) and header[i] else f"列{i+1}"
        parts.append(f"{key}: {cell}")
    return "；".join(parts)


def _rows_to_markdown(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join(["---"] * len(header)) + " |"]
    for r in rows:
        lines.append("| " + " | ".join(c or "" for c in r) + " |")
    return "\n".join(lines)


def split_table(table: TableData, *, size: int, ordinal_start: int,
                heading: str) -> list[TextUnit]:
    """表格 → 若干可独立检索的块，每块都带表头。

    小表整块保留；大表按字符预算切行组，并重复表头。

    行组大小不能只按字符预算切：700 行 × 9 列的表如果每块只装 7 行，
    会切出 100 个碎片，检索时命中某块却看不到足够上下文，
    也会让索引体积和向量调用量翻倍。因此设一个下限行数，
    在不超过预算的前提下尽量多装行；预算实在装不下时才按预算切。
    """
    units: list[TextUnit] = []
    header = table.header or []
    total = table.total_rows
    if total == 0:
        return units

    # 单行渲染成「列名: 值」比 Markdown 竖线表更省字符，也更利于关键词命中；
    # 行数不多时用 Markdown 表格保留可读性。
    use_markdown = total <= 25

    def render(chunk_rows: list[list[str]], start: int, end: int) -> str:
        title = table.caption or heading or "表格"
        head = f"表：{title}（共 {total} 行）"
        if total > 25:
            head += f"，本段为第 {start}-{end} 行"
        if use_markdown:
            body = _rows_to_markdown(header, chunk_rows)
        else:
            body = "\n".join(_row_to_line(header, r) for r in chunk_rows)
        return f"{head}\n{body}"

    # 整表能放下就整表一块
    whole = render(table.rows, 1, total)
    if len(whole) <= size or total <= 3:
        units.append(TextUnit(
            text=whole, heading=heading, locator=f"{table.locator_prefix} 全表",
            ordinal=ordinal_start, kind="table",
            meta={"row_start": 1, "row_end": total, "total_rows": total,
                  "cols": table.col_count, "caption": table.caption},
        ))
        return units

    # 大表：按字符预算装行。
    #
    # 这里的取舍：**字符预算是硬约束，行数是软目标**。
    # 原因：embedding 模型的上下文窗口按 token 计，超了就会被静默截断，
    # 导致块尾数据根本没进索引——这比"块偏碎"严重得多。
    # 因此宁可切碎，也不超预算；同时用 TABLE_CHUNK_BUDGET 单独给表格
    # 一个比正文更大的预算（表格信息密度高，同样 token 能承载更多有效内容）。
    header_overhead = len(render([], 1, 1)) + 24
    budget = max(config.TABLE_CHUNK_BUDGET - header_overhead, 200)

    groups: list[tuple[int, int, list[list[str]]]] = []
    cur: list[list[str]] = []
    cur_len = 0
    start_row = 1
    for idx, row in enumerate(table.rows, start=1):
        line_len = len(_row_to_line(header, row)) + 1
        if cur and cur_len + line_len > budget:
            groups.append((start_row, idx - 1, cur))
            cur = []
            cur_len = 0
            start_row = idx
        cur.append(row)
        cur_len += line_len
    if cur:
        groups.append((start_row, total, cur))

    for gi, (s, e, rows) in enumerate(groups):
        units.append(TextUnit(
            text=render(rows, s, e), heading=heading,
            locator=f"{table.locator_prefix} 第 {s}-{e} 行",
            ordinal=ordinal_start + gi, kind="table",
            meta={"row_start": s, "row_end": e, "total_rows": total,
                  "cols": table.col_count, "caption": table.caption,
                  "part": gi + 1, "parts": len(groups)},
        ))
    return units


# ------------------------------------------------------------------ 文本切分
def split_blocks(blocks: list[ParsedBlock], *, size: int | None = None,
                 overlap: int | None = None) -> list[TextUnit]:
    """按段落聚合到目标长度，超长段落按句切开；表格走独立分支。"""
    size = size or config.DEFAULT_CHUNK_SIZE
    # 必须用 `is None` 而不是 `or`：overlap=0 是合法输入（表示不要重叠），
    # 用 `or` 会被当成假值替换成默认 120，调用方显式关掉重叠却毫无效果。
    if overlap is None:
        overlap = config.DEFAULT_CHUNK_OVERLAP
    overlap = max(int(overlap), 0)
    units: list[TextUnit] = []
    buf: list[str] = []
    buf_len = 0
    heading = ""
    locator = ""
    ordinal = 0

    def emit(text: str, head: str, loc: str) -> None:
        nonlocal ordinal
        text = text.strip()
        if len(text) < 8:
            return
        units.append(TextUnit(text=text, heading=head, locator=loc, ordinal=ordinal))
        ordinal += 1

    def flush(*, carry_overlap: bool = True) -> None:
        """收尾当前缓冲。

        carry_overlap=True 时把尾部 overlap 字符带到下一块（同一标题内），
        避免长段落被切断后，跨边界的句子两边都读不到完整语义。
        换标题时必须传 False：否则上一节的内容会混进新章节，
        检索命中后分不清属于哪一节，Wiki 生成也会串主题。
        """
        nonlocal buf, buf_len
        if buf:
            emit("\n".join(buf), heading, locator)
            if carry_overlap and overlap > 0:
                tail = "\n".join(buf)[-overlap:]
                buf = [tail] if tail else []
                buf_len = len(tail)
            else:
                buf = []
                buf_len = 0

    for block in blocks:
        if block.kind == "heading":
            flush(carry_overlap=False)
            heading = block.text
            locator = block.locator
            continue

        if block.kind == "table" and block.table:
            flush(carry_overlap=False)
            table_units = split_table(block.table, size=size, ordinal_start=ordinal,
                                      heading=block.heading or heading)
            for u in table_units:
                units.append(u)
            ordinal += len(table_units)
            locator = ""
            continue

        if block.kind == "image":
            flush(carry_overlap=False)
            emit(block.text, block.heading or heading, block.locator or "图片")
            locator = ""
            continue

        block_heading = block.heading or heading
        if block_heading != heading:
            flush(carry_overlap=False)
            heading = block_heading
            locator = block.locator
        text = block.text.strip()
        if not text:
            continue
        if len(text) > size * 1.6:
            flush(carry_overlap=False)
            sentences = [s for s in _SENT_SPLIT.split(text) if s and s.strip()]
            acc: list[str] = []
            acc_len = 0
            for sent in sentences:
                if acc_len + len(sent) > size and acc:
                    emit(" ".join(acc), heading, block.locator)
                    acc = acc[-1:] if overlap else []
                    acc_len = sum(len(x) for x in acc)
                acc.append(sent)
                acc_len += len(sent)
            if acc:
                emit(" ".join(acc), heading, block.locator)
            continue
        if buf_len + len(text) > size and buf:
            # 同一标题内的长度溢出：保留 overlap，避免跨块句子两边都不完整。
            # 注意与上面几处区分——那些是标题切换/表格/图片边界，必须清零。
            flush(carry_overlap=True)
            locator = block.locator
        if not locator:
            locator = block.locator
        buf.append(text)
        buf_len += len(text) + 1
    flush(carry_overlap=False)
    return units


def split_markdown(md: str, *, size: int | None = None) -> list[TextUnit]:
    """Wiki 页面按章节切分，保留 heading_path 层级。"""
    size = size or config.DEFAULT_CHUNK_SIZE
    units: list[TextUnit] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    ordinal = 0

    def path() -> str:
        return " / ".join(t for _, t in stack)

    def flush() -> None:
        nonlocal buf, ordinal
        text = "\n".join(buf).strip()
        if len(text) >= 8:
            units.append(TextUnit(text=text, heading=path(),
                                  locator=f"章节 {ordinal + 1}", ordinal=ordinal))
            ordinal += 1
        buf = []

    for line in md.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)$", line.strip())
        if m:
            flush()
            level = len(m.group(1))
            title = m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            continue
        buf.append(line)
        if sum(len(x) for x in buf) > size:
            flush()
    flush()
    return units


# ------------------------------------------------------------------ 关键词
def extract_keywords(text: str, limit: int = 24) -> list[str]:
    """轻量关键词：中文二元组 + 英文词，按词频加权。不依赖分词库，保证离线可用。"""
    low = text.lower()
    counts: dict[str, int] = {}
    for tok in _EN_TOKEN.findall(low):
        if tok in STOPWORDS or len(tok) < 2:
            continue
        counts[tok] = counts.get(tok, 0) + 2
    for run in _CN_TOKEN.findall(text):
        if run in STOPWORDS:
            continue
        if len(run) <= 6:
            counts[run] = counts.get(run, 0) + 2
        for i in range(len(run) - 1):
            bigram = run[i:i + 2]
            if bigram in STOPWORDS:
                continue
            counts[bigram] = counts.get(bigram, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [w for w, _ in ranked]


def keyword_field(text: str) -> str:
    return " ".join(extract_keywords(text))


def chunk_hash(text: str, heading: str = "") -> str:
    return hashlib.sha256(f"{heading}\x00{text}".encode("utf-8")).hexdigest()


def slugify(title: str, fallback: str = "page") -> str:
    slug = re.sub(r"[^\w\u4e00-\u9fff]+", "-", title.strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        slug = fallback
    return slug[:120]
