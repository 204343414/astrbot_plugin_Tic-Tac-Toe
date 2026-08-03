"""Gomoku (五子棋) rules and coordinate parsing.

Why this game has no board buttons
----------------------------------
A 15x15 board is 225 cells, while a QQ keyboard holds at most 5x5 = 25 buttons.
Even a 9x9 board would not fit. So the board is rendered as an **image** and
moves arrive as text: the player quotes the board and replies with a coordinate
like ``H8``.

That also sidesteps a second limit: QQ refuses to put rich media and a keyboard
in the same message, so a picture *plus* buttons would cost two messages per
turn out of a five-message passive budget.

Pure logic only -- no AstrBot, no Hub, no Pillow. Rendering lives in
``games/gomoku_render.py`` so the rules stay testable on their own.
"""
from __future__ import annotations

import random
import re
from typing import Any

SIZE = 15
EMPTY = ""
BLACK = "B"
WHITE = "W"
MARKS = {BLACK: "⚫", WHITE: "⚪"}
WIN_LENGTH = 5

MODE_AI = "ai"
MODE_PVP = "pvp"

LEVEL_EASY = "easy"
LEVEL_NORMAL = "normal"
LEVEL_HARD = "hard"
AI_LEVELS = (LEVEL_EASY, LEVEL_NORMAL, LEVEL_HARD)
LEVEL_LABELS = {LEVEL_EASY: "轻松", LEVEL_NORMAL: "普通", LEVEL_HARD: "困难"}
#: Chance the AI ignores its best move. Gomoku is not a solved draw like
#: tic-tac-toe, but a greedy scorer still beats a casual player every time.
LEVEL_SLIP = {LEVEL_EASY: 0.55, LEVEL_NORMAL: 0.2, LEVEL_HARD: 0.0}

COLUMN_LETTERS = "ABCDEFGHIJKLMNO"[:SIZE]
_COORD_RE = re.compile(r"^\s*([A-Oa-o])\s*(\d{1,2})\s*$")

DIRECTIONS = ((1, 0), (0, 1), (1, 1), (1, -1))


def new_board() -> list[str]:
    return [EMPTY] * (SIZE * SIZE)


def index_of(row: int, col: int) -> int:
    return row * SIZE + col


def parse_coordinate(text: object) -> int:
    """Parse "H8" into a board index, or -1 when unparseable.

    Column is a letter A-O, row is 1-15 counted from the top, matching how the
    rendered image is labelled.
    """
    match = _COORD_RE.match(str(text or ""))
    if not match:
        return -1
    col = COLUMN_LETTERS.index(match.group(1).upper())
    row = int(match.group(2)) - 1
    if not 0 <= row < SIZE:
        return -1
    return index_of(row, col)


def format_coordinate(index: int) -> str:
    if not 0 <= index < SIZE * SIZE:
        return ""
    return f"{COLUMN_LETTERS[index % SIZE]}{index // SIZE + 1}"


def winning_line(board: list[str]) -> tuple[int, ...]:
    """Return the five indexes that win, or () when nobody has won."""
    for row in range(SIZE):
        for col in range(SIZE):
            mark = board[index_of(row, col)]
            if not mark:
                continue
            for d_row, d_col in DIRECTIONS:
                line = [index_of(row, col)]
                r, c = row + d_row, col + d_col
                while (
                    0 <= r < SIZE
                    and 0 <= c < SIZE
                    and board[index_of(r, c)] == mark
                    and len(line) < WIN_LENGTH
                ):
                    line.append(index_of(r, c))
                    r += d_row
                    c += d_col
                if len(line) == WIN_LENGTH:
                    return tuple(line)
    return ()


def winner(board: list[str]) -> str:
    line = winning_line(board)
    return board[line[0]] if line else ""


def is_full(board: list[str]) -> bool:
    return all(cell for cell in board)


def is_over(board: list[str]) -> bool:
    return bool(winner(board)) or is_full(board)


def free_cells(board: list[str]) -> list[int]:
    return [i for i, cell in enumerate(board) if not cell]


def _line_score(board: list[str], index: int, mark: str) -> int:
    """Heuristic value of playing ``mark`` at ``index``.

    Counts the longest runs it would create in each direction, plus whether the
    ends are open. Good enough to block obvious threats without minimax.
    """
    row, col = divmod(index, SIZE)
    total = 0
    for d_row, d_col in DIRECTIONS:
        run = 1
        open_ends = 0
        for sign in (1, -1):
            r, c = row + d_row * sign, col + d_col * sign
            while 0 <= r < SIZE and 0 <= c < SIZE and board[index_of(r, c)] == mark:
                run += 1
                r += d_row * sign
                c += d_col * sign
            if 0 <= r < SIZE and 0 <= c < SIZE and not board[index_of(r, c)]:
                open_ends += 1
        if run >= WIN_LENGTH:
            total += 10 ** 6
        elif run == 4 and open_ends:
            total += 10 ** 4 * (2 if open_ends == 2 else 1)
        elif run == 3 and open_ends:
            total += 10 ** 2 * (2 if open_ends == 2 else 1)
        elif run == 2 and open_ends == 2:
            total += 10
        else:
            total += run
    return total


def _candidate_cells(board: list[str]) -> list[int]:
    """Only consider cells near existing stones; 225 empty cells is wasteful."""
    if all(not cell for cell in board):
        return [index_of(SIZE // 2, SIZE // 2)]
    near: set[int] = set()
    for index, cell in enumerate(board):
        if not cell:
            continue
        row, col = divmod(index, SIZE)
        for d_row in (-2, -1, 0, 1, 2):
            for d_col in (-2, -1, 0, 1, 2):
                r, c = row + d_row, col + d_col
                if 0 <= r < SIZE and 0 <= c < SIZE and not board[index_of(r, c)]:
                    near.add(index_of(r, c))
    return sorted(near) or free_cells(board)


def ai_move(
    board: list[str],
    mark: str = WHITE,
    rng: random.Random | None = None,
    level: str = LEVEL_NORMAL,
) -> int:
    """Greedy: maximise own threat plus the opponent threat it denies."""
    rng = rng or random
    free = free_cells(board)
    if not free:
        return -1
    slip = LEVEL_SLIP.get(level, 0.0)
    candidates = _candidate_cells(board)
    if slip and rng.random() < slip:
        return rng.choice(candidates)
    opponent = BLACK if mark == WHITE else WHITE
    best_score, best = -1, candidates[0]
    for index in candidates:
        score = _line_score(board, index, mark) * 2 + _line_score(board, index, opponent)
        if score > best_score:
            best_score, best = score, index
    return best


def new_state(mode: str, host_openid: str, level: str = LEVEL_NORMAL) -> dict[str, Any]:
    players = {BLACK: host_openid} if mode == MODE_AI else {BLACK: host_openid, WHITE: ""}
    return {
        "game": "gomoku",
        "mode": mode,
        "board": new_board(),
        "turn": BLACK,
        "players": players,
        "labels": {},
        "level": level if level in AI_LEVELS else LEVEL_NORMAL,
        "last_move": -1,
        "session_id": "",
    }


def apply_move(state: dict[str, Any], index: int, actor_openid: str) -> str:
    """Apply a move; returns "" on success or a human-readable refusal."""
    board = state["board"]
    if not 0 <= index < SIZE * SIZE:
        return "坐标超出棋盘范围"
    if board[index]:
        return f"{format_coordinate(index)} 已经有子了"
    if is_over(board):
        return "对局已结束"

    turn = state["turn"]
    if state["mode"] == MODE_PVP:
        holder = state["players"].get(turn)
        if not holder:
            if actor_openid in state["players"].values():
                return "等待对手落子"
            state["players"][turn] = actor_openid
        elif holder != actor_openid:
            return "现在不是你的回合"
    else:
        if turn != BLACK:
            return "请等待 AI 落子"
        if state["players"].get(BLACK) != actor_openid:
            return "这不是你的对局"

    board[index] = turn
    state["last_move"] = index
    state["turn"] = WHITE if turn == BLACK else BLACK
    return ""


def maybe_ai_move(state: dict[str, Any], rng: random.Random | None = None) -> int:
    if state["mode"] != MODE_AI or state["turn"] != WHITE or is_over(state["board"]):
        return -1
    index = ai_move(state["board"], WHITE, rng, state.get("level", LEVEL_NORMAL))
    if index < 0:
        return -1
    state["board"][index] = WHITE
    state["last_move"] = index
    state["turn"] = BLACK
    return index


def player_label(state: dict[str, Any], mark: str) -> str:
    labels = state.get("labels") or {}
    label = str(labels.get(mark) or "").strip()
    if label:
        return label
    openid = str((state.get("players") or {}).get(mark) or "")
    return f"玩家…{openid[-4:]}" if openid else ""


def status_text(state: dict[str, Any]) -> str:
    board = state["board"]
    win = winner(board)
    if win:
        if state["mode"] == MODE_AI:
            return "🎉 你赢了！" if win == BLACK else "🤖 AI 赢了。"
        return f"🎉 {MARKS[win]} {player_label(state, win)} 获胜！".replace("  ", " ")
    if is_full(board):
        return "🤝 平局。"
    turn = state["turn"]
    if state["mode"] == MODE_AI:
        return "轮到你落子 ⚫" if turn == BLACK else "AI 思考中…"
    return f"轮到 {MARKS[turn]} {player_label(state, turn)} 落子".replace("  ", " ").strip()


def move_hint(state: dict[str, Any]) -> str:
    """Tell players how to move, since there are no board buttons."""
    if is_over(state["board"]):
        return ""
    last = state.get("last_move", -1)
    tail = f"（上一手 {format_coordinate(last)}）" if last >= 0 else ""
    return f"引用本图回复坐标落子，例如 H8{tail}"
