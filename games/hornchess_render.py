"""把牛角棋棋盘画成图片：照实拍棋盘的样子——一个高瘦、微弯的牛角，6 行 11 个点。

和另外几个棋一样，渲染单独一个文件——规则模块要能在没有 Pillow 的环境里跑测试。

画的东西和实拍盘一一对应：

* 一块**牛角形的木板**（左边缘微弯、右边缘外张，上端收尖、下端是平的底边）；
* 板上的 19 条线：左列 5 条、右列 5 条、5 道横杆、4 条平行的长斜线；
* 11 个洞眼，洞眼旁边标 A1…F2 —— 走法按钮里写的就是这些名字，不标玩家对不上。

坐标是照着用户实拍的照片量出来的相对位置（左列中间往外鼓、右列越往下越靠右），
所以看起来就是真盘那个"高瘦牛角"，不是对称的等腰三角形。

尺寸固定 620×720（竖版，符合牛角的高瘦比例），卡片里声明的像素必须与此完全一致
（这个坑在斗兽棋上踩过：声明和实际不符会让图片被拉伸，甚至干脆不渲染）。
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

# 每行的纵坐标（第 0 行是牛角尖，最后一行是底角）。
ROW_TOP = 104.0
ROW_GAP = 92.0

#: 每行的左列／右列横坐标。数是从实拍照片上量的：左列先往右挪、中间往外鼓一点，
#: 右列越往下越靠右（牛角张开），所以左右不对称——真盘就这样。
COLUMN_X: tuple[tuple[float, float], ...] = (
    (170.0, 170.0),
    (205.0, 262.0),
    (250.0, 330.0),
    (282.0, 386.0),
    (296.0, 424.0),
    (276.0, 452.0),
)

BG = (247, 244, 236)
WOOD = (238, 226, 198)
WOOD_EDGE = (206, 186, 146)
LINE = (128, 104, 72)
FRAME = (88, 68, 44)
TEXT = (92, 70, 46)
FAINT = (170, 156, 132)

BULL_FILL = (196, 74, 62)       # 牛：红
BULL_RING = (128, 40, 32)
HUMAN_FILL = (86, 138, 196)     # 人：蓝
HUMAN_RING = (44, 84, 132)
GLYPH = (252, 248, 240)

LAST_MOVE = (250, 196, 40)
ESCAPE_RING = (196, 74, 62)

JPEG_QUALITY = 88

#: 棋盘的字体是中文黑体，没有彩色 emoji 和 ZWJ 组合符，画上去就是一串豆腐块。
#: 状态行里那几个图标（🐂🧑‍🌾）留给 QQ 的 markdown，出图时剔掉。
_ICON_RE = re.compile("[\U0001F000-\U0001FAFF\u200d\ufe0f]+")

DISC_RADIUS = 27
DOT_RADIUS = 6


def point_xy(index: int) -> tuple[float, float]:
    """点 → 画布坐标。左列贴左边、右列贴右边，行号决定纵坐标。"""
    depth = hc.DEPTH[index]
    left, right = COLUMN_X[depth]
    return (left if hc.column_of(index) == 0 else right), ROW_TOP + depth * ROW_GAP


def _edges() -> list[tuple[int, int]]:
    """所有连线，去重（每条边只画一次）。"""
    seen: set[tuple[int, int]] = set()
    for index in range(len(hc.NAMES)):
        for neighbor in hc.neighbors(index):
            seen.add((min(index, neighbor), max(index, neighbor)))
    return sorted(seen)


def _board_outline() -> list[tuple[float, float]]:
    """牛角形木板的轮廓：四边各往外让出一点余量，上端是平的（像实拍那块板）。"""
    lefts = [point_xy(i) for i in range(len(hc.NAMES)) if hc.column_of(i) == 0]
    rights = [point_xy(i) for i in range(len(hc.NAMES)) if hc.column_of(i) != 0]
    apex, base = lefts[0], lefts[-1][1] + 58
    points: list[tuple[float, float]] = [
        (apex[0] - 56, apex[1] - 30),          # 左上角
        (rights[0][0] + 20, apex[1] - 30),     # 上沿
    ]
    points += [(x + 56, y) for x, y in rights[1:]]        # 右边缘（外张的斜边）
    points.append((rights[-1][0] + 40, base))             # 右下角
    points.append((lefts[-1][0] - 44, base))              # 左下角
    points += [(x - 56, y) for x, y in reversed(lefts[1:])]   # 左边缘（微弯的竖边）
    return points


def _label_xy(index: int) -> tuple[float, float]:
    x, y = point_xy(index)
    if hc.DEPTH[index] == 0:
        return x + 4, y - 46            # 牛角尖的名字放头顶
    if hc.column_of(index) == 0:
        return x - 40, y                # 左列放左边
    return x + 40, y                    # 右列放右边


def _disc(draw: ImageDraw.ImageDraw, index: int, fill: tuple, ring: tuple,
          glyph: str) -> None:
    x, y = point_xy(index)
    draw.ellipse((x - DISC_RADIUS, y - DISC_RADIUS,
                  x + DISC_RADIUS, y + DISC_RADIUS),
                 fill=fill, outline=ring, width=4)
    font = _font(30 if has_cjk_font() else 26)
    draw.text((x, y), glyph, font=font, anchor="mm", fill=GLYPH)


def render_board(state: dict[str, Any], banner: bool = True) -> bytes:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)

    # 木板：牛角形的一块，浅木色 + 一圈深一点的边
    plate = _board_outline()
    draw.polygon(plate, fill=WOOD)
    draw.line(plate + [plate[0]], fill=WOOD_EDGE, width=4, joint="curve")

    # 19 条线
    for a, b in _edges():
        draw.line([point_xy(a), point_xy(b)], fill=LINE, width=5)

    # 底角画个圈：那是牛要冲出去的口子，玩家一眼能看见目标
    for corner in hc.ESCAPE_POINTS:
        x, y = point_xy(corner)
        draw.ellipse((x - 40, y - 40, x + 40, y + 40),
                     outline=ESCAPE_RING, width=3)

    # 上一手
    last = state.get("last_move") or {}
    for key in ("frm", "to"):
        square = last.get(key)
        if isinstance(square, int) and 0 <= square < len(hc.NAMES):
            x, y = point_xy(square)
            draw.ellipse((x - 37, y - 37, x + 37, y + 37),
                         outline=LAST_MOVE, width=6)

    # 洞眼
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

    # 顶部／底部各一行小字：哪边是尖、哪边是出口，再一行状态
    small = _font(20)
    draw.text((WIDTH / 2, 34), "⬆ 牛角尖：人要把牛赶到这里堵死",
              font=small, anchor="mm", fill=FAINT)
    draw.text((WIDTH / 2, ROW_TOP + (len(hc.ROWS) - 1) * ROW_GAP + 76),
              "⬇ 底角＝牛的出口　·　人只能往尖上赶，同杆可横挪",
              font=small, anchor="mm", fill=FAINT)
    if banner:
        text = _ICON_RE.sub("", hc.status_text(state)).strip()
        draw.text((WIDTH / 2, HEIGHT - 32), text,
                  font=_font(26), anchor="mm", fill=TEXT)

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue()
