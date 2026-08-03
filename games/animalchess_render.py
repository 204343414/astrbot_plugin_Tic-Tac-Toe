"""Render a jungle-chess board to PNG.

Geometry
--------
The artwork is a 2135x1436 landscape board holding a 9x7 grid with **no
margin**, so every square is exactly::

    2135 / 9 = 237.222 px wide
    1436 / 7 = 205.143 px tall

Those are deliberately kept as floats and only rounded at draw time; rounding
the cell size first would drift by several pixels by the ninth column.

If the artwork is ever replaced by one with a border, the four constants below
are the only thing that needs to change.

Assets are optional. A missing board or piece image falls back to drawing the
same geometry with text, so the game is playable before any art exists -- and a
broken asset shows up as a visible placeholder rather than a crash.
"""
from __future__ import annotations

import io
import os
from typing import Any

from PIL import Image, ImageDraw

from . import animalchess as ac
from .gomoku_render import _font, has_cjk_font

#: Native size of the supplied board artwork.
BOARD_WIDTH = 2135
BOARD_HEIGHT = 1436
#: Grid origin inside that artwork. Zero because the grid is full-bleed.
GRID_LEFT = 0.0
GRID_TOP = 0.0

CELL_WIDTH = (BOARD_WIDTH - GRID_LEFT * 2) / ac.COLS
CELL_HEIGHT = (BOARD_HEIGHT - GRID_TOP * 2) / ac.ROWS

#: Rendered at half size: 2135px is far larger than any chat bubble, and the
#: upload budget matters more than pixels nobody sees.
SCALE = 0.5

ASSET_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets", "animalchess",
)
BOARD_ASSET = os.path.join(ASSET_DIR, "board.png")
PIECE_DIR = os.path.join(ASSET_DIR, "pieces")

RED_COLOR = (206, 58, 52)
BLUE_COLOR = (48, 104, 190)
PIECE_BG = (250, 246, 236)
TEXT_DARK = (40, 32, 20)
HIGHLIGHT = (250, 196, 40)

#: Fallback board colours, used only when board.png is absent.
FALLBACK_LAND = (226, 202, 150)
FALLBACK_WATER = (128, 186, 214)
FALLBACK_TRAP = (198, 158, 96)
FALLBACK_DEN = (176, 132, 72)
FALLBACK_LINE = (120, 92, 48)


def piece_asset_path(animal: str) -> str:
    """Both sides share one image per animal; colour comes from the ring."""
    return os.path.join(PIECE_DIR, f"{animal}.png")


def square_centre(row: int, col: int) -> tuple[float, float]:
    """Pixel centre of a square in the *native* board resolution."""
    return (
        GRID_LEFT + (col + 0.5) * CELL_WIDTH,
        GRID_TOP + (row + 0.5) * CELL_HEIGHT,
    )


def _is_blank(image: Image.Image) -> bool:
    """True for a fully transparent (or empty) placeholder.

    The repository ships blank PNGs at the right dimensions so the artwork can
    be dropped in by overwriting them. Treating those as real art would render
    an invisible board, so a placeholder must count as *absent* -- otherwise
    the whole point of the text fallback is lost.
    """
    try:
        alpha = image.getchannel("A") if "A" in image.getbands() else None
    except Exception:
        return False
    if alpha is not None:
        return (alpha.getextrema() or (0, 0))[1] == 0
    extrema = image.convert("L").getextrema()
    return bool(extrema and extrema[0] == extrema[1])


def _load_board() -> Image.Image:
    if os.path.exists(BOARD_ASSET):
        try:
            board = Image.open(BOARD_ASSET)
            if not _is_blank(board):
                board = board.convert("RGB")
                if board.size != (BOARD_WIDTH, BOARD_HEIGHT):
                    board = board.resize((BOARD_WIDTH, BOARD_HEIGHT), Image.LANCZOS)
                return board
        except Exception:
            pass
    return _draw_fallback_board()


def _draw_fallback_board() -> Image.Image:
    """A plain but correct board, so the game works before the art lands."""
    image = Image.new("RGB", (BOARD_WIDTH, BOARD_HEIGHT), FALLBACK_LAND)
    draw = ImageDraw.Draw(image)
    for row in range(ac.ROWS):
        for col in range(ac.COLS):
            left = GRID_LEFT + col * CELL_WIDTH
            top = GRID_TOP + row * CELL_HEIGHT
            box = (round(left), round(top),
                   round(left + CELL_WIDTH), round(top + CELL_HEIGHT))
            if ac.is_water(row, col):
                fill = FALLBACK_WATER
            elif (row, col) in ac.TRAPS[ac.RED] or (row, col) in ac.TRAPS[ac.BLUE]:
                fill = FALLBACK_TRAP
            elif (row, col) in ac.DENS.values():
                fill = FALLBACK_DEN
            else:
                fill = FALLBACK_LAND
            draw.rectangle(box, fill=fill, outline=FALLBACK_LINE, width=3)

    label_font = _font(46)
    for side, den in ac.DENS.items():
        x, y = square_centre(*den)
        colour = RED_COLOR if side == ac.RED else BLUE_COLOR
        text = "穴" if has_cjk_font() else "DEN"
        draw.text((x, y), text, font=label_font, fill=colour, anchor="mm")
    for side, traps in ac.TRAPS.items():
        for trap in traps:
            x, y = square_centre(*trap)
            text = "陷" if has_cjk_font() else "T"
            draw.text((x, y), text, font=label_font, fill=TEXT_DARK, anchor="mm")
    return image


_PIECE_CACHE: dict[tuple[str, int], Image.Image | None] = {}


def _load_piece(animal: str, size: int) -> Image.Image | None:
    key = (animal, size)
    if key in _PIECE_CACHE:
        return _PIECE_CACHE[key]
    path = piece_asset_path(animal)
    image: Image.Image | None = None
    if os.path.exists(path):
        try:
            loaded = Image.open(path).convert("RGBA")
            if not _is_blank(loaded):
                image = loaded.resize((size, size), Image.LANCZOS)
        except Exception:
            image = None
    _PIECE_CACHE[key] = image
    return image


def clear_asset_cache() -> None:
    """Drop cached artwork so replaced files are picked up without a restart."""
    _PIECE_CACHE.clear()


def _draw_piece(image: Image.Image, draw: ImageDraw.ImageDraw,
                row: int, col: int, side: str, animal: str,
                highlight: bool = False) -> None:
    x, y = square_centre(row, col)
    radius = min(CELL_WIDTH, CELL_HEIGHT) * 0.42
    colour = RED_COLOR if side == ac.RED else BLUE_COLOR
    box = (round(x - radius), round(y - radius),
           round(x + radius), round(y + radius))

    draw.ellipse(box, fill=PIECE_BG, outline=colour, width=round(radius * 0.16))

    art = _load_piece(animal, round(radius * 1.5))
    if art is not None:
        image.paste(art, (round(x - art.width / 2), round(y - art.height / 2)), art)
    else:
        # No artwork yet: the animal's name at the same size and position, so
        # the board reads correctly and the layout never shifts once art lands.
        text = ac.NAMES[animal] if has_cjk_font() else animal[:2].upper()
        draw.text((x, y), text, font=_font(round(radius * 1.1)),
                  fill=colour, anchor="mm")

    if highlight:
        pad = radius * 0.22
        draw.ellipse((box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad),
                     outline=HIGHLIGHT, width=round(radius * 0.14))


def render_board(state: dict[str, Any]) -> bytes:
    """Draw the current position and return PNG bytes.

    The move hint is drawn *into* the picture: the image message carries no
    caption, so putting it in the body would print it twice.
    """
    image = _load_board().copy()
    draw = ImageDraw.Draw(image)

    last = state.get("last_move") or {}
    last_to = tuple(last["to"]) if last.get("to") else None
    for (row, col), (side, animal) in sorted(state["pieces"].items()):
        _draw_piece(image, draw, row, col, side, animal,
                    highlight=(row, col) == last_to)

    _draw_banner(image, draw, state)

    if SCALE != 1.0:
        image = image.resize(
            (round(BOARD_WIDTH * SCALE), round(BOARD_HEIGHT * SCALE)),
            Image.LANCZOS,
        )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _draw_banner(image: Image.Image, draw: ImageDraw.ImageDraw,
                 state: dict[str, Any]) -> None:
    """Status on top, move hint at the bottom, both inside the picture."""
    status = ac.status_text(state)
    hint = ac.move_hint(state)
    if not has_cjk_font():
        status = "".join(ch for ch in status if ord(ch) < 128).strip() or "Jungle"
        hint = "".join(ch for ch in hint if ord(ch) < 128).strip()

    for text, y, size in ((status, 44, 52), (hint, BOARD_HEIGHT - 44, 42)):
        if not text:
            continue
        font = _font(size)
        box = draw.textbbox((0, 0), text, font=font)
        width, height = box[2] - box[0], box[3] - box[1]
        pad = 18
        draw.rectangle(
            (BOARD_WIDTH / 2 - width / 2 - pad, y - height / 2 - pad,
             BOARD_WIDTH / 2 + width / 2 + pad, y + height / 2 + pad),
            fill=(252, 250, 244),
            outline=(150, 122, 70),
            width=2,
        )
        draw.text((BOARD_WIDTH / 2, y), text, font=font,
                  fill=TEXT_DARK, anchor="mm")
