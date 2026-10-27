"""生成应用图标：512×512 PNG + 多尺寸 ICO。

设计意图：深蓝底 + 白色几何图形，体现「知识库 / 文档 / 问答」。
用纯几何绘制，不依赖外部素材，保证任何机器上可复现。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "frontend" / "build"
OUT.mkdir(parents=True, exist_ok=True)

SIZE = 512
BG_TOP = (37, 71, 122)      # 稍亮的深蓝，缩到 16px 时不至于糊成一团黑
BG_BOTTOM = (16, 34, 60)
ACCENT = (72, 148, 232)     # 亮蓝，用于强调
ACCENT_DARK = (52, 118, 196)
WHITE = (255, 255, 255)


def _rounded_bg(img: Image.Image) -> None:
    """圆角方形底：Windows 图标习惯用圆角，直角会显得生硬。"""
    mask = Image.new("L", (SIZE, SIZE), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=int(SIZE * 0.20), fill=255)

    bg = Image.new("RGB", (SIZE, SIZE), BG_TOP)
    gd = ImageDraw.Draw(bg)
    # 对角渐变比纯垂直更有层次，也更耐缩小
    for y in range(SIZE):
        for_t = y / (SIZE - 1)
        color = tuple(int(BG_TOP[i] * (1 - for_t) + BG_BOTTOM[i] * for_t) for i in range(3))
        gd.line([(0, y), (SIZE, y)], fill=color)

    img.paste(bg, (0, 0), mask)


def _draw_document(img: Image.Image) -> None:
    """三张叠放的文档卡片，暗示「多知识库」。整体略偏左上，为气泡留出空间。"""
    d = ImageDraw.Draw(img)
    w, h = int(SIZE * 0.34), int(SIZE * 0.42)
    cx, cy = int(SIZE * 0.455), int(SIZE * 0.445)

    # 后两张：偏移 + 逐渐变亮的描边，制造堆叠层次
    for dx, dy, fill, outline in [
        (-30, -22, (48, 82, 130), (78, 118, 172)),
        (-15, -11, (58, 96, 150), (92, 134, 190)),
    ]:
        box = [cx - w // 2 + dx, cy - h // 2 + dy, cx + w // 2 + dx, cy + h // 2 + dy]
        d.rounded_rectangle(box, radius=20, fill=fill, outline=outline, width=3)

    # 前面一张：白色卡片
    box = [cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2]
    d.rounded_rectangle(box, radius=20, fill=WHITE)

    # 卡片上的文本行：首行用亮蓝加粗，其余灰色，形成「标题 + 正文」观感
    line_x0 = cx - w // 2 + 26
    line_x1 = cx + w // 2 - 26
    y = cy - h // 2 + 40
    for idx, ratio in enumerate([1.0, 0.74, 0.90, 0.58]):
        end = line_x0 + int((line_x1 - line_x0) * ratio)
        color = ACCENT if idx == 0 else (170, 192, 216)
        height = 16 if idx == 0 else 13
        d.rounded_rectangle([line_x0, y, end, y + height], radius=7, fill=color)
        y += 34


def _draw_dialogue(img: Image.Image) -> None:
    """右下角对话气泡，暗示「问答」。压在卡片右下角，形成整体构图。"""
    d = ImageDraw.Draw(img)
    cx, cy = int(SIZE * 0.715), int(SIZE * 0.715)
    w, h = int(SIZE * 0.29), int(SIZE * 0.205)

    # 白色描边让气泡从深底上跳出来，缩小时也不糊
    box = [cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2]
    d.rounded_rectangle([box[0] - 4, box[1] - 4, box[2] + 4, box[3] + 4],
                        radius=26, fill=(240, 245, 252))
    d.rounded_rectangle(box, radius=22, fill=ACCENT)

    # 气泡小尾巴（朝左下）
    tail = [
        (cx - w // 2 + 20, cy + h // 2 - 6),
        (cx - w // 2 + 2, cy + h // 2 + 30),
        (cx - w // 2 + 52, cy + h // 2 - 6),
    ]
    d.polygon(tail, fill=ACCENT)

    # 三个白点，表示「正在回答」
    r = 10
    for i in range(3):
        px = cx - 36 + i * 36
        d.ellipse([px - r, cy - r, px + r, cy + r], fill=WHITE)


def build() -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), BG_TOP)
    _rounded_bg(img)
    _draw_document(img)
    _draw_dialogue(img)
    return img


def main() -> None:
    img = build()

    png = OUT / "icon.png"
    img.save(png, "PNG")
    print(f"  {png}  {png.stat().st_size / 1024:.1f} KB")

    # ICO 需要多尺寸，Windows 在任务栏/资源管理器/桌面用不同尺寸
    ico = OUT / "icon.ico"
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(ico, format="ICO", sizes=sizes)
    print(f"  {ico}  {ico.stat().st_size / 1024:.1f} KB")

    # 顺带给登录页/关于页用的小图
    small = OUT / "icon-128.png"
    img.resize((128, 128), Image.LANCZOS).save(small, "PNG")
    print(f"  {small}  {small.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
