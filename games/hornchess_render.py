"""把牛角棋棋盘画成图片：一个**高瘦的牛角**，4 行 7 个点。

和另外几个棋一样，渲染单独一个文件——规则模块要能在没有 Pillow 的环境里跑测试。

画法照民间真盘：左边一条竖边（A1-B1-C1-D1），右边一条斜边（A1-B2-C2-D2），
三道横杆（B1-B2、C1-C2、D1-D2），再加两条**互相平行的长斜线**
（B2-C1、C2-D1，都是"从下一行左端连到上一行右端"）。所以这个盘本身左右不对称，
真盘就是这样，不是画歪了。

尺寸固定 620×720（竖版，符合牛角的高瘦比例），卡片里声明的像素必须与此完全一致
（这个坑在斗兽棋上踩过：声明和实际不符会让图片被拉伸，甚至干脆不渲染）。

每个点都标上名字（A1…D2），因为走法按钮里写的就是这些名字。不标的话，玩家
得自己数格子才能把"🧑‍🌾 C1→B2"和图上某个圆圈对上。
"""
from __future__ import annotations

import io
import re
from typing import Any

from PIL import Image, ImageDraw

from . import hornchess as hc
from .gomoku_render import _font, has_cjk_font

#: 卡片里声明的尺寸，必须等于下面算出来的实际像素。
WIDTH = 620
HEIGHT = 720

# 牛角的外形：左边竖边在 LEFT_X，尖在 TOP_Y，右下角在 RIGHT_X / BASE_Y。
LEFT_X = 158.0
RIGHT_X = 468.0
TOP_Y = 118.0
BASE_Y = 566.0
ROWS_GAP = (BASE_Y - TOP_Y) / (len(hc.ROWS) - 1)

BG = (252, 249, 242)
LINE = (138, 112, 78)
FRAME = (96, 72, 48)
TEXT = (92, 70, 46)
FAINT = (196, 184, 166)

BULL_FILL = (196, 74, 62)       # 牛：红
BULL_RING = (128, 40, 32)
HUMAN_FILL = (86, 138, 196)     # 人：蓝
HUMAN_RING = (44, 84, 132)
GLYPH = (252, 248, 240)

LAST_MOVE = (250, 196, 40)
ESCAPE_RING = (196, 74, 62)
LATERAL = (120, 160, 120)

JPEG_QUALITY = 88

#: 棋盘的字体是中文黑体，没有彩色 emoji 和 ZWJ 组合符，画上去就是一串豆腐块。
#: 状态行里那几个图标（🐂🧑‍🌾）留给 QQ 的 markdown，出图时剔掉。
_ICON_RE = re.compile("[\U0001F000-\U0001FAFF\u200d\ufe0f]+")

DISC_RADIUS = 30
DOT_RADIUS = 7


def point_xy(index: int) -> tuple[float, float]:
    """点 → 画布坐标。

    左列（A1、B1、C1、D1）贴在竖边上；右列（B2、C2、D2）落在斜边上，
    斜边从牛角尖直通右下角，所以越往下右列越靠右。
    """
    depth = hc.DEPTH[index]
    y = TOP_Y + depth * ROWS_GAP
    left = hc.column_of(index) == 0
    if left:
        return LEFT_X, y
    ratio = depth / (len(hc.ROWS) - 1)
    return LEFT_X + (RIGHT_X - LEFT_X) * ratio, y


def _edges() -> list[tuple[int, int]]:
    """所有连线，去重（每条边只画一次）。"""
    seen: set[tuple[int, int]] = set()
    for index in range(len(hc.NAMES)):
        for neighbor in hc.neighbors(index):
            seen.add((min(index, neighbor), max(index, neighbor)))
    return sorted(seen)


def _label_xy(index: int) -> tuple[float, float]:
    x, y = point_xy(index)
    if hc.DEPTH[index] == 0:
        return x - 6, y - 52            # 牛角尖的名字放头顶
    if hc.column_of(index) == 0:
        return x - 44, y                # 左列放左边
    return x + 44, y                    # 右列放右边


def _disc(draw: ImageDraw.ImageDraw, index: int, fill: tuple, ring: tuple,
          glyph: str) -> None:
    x, y = point_xy(index)
    draw.ellipse((x - DISC_RADIUS, y - DISC_RADIUS,
                  x + DISC_RADIUS, y + DISC_RADIUS),
                 fill=fill, outline=ring, width=4)
    font = _font(32 if has_cjk_font() else 28)
    draw.text((x, y), glyph, font=font, anchor="mm", fill=GLYPH)


def render_board(state: dict[str, Any], banner: bool = True) -> bytes:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)

    # 牛角的外框：左竖边、右斜边，画粗一点，让它一眼就是个"角"
    apex = point_xy(hc.TIP)
    bottom_right = point_xy(len(hc.NAMES) - 1)
    draw.line([apex, (LEFT_X, BASE_Y), bottom_right],
              fill=FRAME, width=6, joint="curve")
    draw.line([apex, bottom_right], fill=FRAME, width=6)

    # 内部连线：三道横杆 + 两条平行长斜线，比外框细
    # （A1 那两条已经由外框画过，跳过）
    for a, b in _edges():
        if a == hc.TIP:
            continue
        draw.line([point_xy(a), point_xy(b)], fill=LINE, width=5)

    # 底角画粗圈：那是牛要冲出去的口子，玩家一眼能看见目标
    for corner in hc.ESCAPE_POINTS:
        x, y = point_xy(corner)
        draw.ellipse((x - 44, y - 44, x + 44, y + 44),
                     outline=ESCAPE_RING, width=3)

    # 上一手
    last = state.get("last_move") or {}
    for key in ("frm", "to"):
        square = last.get(key)
        if isinstance(square, int) and 0 <= square < len(hc.NAMES):
            x, y = point_xy(square)
            draw.ellipse((x - 40, y - 40, x + 40, y + 40),
                         outline=LAST_MOVE, width=6)

    # 点
    for index in range(len(hc.NAMES)):
        x, y = point_xy(index)
        draw.ellipse((x - DOT_RADIUS, y - DOT_RADIUS,
                      x + DOT_RADIUS, y + DOT_RADIUS), fill=FRAME)

    # 棋子
    for human in state["humans"]:
        _disc(draw, human, HUMAN_FILL, HUMAN_RING, "人")
    _disc(draw, state["bull"], BULL_FILL, BULL_RING, "牛")

    # 点名
    label_font = _font(22)
    for index, name in enumerate(hc.NAMES):
        x, y = _label_xy(index)
        anchor = "mm" if hc.DEPTH[index] == 0 else ("rm" if hc.column_of(index) == 0 else "lm")
        draw.text((x, y), name, font=label_font, anchor=anchor, fill=TEXT)

    # 顶部/底部各一行小字：哪边是出口、人怎么走，再一行状态
    small = _font(20)
    draw.text((WIDTH / 2, 34), "⬆ 牛角尖：人要把牛赶到这里堵死",
              font=small, anchor="mm", fill=FAINT)
    draw.text((WIDTH / 2, BASE_Y + 62),
              "⬇ 牛冲到底角就跑了　·　人只能往尖上赶，同一横杆可左右挪",
              font=small, anchor="mm", fill=FAINT)
    if banner:
        text = _ICON_RE.sub("", hc.status_text(state)).strip()
        draw.text((WIDTH / 2, HEIGHT - 46), text,
                  font=_font(26), anchor="mm", fill=TEXT)

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue()
