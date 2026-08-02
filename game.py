"""Pure tic-tac-toe rules and card rendering.

Deliberately free of AstrBot and Hub imports so the rules can be tested on
their own. Everything here is a plain dict, which is also what the Hub's
ephemeral-card API consumes.
"""
from __future__ import annotations

import random
from typing import Any

EMPTY = ""
HUMAN = "O"
AI = "X"

MARKS = {HUMAN: "⭕", AI: "❌", EMPTY: "·"}
CELL_LABELS = ("1", "2", "3", "4", "5", "6", "7", "8", "9")

WIN_LINES = (
    (0, 1, 2), (3, 4, 5), (6, 7, 8),   # rows
    (0, 3, 6), (1, 4, 7), (2, 5, 8),   # columns
    (0, 4, 8), (2, 4, 6),              # diagonals
)

MODE_AI = "ai"
MODE_PVP = "pvp"


def new_board() -> list[str]:
    return [EMPTY] * 9


def winner(board: list[str]) -> str:
    """Return the winning mark, or "" when nobody has won yet."""
    for a, b, c in WIN_LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    return ""


def winning_line(board: list[str]) -> tuple[int, ...]:
    for line in WIN_LINES:
        a, b, c = line
        if board[a] and board[a] == board[b] == board[c]:
            return line
    return ()


def is_full(board: list[str]) -> bool:
    return all(cell for cell in board)


def is_over(board: list[str]) -> bool:
    return bool(winner(board)) or is_full(board)


def free_cells(board: list[str]) -> list[int]:
    return [i for i, cell in enumerate(board) if not cell]


def ai_move(board: list[str], mark: str = AI, rng: random.Random | None = None) -> int:
    """Pick a cell: win if possible, else block, else centre/corner, else random.

    Intentionally simple (not minimax) -- it is a sample opponent, but blocking
    an immediate loss costs three lines and makes it feel far less broken.
    """
    rng = rng or random
    free = free_cells(board)
    if not free:
        return -1
    opponent = HUMAN if mark == AI else AI

    for candidate in (mark, opponent):          # win first, then block
        for cell in free:
            probe = list(board)
            probe[cell] = candidate
            if winner(probe) == candidate:
                return cell

    for cell in (4, 0, 2, 6, 8):
        if cell in free:
            return cell
    return rng.choice(free)


def render_board_text(board: list[str]) -> str:
    highlight = set(winning_line(board))
    rows = []
    for row in range(3):
        cells = []
        for col in range(3):
            index = row * 3 + col
            mark = MARKS[board[index]] if board[index] else MARKS[EMPTY]
            cells.append(f"**{mark}**" if index in highlight else mark)
        rows.append(" ".join(cells))
    return "\n".join(rows)


def status_text(state: dict[str, Any]) -> str:
    board = state["board"]
    win = winner(board)
    if win:
        if state["mode"] == MODE_AI:
            return "🎉 你赢了！" if win == HUMAN else "🤖 AI 赢了。"
        name = state["players"].get(win, "")
        return f"🎉 {MARKS[win]} 获胜！{_short(name)}"
    if is_full(board):
        return "🤝 平局。"
    turn = state["turn"]
    if state["mode"] == MODE_AI:
        return "轮到你落子 ⭕" if turn == HUMAN else "AI 思考中…"
    holder = state["players"].get(turn, "")
    return f"轮到 {MARKS[turn]} 落子 {_short(holder)}".rstrip()


def _short(openid: str) -> str:
    return f"({openid[-6:]})" if openid else ""


def build_card(state: dict[str, Any]) -> dict[str, Any]:
    """Render the current state as a Hub ephemeral card.

    Every live cell is one-shot: a square can only be played once, and the Hub
    enforces that server-side even if two players tap simultaneously.
    """
    board = state["board"]
    over = is_over(board)
    # The grid lives in the buttons, so repeating it as text only adds noise.
    # Keep a single status line; on the final card show the board once, since
    # the buttons are gone by then.
    lines = ["# 井字棋", status_text(state)]
    if over:
        lines.insert(1, render_board_text(board))

    rows: list[list[dict[str, Any]]] = []
    if not over:
        owner = _current_owner(state)
        for row in range(3):
            buttons = []
            for col in range(3):
                index = row * 3 + col
                if board[index]:
                    # Keep the 3x3 grid intact. An occupied square stays as a
                    # button showing its mark; clicking it is answered with an
                    # ACK code only, which QQ shows as a toast to the clicker
                    # and costs no group message at all.
                    buttons.append({
                        "id": f"cell{index}",
                        "label": MARKS[board[index]],
                        "style": 0,
                        "action_id": "tictactoe.occupied",
                        "params": {"cell": index},
                        "one_shot": False,
                        "owner_mode": "everyone",
                        "owner_openid": "",
                        "unsupport_tips": "该位置已被占据",
                    })
                    continue
                buttons.append({
                    "id": f"cell{index}",
                    "label": CELL_LABELS[index],
                    "style": 1,
                    "action_id": "tictactoe.move",
                    "params": {"cell": index},
                    "one_shot": True,
                    "owner_mode": "specified" if owner else "everyone",
                    "owner_openid": owner,
                })
            rows.append(buttons)

    rows.append([{
        "id": "quit",
        "label": "🔚 结束对局" if not over else "🔄 再来一局",
        "style": 0,
        "action_id": "tictactoe.quit" if not over else "tictactoe.restart",
        "params": {},
        "one_shot": True,
    }])

    return {
        "id": "tictactoe_board",
        "markdown": "\n".join(lines),
        "rows": rows,
        # The board itself is not one_shot: a player must be able to look at a
        # stale card without killing the game. Per-cell one_shot is what stops
        # replaying a move.
        "one_shot": False,
        "owner_reject_tip": "现在不是你的回合",
        "ttl_seconds": 3600,
    }


def _current_owner(state: dict[str, Any]) -> str:
    """Whose turn it is, as an OpenID. Empty means anyone may click."""
    if state["mode"] == MODE_AI:
        return str(state["players"].get(HUMAN) or "")
    return str(state["players"].get(state["turn"]) or "")


def new_state(mode: str, host_openid: str) -> dict[str, Any]:
    players = {HUMAN: host_openid} if mode == MODE_AI else {HUMAN: host_openid, AI: ""}
    return {
        "mode": mode,
        "board": new_board(),
        "turn": HUMAN,
        "players": players,
        "session_id": "",
    }


def apply_move(state: dict[str, Any], cell: int, actor_openid: str) -> str:
    """Apply a human move, returning "" on success or a refusal reason.

    The Hub already enforces one-shot and ownership; this is the second line of
    defence so the rules stay correct even if the game is driven another way.
    """
    board = state["board"]
    if cell < 0 or cell >= 9:
        return "位置无效"
    if board[cell]:
        return "该位置已被占据"
    if is_over(board):
        return "对局已结束"

    turn = state["turn"]
    if state["mode"] == MODE_PVP:
        holder = state["players"].get(turn)
        if not holder:
            # Second player joins by making the first move of their mark.
            if actor_openid in state["players"].values():
                return "等待对手落子"
            state["players"][turn] = actor_openid
        elif holder != actor_openid:
            return "现在不是你的回合"
    else:
        if turn != HUMAN:
            return "请等待 AI 落子"
        if state["players"].get(HUMAN) != actor_openid:
            return "这不是你的对局"

    board[cell] = turn
    state["turn"] = AI if turn == HUMAN else HUMAN
    return ""


def autoplay_forced_move(state: dict[str, Any]) -> int:
    """Play the last remaining square automatically.

    With one cell left there is no decision to make, so asking for a tap only
    costs another card. Returns the cell played, or -1.
    """
    board = state["board"]
    if is_over(board):
        return -1
    free = free_cells(board)
    if len(free) != 1:
        return -1
    cell = free[0]
    board[cell] = state["turn"]
    state["turn"] = AI if state["turn"] == HUMAN else HUMAN
    return cell


def maybe_ai_move(state: dict[str, Any], rng: random.Random | None = None) -> int:
    """Let the AI play if it is its turn. Returns the cell, or -1."""
    if state["mode"] != MODE_AI or state["turn"] != AI or is_over(state["board"]):
        return -1
    cell = ai_move(state["board"], AI, rng)
    if cell < 0:
        return -1
    state["board"][cell] = AI
    state["turn"] = HUMAN
    return cell
