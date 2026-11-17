"""把牛角棋棋盘画成图片：坐标系直接取自用户给的参照图。

**所有坐标都是从参照图（1280×1164 的白底黑线图）上量出来、再按图宽高归一化的**，
不是自己摆的几何。换图不用改算法，改下面两张表就行：

* ``OUTLINE`` —— 板子的外轮廓（56 个点，从参照图的墨线内侧沿着轮廓等距取）。
* ``POINTS`` —— 11 个洞眼的位置，键是点名，值是归一化坐标。

连线的画法还是老规矩：``hornchess.neighbors()`` 决定谁连谁（每点只连编号
±1／±2 的点），所以 19 条线和参照图一一对应，线上的点也正好落在洞眼上。

棋子用 emoji 位图（``assets/emoji/{cow,man,woman}.png``，OpenMoji，CC BY-SA 4.0），
用户要求的 🐮 / 👨 / 👩。字体画不出彩色 emoji，所以这里走贴图；图缺失时退化成
画一个带汉字的棋子，不至于整盘画不出来。

尺寸固定 620×720（竖版），卡片里声明的像素必须与此完全一致（这个坑在斗兽棋上
踩过：声明和实际不符会让图片被拉伸，甚至干脆不渲染）。
"""
from __future__ import annotations

import io
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from . import hornchess as hc
from .gomoku_render import _font, has_cjk_font

#: 卡片里声明的尺寸，必须等于下面算出来的实际像素。
WIDTH = 620
HEIGHT = 720

#: 参照图的原尺寸（坐标表就是按它归一化的）。
REF_W = 1280.0
REF_H = 1164.0

#: 参照图缩到卡片上的比例，以及左上角偏移。
SCALE = 0.503
OFFSET_X = 12.0
OFFSET_Y = 78.0

#: 板子外轮廓，归一化坐标（见模块开头的说明）。
OUTLINE: tuple[tuple[float, float], ...] = (
    (0.2219, 0.0146), (0.1685, 0.0189), (0.1164, 0.0266), (0.0659, 0.0387),
    (0.0196, 0.0559), (0.0614, 0.0911), (0.1024, 0.1281), (0.1425, 0.1678),
    (0.1810, 0.2103), (0.2172, 0.2543), (0.2500, 0.2998), (0.2801, 0.3466),
    (0.3068, 0.3949), (0.3297, 0.4450), (0.3487, 0.4968), (0.3638, 0.5504),
    (0.3750, 0.6058), (0.3814, 0.6634), (0.3844, 0.7225), (0.3828, 0.7823),
    (0.3773, 0.8403), (0.3680, 0.8965), (0.3925, 0.9227), (0.4475, 0.9227),
    (0.5025, 0.9227), (0.5575, 0.9227), (0.6126, 0.9227), (0.6676, 0.9227),
    (0.7226, 0.9227), (0.7776, 0.9227), (0.8326, 0.9227), (0.8876, 0.9227),
    (0.9426, 0.9227), (0.9430, 0.8675), (0.9375, 0.8095), (0.9305, 0.7522),
    (0.9217, 0.6957), (0.9109, 0.6401), (0.8986, 0.5853), (0.8851, 0.5309),
    (0.8694, 0.4776), (0.8515, 0.4252), (0.8309, 0.3742), (0.8063, 0.3249),
    (0.7774, 0.2775), (0.7440, 0.2323), (0.7060, 0.1896), (0.6651, 0.1523),
    (0.6220, 0.1205), (0.5771, 0.0936), (0.5305, 0.0713), (0.4827, 0.0524),
    (0.4332, 0.0378), (0.3824, 0.0266), (0.3303, 0.0189), (0.2766, 0.0155),
)

#: 11 个洞眼在参照图上的位置（归一化）。
POINTS: dict[str, tuple[float, float]] = {
    "A1": (0.1719, 0.0773),      # 牛角尖
    "B1": (0.3062, 0.2002),
    "B2": (0.3969, 0.1117),
    "C1": (0.4023, 0.3187),
    "C2": (0.5789, 0.2139),
    "D1": (0.4695, 0.4630),
    "D2": (0.7063, 0.3849),
    "E1": (0.5016, 0.6237),
    "E2": (0.7789, 0.5842),
    "F1": (0.4977, 0.8333),      # 底角（牛的出口）
    "F2": (0.8297, 0.8333),
}

#: 棋子贴图。字体没有彩色 emoji，只能贴图；文件缺失时退化成汉字棋子。
ASSET_DIR = Path(__file__).resolve().parent.parent / "assets" / "emoji"

BG = (255, 255, 255)
BOARD = (255, 255, 255)
INK = (10, 10, 10)
LABEL = (90, 90, 90)
FAINT = (166, 166, 166)

BULL_RING = (196, 74, 62)
MAN_RING = (44, 92, 150)
WOMAN_RING = (168, 60, 120)
LAST_MOVE = (250, 196, 40)
ESCAPE_RING = (196, 74, 62)

JPEG_QUALITY = 92

#: 棋盘的字体是中文黑体，没有彩色 emoji 和 ZWJ 组合符，画上去就是一串豆腐块。
#: 状态行里那几个图标留给 QQ 的 markdown，出图时剔掉。
_ICON_RE = re.compile("[\U0001F000-\U0001FAFF\u200d\ufe0f]+")

HOLE_RADIUS = 6.5        # 普通洞眼
CUP_RADIUS = 19.5        # 底角那两个"窝"
PIECE_RADIUS = 24.0      # 棋子圆的半径
LINE_WIDTH = 4


def _to_canvas(nx: float, ny: float) -> tuple[float, float]:
    return OFFSET_X + nx * REF_W * SCALE, OFFSET_Y + ny * REF_H * SCALE


def point_xy(index: int) -> tuple[float, float]:
    """点 → 画布坐标。"""
    nx, ny = POINTS[hc.name_of(index)]
    return _to_canvas(nx, ny)


def _edges() -> list[tuple[int, int]]:
    """所有连线，去重（每条边只画一次）。"""
    seen: set[tuple[int, int]] = set()
    for index in range(len(hc.NAMES)):
        for neighbor in hc.neighbors(index):
            seen.add((min(index, neighbor), max(index, neighbor)))
    return sorted(seen)


@lru_cache(maxsize=8)
def _emoji(asset: str, size: int) -> Image.Image | None:
    path = ASSET_DIR / f"{asset}.png"
    try:
        image = Image.open(path).convert("RGBA")
    except (OSError, ValueError):
        return None
    return image.resize((size, size), Image.LANCZOS)


def _label_xy(index: int) -> tuple[float, float, str]:
    """点名的位置：放在棋子之外、板子内侧，别压住线。"""
    x, y = point_xy(index)
    if index == hc.TIP:
        # 尖点在板子的左上角上，往上写会顶到板边，往左写正好落在板子里
        return x - 28, y + 2, "rm"
    if hc.column_of(index) == 0:
        return x - 40, y, "rm"
    return x + 40, y, "lm"


def _draw_piece(image: Image.Image, draw: ImageDraw.ImageDraw, index: int,
                asset: str, glyph: str, ring: tuple) -> None:
    x, y = point_xy(index)
    r = PIECE_RADIUS
    draw.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255),
                 outline=ring, width=3)
    size = int(r * 1.95)
    art = _emoji(asset, size)
    if art is not None:
        image.paste(art, (int(x - size / 2), int(y - size / 2)), art)
    else:
        draw.text((x, y), glyph, font=_font(int(r * 1.05)),
                  anchor="mm", fill=ring)


def render_board(state: dict[str, Any], banner: bool = True) -> bytes:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)

    # 板子：白底 + 一圈黑边（和参照图一样的白板黑线）
    plate = [_to_canvas(nx, ny) for nx, ny in OUTLINE]
    draw.polygon(plate, fill=BOARD)
    draw.line(plate + [plate[0]], fill=INK, width=LINE_WIDTH + 1, joint="curve")

    # 19 条线
    for a, b in _edges():
        draw.line([point_xy(a), point_xy(b)], fill=INK, width=LINE_WIDTH)

    # 洞眼：底角画成"窝"（牛要冲出去的口子），其余画成实心小圆
    for index in range(len(hc.NAMES)):
        x, y = point_xy(index)
        if index in hc.ESCAPE_POINTS:
            draw.ellipse((x - CUP_RADIUS, y - CUP_RADIUS,
                          x + CUP_RADIUS, y + CUP_RADIUS),
                         fill=(255, 255, 255), outline=ESCAPE_RING, width=2)
        draw.ellipse((x - HOLE_RADIUS, y - HOLE_RADIUS,
                      x + HOLE_RADIUS, y + HOLE_RADIUS), fill=INK)

    # 上一手：两个黄圈，谁走到哪儿一眼看见
    last = state.get("last_move") or {}
    for key in ("frm", "to"):
        square = last.get(key)
        if isinstance(square, int) and 0 <= square < len(hc.NAMES):
            x, y = point_xy(square)
            draw.ellipse((x - PIECE_RADIUS - 5, y - PIECE_RADIUS - 5,
                          x + PIECE_RADIUS + 5, y + PIECE_RADIUS + 5),
                         outline=LAST_MOVE, width=4)

    # 棋子：两个赶牛的人（👨／👩 固定到各自的子）＋ 牛（🐮）
    for slot, position in enumerate(state["humans"]):
        _draw_piece(image, draw, position,
                    hc.PIECE_ASSETS[slot % len(hc.PIECE_ASSETS)],
                    hc.PIECE_MARKS[slot % len(hc.PIECE_MARKS)],
                    MAN_RING if slot == 0 else WOMAN_RING)
    _draw_piece(image, draw, state["bull"], "cow", hc.MARKS[hc.BULL], BULL_RING)

    # 点名
    label_font = _font(19)
    for index, name in enumerate(hc.NAMES):
        lx, ly, anchor = _label_xy(index)
        draw.text((lx, ly), name, font=label_font, anchor=anchor, fill=LABEL)

    # 顶部／底部各一行小字，再一行状态
    small = _font(19)
    draw.text((WIDTH / 2, 30), "⬆ 牛角尖：人要把牛赶到这儿堵死",
              font=small, anchor="mm", fill=FAINT)
    draw.text((WIDTH / 2, HEIGHT - 64), "⬇ 底角＝牛的出口　·　人只能往尖上赶，同一条横杆可左右挪",
              font=small, anchor="mm", fill=FAINT)
    if banner:
        text = _ICON_RE.sub("", hc.status_text(state)).strip()
        draw.text((WIDTH / 2, HEIGHT - 30), text,
                  font=_font(25), anchor="mm", fill=INK)

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue()
