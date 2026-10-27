"""实测脚本：打印每个 fixture 的解析与分块真实数字。

用途：验证「大表格按行组切分 + 重复表头」是否真的落地，
并检查是否存在超过 embedding 安全长度的块。

用法（仓库根目录）：
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe backend/scripts/inspect_chunks.py
    # 只看某个文件
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe backend/scripts/inspect_chunks.py 员工薪酬明细_700行.docx

退出码：存在超长块时返回 1，便于 CI 拦截。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app import config                     # noqa: E402
from app.services import chunking, parser  # noqa: E402

FIXTURES = ROOT / "demo_data" / "fixtures"

# 表格块上限：表格走独立预算；正文块上限用 DEFAULT_CHUNK_SIZE。
# 留 1.3 倍余量用于「整表一块」的小表与标题前缀，超过即视为不安全。
TABLE_LIMIT = config.TABLE_CHUNK_BUDGET
TEXT_LIMIT = int(config.DEFAULT_CHUNK_SIZE * 1.3)


def _row_ranges(units) -> list[tuple[int, int]]:
    return [(u.meta["row_start"], u.meta["row_end"]) for u in units if u.meta]


def _ranges_contiguous(ranges: list[tuple[int, int]], total: int) -> bool:
    if not ranges:
        return False
    if ranges[0][0] != 1 or ranges[-1][1] != total:
        return False
    return all(cur[0] == prev[1] + 1 for prev, cur in zip(ranges, ranges[1:]))


def inspect(path: Path) -> bool:
    """打印单个文件的解析/分块情况。返回 True 表示没有超长块。"""
    print("=" * 78)
    print(f"文件：{path.name}")
    try:
        parsed = parser.parse_document(path)
    except Exception as exc:                      # 解析失败也要报出来，不能静默
        print(f"  解析失败：{exc.__class__.__name__}: {exc}")
        return False

    units = chunking.split_blocks(parsed.blocks)
    table_units = [u for u in units if u.kind == "table"]
    text_units = [u for u in units if u.kind != "table"]

    print(f"  格式={parsed.meta.get('format')}  解析块={len(parsed.blocks)}  "
          f"表格={parsed.table_count}  图片={len(parsed.images)}  "
          f"分块={len(units)}（表格 {len(table_units)} / 文本 {len(text_units)}）")

    # ---- 每张表的切分情况
    table_blocks = [b for b in parsed.blocks if b.kind == "table"]
    for tb in table_blocks:
        table = tb.table
        own = [u for u in table_units
               if (u.meta or {}).get("caption") == table.caption
               and (u.meta or {}).get("total_rows") == table.total_rows]
        if not own:
            print(f"  [表格 {table.index}] {table.caption}  "
                  f"{table.total_rows} 行 -> 未切出块")
            continue
        lens = [len(u.text) for u in own]
        ranges = _row_ranges(own)
        ok = _ranges_contiguous(ranges, table.total_rows)
        print(f"  [表格 {table.index}] {table.caption}  "
              f"表头={table.header[:4]}  数据行={table.total_rows}  列={table.col_count}")
        print(f"      切块={len(own)}  字符 min/avg/max={min(lens)}/"
              f"{sum(lens) // len(lens)}/{max(lens)}  上限={TABLE_LIMIT}")
        print(f"      行区间={'连续无遗漏 ✓' if ok else '不连续 ✗'}  "
              f"首块={ranges[0] if ranges else None}  末块={ranges[-1] if ranges else None}")
        print(f"      行区间明细：{ranges[:4]}{' ...' if len(ranges) > 4 else ''}")

    # ---- 超长块检查
    over = []
    for u in table_units:
        if len(u.text) > TABLE_LIMIT:
            over.append(("表格", len(u.text), u.locator))
    for u in text_units:
        if len(u.text) > TEXT_LIMIT:
            over.append(("文本", len(u.text), u.locator))

    if over:
        print(f"  ✗ 存在 {len(over)} 个超过安全长度的块：")
        for kind, ln, loc in over[:10]:
            limit = TABLE_LIMIT if kind == "表格" else TEXT_LIMIT
            print(f"      [{kind}] {ln} 字符 > 上限 {limit}  @ {loc}")
        return False

    if units:
        print(f"  ✓ 无超长块（表格上限 {TABLE_LIMIT} / 文本上限 {TEXT_LIMIT}）")
    return True


def main(argv: list[str]) -> int:
    if not FIXTURES.exists():
        print(f"夹具目录不存在：{FIXTURES}\n请先运行 python scripts/make_fixtures.py")
        return 2

    if argv:
        targets = [FIXTURES / name for name in argv]
    else:
        targets = sorted(FIXTURES.iterdir())

    print(f"表格块预算 TABLE_CHUNK_BUDGET={TABLE_LIMIT}  "
          f"正文预算 DEFAULT_CHUNK_SIZE={config.DEFAULT_CHUNK_SIZE}"
          f"（上限 {TEXT_LIMIT}）")

    all_ok = True
    for path in targets:
        if not path.is_file():
            print(f"跳过不存在的文件：{path}")
            continue
        if not inspect(path):
            all_ok = False
    print("=" * 78)
    print("结论：全部块长度安全 ✓" if all_ok else "结论：存在超长块或解析失败 ✗")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
