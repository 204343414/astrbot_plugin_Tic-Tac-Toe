"""把国际象棋棋盘画成图片。

为什么单独一个文件
------------------
和 ``animalchess_render`` / ``gomoku_render`` 一样：规则模块要能在没有 Pillow
的环境里跑测试，画图的东西一律隔开。

尺寸
----
宽度固定 720，和另外两个游戏的卡片一致（QQ 文档给的上限就是 720）。高度
756 是**刻意不是正方形**的：卡片里声明尺寸必须和实际像素一模一样，而正方形
图片在部分客户端上渲染异常，长宽不同最省事。

棋子用"圆形底 + 汉字"而不是 Unicode 象棋字符（♔♕♖…）：字符要靠字体覆盖，
缺字会画成豆腐块，而且黑白两套在某些字体里是同一个字形。汉字在
``gomoku_render._font`` 已经保证有 CJK 覆盖，颜色直接表达阵营。

风格刻意贴近斗兽棋的水彩木色，三个游戏摆在一个群里不显得是三个人做的。
"""
from __future__ import annotations

import io
from typing import Any

from PIL import Image, ImageDraw

from . import chess as ch
from .gomoku_render import _font, has_cjk_font

#: 卡片里声明的尺寸，必须与下面算出来的实际像素一致。
WIDTH = 720
HEIGHT = 756

MARGIN_LEFT = 32
MARGIN_RIGHT = 16
MARGIN_TOP = 24
#: 底部留白放 a-h 坐标。
FOOTER = 60
CELL = (WIDTH - MARGIN_LEFT - MARGIN_RIGHT) // ch.SIZE      # 84

BOARD_LEFT = MARGIN_LEFT
BOARD_TOP = MARGIN_TOP
BOARD_RIGHT = BOARD_LEFT + CELL * ch.SIZE
BOARD_BOTTOM = BOARD_TOP + CELL * ch.SIZE

LIGHT = (238, 228, 208)
DARK = (178, 146, 112)
BORDER = (96, 70, 48)
FRAME = (250, 246, 238)
TEXT = (86, 62, 40)

WHITE_FILL = (252, 250, 244)
WHITE_RING = (96, 74, 52)
WHITE_GLYPH = (58, 44, 28)
BLACK_FILL = (74, 66, 60)
BLACK_RING = (28, 24, 20)
BLACK_GLYPH = (246, 240, 228)

LAST_MOVE = (250, 208, 84)
CHECK_RING = (206, 58, 52)

JPEG_QUALITY = 88


def _cell_box(index: int) -> tuple[float, float, float, float]:
    row, col = ch.row_of(index), ch.col_of(index)
    left = BOARD_LEFT + col * CELL
    top = BOARD_TOP + row * CELL
    return left, top, left + CELL, top + CELL


def _draw_piece(draw: ImageDraw.ImageDraw, index: int, piece: str) -> None:
    left, top, right, bottom = _cell_box(index)
    cx, cy = (left + right) / 2, (top + bottom) / 2
    radius = CELL * 0.40
    box = (cx - radius, cy - radius, cx + radius, cy + radius)
    is_white = ch.color_of(piece) == ch.WHITE
    draw.ellipse(box, fill=WHITE_FILL if is_white else BLACK_FILL,
                 outline=WHITE_RING if is_white else BLACK_RING,
                 width=max(2, round(radius * 0.14)))
    glyph = ch.NAMES[ch.kind_of(piece)]
    if not has_cjk_font():
        glyph = ch.kind_of(piece)          # 没有中文字体时退回字母，别画豆腐块
    font = _font(round(CELL * 0.52))
    draw.text((cx, cy), glyph, font=font, anchor="mm",
              fill=WHITE_GLYPH if is_white else BLACK_GLYPH)


def render_board(state: dict[str, Any], banner: bool = True) -> bytes:
    """画当前局面，返回 JPEG 字节。

    ``banner`` 为 False 时不留底部提示区（卡片模式下正文已经写了走法提示，
    画在图里就是重复）。
    """
    # 画布尺寸**永远**是 WIDTH×HEIGHT：卡片里声明的像素必须和实际一致，
    # 而"卡片模式不画提示"只是少画一行字，不能顺手把画布改矮。
    height = HEIGHT
    image = Image.new("RGB", (WIDTH, height), FRAME)
    draw = ImageDraw.Draw(image)

    # 棋盘格
    for row in range(ch.SIZE):
        for col in range(ch.SIZE):
            index = ch.index_of(row, col)
            box = (BOARD_LEFT + col * CELL, BOARD_TOP + row * CELL,
                   BOARD_LEFT + (col + 1) * CELL, BOARD_TOP + (row + 1) * CELL)
            draw.rectangle(box, fill=LIGHT if (row + col) % 2 == 0 else DARK)

    # 上一手：起点和落点都标出来，玩家一眼看到"刚才那步下在哪"
    last = state.get("last_move") or {}
    for key in ("frm", "to"):
        square = last.get(key)
        if isinstance(square, int) and 0 <= square < 64:
            draw.rectangle(_cell_box(square), outline=LAST_MOVE, width=5)

    for index, piece in enumerate(state["board"]):
        if piece:
            _draw_piece(draw, index, piece)

    # 被将军的王套一圈红
    color = state.get("turn") or ch.WHITE
    if ch.in_check(state["board"], color):
        square = ch.king_square(state["board"], color)
        if square >= 0:
            left, top, right, bottom = _cell_box(square)
            pad = CELL * 0.08
            draw.ellipse((left + pad, top + pad, right - pad, bottom - pad),
                         outline=CHECK_RING, width=6)

    draw.rectangle((BOARD_LEFT, BOARD_TOP, BOARD_RIGHT, BOARD_BOTTOM),
                   outline=BORDER, width=4)

    # 坐标：横 a-h 在下面，竖 8-1 在左边。和国际象棋的习惯一致（白方在下）。
    label_font = _font(round(CELL * 0.30))
    for col in range(ch.SIZE):
        cx = BOARD_LEFT + (col + 0.5) * CELL
        draw.text((cx, BOARD_BOTTOM + 6), ch.FILES[col], font=label_font,
                  anchor="mt", fill=TEXT)
    for row in range(ch.SIZE):
        cy = BOARD_TOP + (row + 0.5) * CELL
        draw.text((BOARD_LEFT - 8, cy), ch.RANKS[row], font=label_font,
                  anchor="rm", fill=TEXT)

    if banner:
        text = ch.describe_move(state, last) or "引用本图回复走法，例如 e2e4"
        hint_font = _font(round(CELL * 0.30))
        draw.text(((WIDTH) / 2, height - FOOTER / 2 + 4), text,
                  font=hint_font, anchor="mm", fill=TEXT)

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue()
