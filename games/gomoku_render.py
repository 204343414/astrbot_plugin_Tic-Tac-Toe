"""Render a gomoku board to PNG.

Kept apart from the rules so ``gomoku.py`` stays importable (and testable)
without Pillow. Avatars are fetched from QQ's third-party app CDN:

    https://thirdqq.qlogo.cn/qqapp/{appid}/{member_openid}/640

which is the only way to get a picture for an OpenID -- there is no
member-info API in group scope.
"""
from __future__ import annotations

import io
from typing import Any

from PIL import Image, ImageDraw

from . import gomoku as g

AVATAR_TEMPLATE = "https://thirdqq.qlogo.cn/qqapp/{appid}/{member_openid}/640"

CELL = 40
MARGIN = 46
HEADER = 96
BOARD_PX = CELL * (g.SIZE - 1)
WIDTH = BOARD_PX + MARGIN * 2
HEIGHT = HEADER + BOARD_PX + MARGIN * 2

BG = (232, 199, 138)
LINE = (90, 66, 30)
BLACK_STONE = (28, 28, 30)
WHITE_STONE = (248, 248, 250)
TEXT = (60, 44, 20)
HEADER_BG = (250, 245, 235)
HIGHLIGHT = (214, 62, 62)
STAR_POINTS = (3, 7, 11)


_FONT_CACHE: dict[int, Any] = {}
_CJK_PROBE = "棋"

#: Searched in order; the first font that can actually draw a CJK glyph wins.
#: Nicknames are usually Chinese, and a font without those glyphs renders them
#: as tofu boxes -- worse than useless on a board that names both players.
_FONT_HINTS = (
    "NotoSansCJK", "NotoSerifCJK", "SourceHanSans", "SourceHanSerif",
    "wqy-zenhei", "wqy-microhei", "msyh", "simhei", "PingFang", "Hiragino",
    "DroidSansFallback", "ArialUnicode",
)


def _can_render_cjk(font) -> bool:
    """True only when the font really has the glyph.

    Measuring the bounding box is not enough: a font without CJK coverage still
    draws a "tofu" box with a non-zero size. Comparing the rendered bitmap
    against a private-use codepoint (which can never have a glyph) tells the
    two apart -- that is why boards came out full of squares.
    """
    try:
        glyph = bytes(font.getmask(_CJK_PROBE))
        missing = bytes(font.getmask("\ue000"))
        return bool(glyph) and glyph != missing
    except Exception:
        return False


#: Shipped with the plugin so a bare Docker image still renders Chinese.
#: WenQuanYi Micro Hei, Apache-2.0, ~5 MB, full GBK coverage. See
#: assets/fonts/README.md for why this is bundled rather than required.
BUNDLED_FONT = "assets/fonts/wqy-microhei.ttc"


def bundled_font_path() -> str:
    import os

    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), BUNDLED_FONT
    )


def _discover_font_paths() -> list[str]:
    """System fonts first, then the bundled one as a guaranteed floor.

    A host that has Noto or Source Han installed gets the nicer face; a bare
    container still gets Chinese instead of a wall of tofu.
    """
    import glob
    import os

    found: list[str] = []
    roots = (
        "/usr/share/fonts", "/usr/local/share/fonts",
        os.path.expanduser("~/.fonts"),
        "/System/Library/Fonts", "C:/Windows/Fonts",
    )
    for root in roots:
        if not os.path.isdir(root):
            continue
        for pattern in ("**/*.ttc", "**/*.otf", "**/*.ttf"):
            found.extend(glob.glob(os.path.join(root, pattern), recursive=True))
    # Prefer fonts whose name hints at CJK support.
    def rank(path: str) -> int:
        name = os.path.basename(path)
        for index, hint in enumerate(_FONT_HINTS):
            if hint.lower() in name.lower():
                return index
        return len(_FONT_HINTS)

    ordered = sorted(set(found), key=rank)
    bundled = bundled_font_path()
    if os.path.exists(bundled) and bundled not in ordered:
        ordered.append(bundled)
    return ordered


def _font(size: int):
    """A font that can actually draw Chinese, or the default as a last resort."""
    from PIL import ImageFont

    cached = _FONT_CACHE.get(size)
    if cached is not None:
        return cached
    _FONT_CACHE["cjk"] = True
    for path in _discover_font_paths():
        try:
            font = ImageFont.truetype(path, size)
        except Exception:
            continue
        if _can_render_cjk(font):
            _FONT_CACHE[size] = font
            return font
    # Reaching here means even the bundled font failed to load, which is a
    # broken install rather than a missing system package. Take any real font
    # so the board still renders, and say so once in the log -- silently
    # switching the whole UI to English is what made this confusing before.
    for path in _discover_font_paths():
        try:
            font = ImageFont.truetype(path, size)
        except Exception:
            continue
        _FONT_CACHE[size] = font
        _FONT_CACHE["cjk"] = False
        return font
    fallback = ImageFont.load_default()
    _FONT_CACHE[size] = fallback
    _FONT_CACHE["cjk"] = False
    return fallback


def has_cjk_font() -> bool:
    """Whether the active font can draw Chinese.

    Should now always be True: the plugin ships its own CJK font, so this is a
    corruption check rather than a supported mode.
    """
    if "cjk" not in _FONT_CACHE:
        _font(20)
        _FONT_CACHE.setdefault("cjk", True)
    return bool(_FONT_CACHE.get("cjk", True))


def _circle_avatar(data: bytes, size: int) -> Image.Image | None:
    try:
        avatar = Image.open(io.BytesIO(data)).convert("RGBA").resize(
            (size, size), Image.LANCZOS
        )
    except Exception:
        return None
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    avatar.putalpha(mask)
    return avatar


def _ascii_only(text: str) -> str:
    """Strip characters the current font cannot draw."""
    return "".join(ch for ch in text if ord(ch) < 128).strip()


def _fit(text: str) -> str:
    return text if has_cjk_font() else _ascii_only(text)


def render_board(
    state: dict[str, Any],
    avatars: dict[str, bytes] | None = None,
) -> bytes:
    """Draw the board and return PNG bytes.

    The move hint is drawn *inside* the picture: QQ shows an image message with
    little room for a caption, and players need the instruction next to the
    board they are about to quote.
    """
    avatars = avatars or {}
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    font_small = _font(18)
    font_name = _font(20)
    font_status = _font(22)

    draw.rectangle((0, 0, WIDTH, HEADER), fill=HEADER_BG)
    _draw_players(image, draw, state, avatars, font_name, font_status)

    top = HEADER + MARGIN
    left = MARGIN
    for i in range(g.SIZE):
        offset = i * CELL
        draw.line((left, top + offset, left + BOARD_PX, top + offset), fill=LINE)
        draw.line((left + offset, top, left + offset, top + BOARD_PX), fill=LINE)

    for row in STAR_POINTS:
        for col in STAR_POINTS:
            x, y = left + col * CELL, top + row * CELL
            draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=LINE)

    # Labels only along the top and left edges: repeating them on all four
    # sides just adds visual noise on a phone screen.
    for i in range(g.SIZE):
        x = left + i * CELL
        draw.text((x, top - 26), g.COLUMN_LETTERS[i],
                  font=font_small, fill=TEXT, anchor="mm")
        y = top + i * CELL
        draw.text((left - 28, y), str(i + 1),
                  font=font_small, fill=TEXT, anchor="mm")

    radius = CELL // 2 - 2
    win_line = set(g.winning_line(state["board"]))
    last = state.get("last_move", -1)
    for index, mark in enumerate(state["board"]):
        if not mark:
            continue
        row, col = divmod(index, g.SIZE)
        x, y = left + col * CELL, top + row * CELL
        fill = BLACK_STONE if mark == g.BLACK else WHITE_STONE
        draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                     fill=fill, outline=LINE)
        if index in win_line:
            draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                         outline=HIGHLIGHT, width=3)
        elif index == last:
            dot = 4
            marker = WHITE_STONE if mark == g.BLACK else BLACK_STONE
            draw.ellipse((x - dot, y - dot, x + dot, y + dot), fill=marker)

    hint = _fit(g.move_hint(state))
    if hint:
        draw.text((WIDTH // 2, HEIGHT - MARGIN // 2 - 4), hint,
                  font=_font(19), fill=TEXT, anchor="mm")

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _draw_players(image, draw, state, avatars, font_name, font_status) -> None:
    size = 56
    y = (HEADER - size) // 2
    turn = state.get("turn")
    over = g.is_over(state["board"])

    for mark, x in ((g.BLACK, 20), (g.WHITE, WIDTH - 20 - size)):
        avatar = _circle_avatar(avatars.get(mark, b""), size) if avatars.get(mark) else None
        if avatar is not None:
            image.paste(avatar, (x, y), avatar)
        else:
            draw.ellipse((x, y, x + size, y + size),
                         fill=BLACK_STONE if mark == g.BLACK else WHITE_STONE,
                         outline=LINE)
        # Ring the player whose turn it is, so the image alone says who moves.
        if not over and mark == turn:
            draw.ellipse((x - 4, y - 4, x + size + 4, y + size + 4),
                         outline=HIGHLIGHT, width=3)
        label = g.player_label(state, mark) or ("AI" if state["mode"] == g.MODE_AI else "待加入")
        side = ("黑" if mark == g.BLACK else "白") if has_cjk_font() else (
            "B" if mark == g.BLACK else "W"
        )  # only reachable on a corrupt install; see has_cjk_font()
        anchor_x = x + size // 2
        name = _fit(str(label)) or ("AI" if state["mode"] == g.MODE_AI else "?")
        draw.text((anchor_x, y + size + 14), f"{side} {name}"[:14],
                  font=font_name, fill=TEXT, anchor="mm")

    # Emoji fonts are frequently absent on servers, so the in-image status uses
    # plain words even though the chat-side text may use ⚫/⚪.
    status = g.status_text(state)
    for emoji, word in ((g.MARKS[g.BLACK], "黑"), (g.MARKS[g.WHITE], "白")):
        status = status.replace(emoji, word)
    for emoji in ("🎉", "🤝", "🤖"):
        status = status.replace(emoji, "")
    draw.text((WIDTH // 2, HEADER // 2), _fit(status.strip()) or "Gomoku",
              font=font_status, fill=TEXT, anchor="mm")


def avatar_url(appid: str, member_openid: str) -> str:
    from urllib.parse import quote

    if not appid or not member_openid:
        return ""
    return AVATAR_TEMPLATE.format(
        appid=quote(str(appid), safe=""),
        member_openid=quote(str(member_openid), safe=""),
    )
