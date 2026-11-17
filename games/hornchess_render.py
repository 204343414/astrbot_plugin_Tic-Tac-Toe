"""把牛角棋棋盘画成图片：一个上尖下宽的三角形，10 个点。

和另外几个棋一样，渲染单独一个文件——规则模块要能在没有 Pillow 的环境里跑测试。

尺寸固定 720×620，卡片里声明的像素必须与此完全一致（这个坑在斗兽棋上踩过：
声明和实际不符会让图片被拉伸，甚至干脆不渲染）。

每个点都标上名字（A1…D4），因为走法按钮里写的就是这些名字。不标的话，玩家
得自己数格子才能把"🧑‍🌾 D1→C1"和图上某个圆圈对上。
"""
from __future__ import annotations

import io
from typing import Any

from PIL import Image, ImageDraw

from . import hornchess as hc
from .gomoku_render import _font, has_cjk_font

#: 卡片里声明的尺寸，必须等于下面算出来的实际像素。
WIDTH = 720
HEIGHT = 620

SPACING = 170          # 同一层相邻点的水平间距
ROW_HEIGHT = 132       # 层与层之间的垂直间距
TOP = 104              # 牛角尖的纵坐标
CENTER_X = WIDTH / 2

BG = (252, 249, 242)
LINE = (138, 112, 78)
FRAME = (96, 72, 48)
TEXT = (92, 70, 46)

BULL_FILL = (196, 74, 62)       # 牛：红
BULL_RING = (128, 40, 32)
HUMAN_FILL = (86, 138, 196)     # 人：蓝
HUMAN_RING = (44, 84, 132)
GLYPH = (252, 248, 240)

LAST_MOVE = (250, 196, 40)
ESCAPE_RING = (196, 74, 62)

JPEG_QUALITY = 88


def point_xy(index: int) -> tuple[float, float]:
    """点 → 画布坐标。每层比上一层多一个点，所以整层居中。"""
    depth = hc.DEPTH[index]
    col = hc.column_of(index)
    x = CENTER_X + (col - depth / 2) * SPACING
    y = TOP + depth * ROW_HEIGHT
    return x, y


def _edges() -> list[tuple[int, int]]:
    """所有连线，去重（每条边只画一次）。"""
    seen: set[tuple[int, int]] = set()
    for index in range(len(hc.NAMES)):
        for neighbor in hc.neighbors(index):
            edge = (min(index, neighbor), max(index, neighbor))
            seen.add(edge)
    return sorted(seen)


def _disc(draw: ImageDraw.ImageDraw, index: int, fill: tuple, ring: tuple,
          glyph: str) -> None:
    x, y = point_xy(index)
    radius = 34
    draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                 fill=fill, outline=ring, width=4)
    font = _font(34 if has_cjk_font() else 30)
    draw.text((x, y), glyph if has_cjk_font() else glyph, font=font,
              anchor="mm", fill=GLYPH)


def render_board(state: dict[str, Any], banner: bool = True) -> bytes:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)

    # 连线
    for a, b in _edges():
        draw.line([point_xy(a), point_xy(b)], fill=LINE, width=5)

    # 两个底角画粗一点：那是牛要冲出去的口子，玩家一眼能看见目标
    for corner in hc.ESCAPE_POINTS:
        x, y = point_xy(corner)
        draw.ellipse((x - 46, y - 46, x + 46, y + 46),
                     outline=ESCAPE_RING, width=4)

    # 上一手
    last = state.get("last_move") or {}
    for key in ("frm", "to"):
        square = last.get(key)
        if isinstance(square, int) and 0 <= square < len(hc.NAMES):
            x, y = point_xy(square)
            draw.ellipse((x - 44, y - 44, x + 44, y + 44),
                         outline=LAST_MOVE, width=6)

    # 点
    for index in range(len(hc.NAMES)):
        x, y = point_xy(index)
        draw.ellipse((x - 7, y - 7, x + 7, y + 7), fill=FRAME)

    # 棋子
    for human in state["humans"]:
        _disc(draw, human, HUMAN_FILL, HUMAN_RING, "人")
    _disc(draw, state["bull"], BULL_FILL, BULL_RING, "牛")

    # 点名（画在点的左上方，避开棋子）
    label_font = _font(22)
    for index, name in enumerate(hc.NAMES):
        x, y = point_xy(index)
        draw.text((x - 40, y - 44), name, font=label_font,
                  anchor="rm", fill=TEXT)

    if banner:
        text = hc.status_text(state)
        draw.text((CENTER_X, HEIGHT - 34), text, font=_font(26),
                  anchor="mm", fill=TEXT)

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue()
