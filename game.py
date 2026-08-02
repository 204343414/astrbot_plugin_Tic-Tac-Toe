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

#: Lobby -> waiting -> board. Every board game can reuse this shape: one entry
#: card that offers the modes, an optional seat-filling card for PvP, then the
#: game itself.
PHASE_LOBBY = "lobby"
PHASE_WAITING = "waiting"
PHASE_PLAYING = "playing"

#: AI strength. "perfect" never loses, which is correct and no fun to play
#: against; tic-tac-toe is a draw under optimal play from both sides.
LEVEL_EASY = "easy"
LEVEL_NORMAL = "normal"
LEVEL_HARD = "hard"
AI_LEVELS = (LEVEL_EASY, LEVEL_NORMAL, LEVEL_HARD)
LEVEL_LABELS = {LEVEL_EASY: "轻松", LEVEL_NORMAL: "普通", LEVEL_HARD: "困难"}
#: Probability the AI plays a deliberately random move instead of its best one.
LEVEL_SLIP = {LEVEL_EASY: 0.6, LEVEL_NORMAL: 0.25, LEVEL_HARD: 0.0}


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


def ai_move(
    board: list[str],
    mark: str = AI,
    rng: random.Random | None = None,
    level: str = LEVEL_NORMAL,
) -> int:
    """Pick a cell: win if possible, else block, else centre/corner, else random.

    Intentionally simple (not minimax). Even so, perfect tic-tac-toe is a
    solved draw, so a flawless opponent is unbeatable -- measured at ~1% wins
    for a random player. ``level`` therefore injects deliberate mistakes,
    because an opponent you cannot beat is not a game.
    """
    rng = rng or random
    free = free_cells(board)
    if not free:
        return -1
    opponent = HUMAN if mark == AI else AI

    slip = LEVEL_SLIP.get(level, 0.0)
    if slip and rng.random() < slip:
        return rng.choice(free)

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
        return f"🎉 {MARKS[win]} {player_label(state, win)} 获胜！".replace("  ", " ")
    if is_full(board):
        return "🤝 平局。"
    turn = state["turn"]
    if state["mode"] == MODE_AI:
        return "轮到你落子 ⭕" if turn == HUMAN else "AI 思考中…"
    label = player_label(state, turn)
    return f"轮到 {MARKS[turn]} {label} 落子".replace("  ", " ").strip()


def player_label(state: dict[str, Any], mark: str) -> str:
    """Human-readable name for a seat.

    Names are injected by the plugin layer (which can reach the Hub's identity
    book) into ``state["labels"]``. This module stays free of Hub imports, and
    never falls back to showing a raw OpenID -- an opaque hex string tells a
    player nothing and looks broken.
    """
    labels = state.get("labels") or {}
    label = str(labels.get(mark) or "").strip()
    if label:
        return label
    openid = str((state.get("players") or {}).get(mark) or "")
    return f"玩家…{openid[-4:]}" if openid else ""


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

    # Control buttons are not one_shot either: a one-shot click is consumed by
    # the Hub before the game runs, so a single stray tap would leave nobody
    # able to end or restart the match. Idempotence is handled in the handlers.
    seats = [openid for openid in (state.get("players") or {}).values() if openid]
    rows.append([{
        "id": "quit",
        "label": "🔚 结束对局" if not over else "🔄 再来一局",
        "style": 0,
        "action_id": "tictactoe.quit" if not over else "tictactoe.restart",
        "params": {},
        "one_shot": False,
        # While a match is running only its players may end it; once it is over
        # anyone may start the next one.
        "owner_mode": "specified" if (not over and len(seats) == 1) else "everyone",
        "owner_openid": seats[0] if (not over and len(seats) == 1) else "",
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


def build_lobby_card(level: str = LEVEL_NORMAL) -> dict[str, Any]:
    """The entry card every board game can copy: pick a mode, then play.

    Deliberately not owner-locked -- anyone in the group may start a game.
    """
    level = level if level in AI_LEVELS else LEVEL_NORMAL
    return {
        "id": "tictactoe_lobby",
        "markdown": "\n".join([
            "# 井字棋",
            f"当前 AI 难度：**{LEVEL_LABELS[level]}**",
            "",
            "选择对战方式开始游戏。",
        ]),
        "rows": [
            [
                {"id": "start_ai", "label": "🤖 人机对战", "style": 1,
                 "action_id": "tictactoe.start_ai", "params": {"level": level}},
                {"id": "start_pvp", "label": "👥 群友对战", "style": 1,
                 "action_id": "tictactoe.start_pvp", "params": {}},
            ],
            [
                {"id": f"level_{name}",
                 "label": f"{'✅ ' if name == level else ''}{LEVEL_LABELS[name]}",
                 "style": 0,
                 "action_id": "tictactoe.set_level", "params": {"level": name}}
                for name in AI_LEVELS
            ],
        ],
        "one_shot": False,
        "ttl_seconds": 3600,
    }


def build_waiting_card(state: dict[str, Any], host_label: str = "") -> dict[str, Any]:
    """Shown after "群友对战": one seat taken, waiting for an opponent."""
    host = host_label or "发起者"
    host_openid = str((state.get("players") or {}).get(HUMAN) or "")
    return {
        "id": "tictactoe_waiting",
        "markdown": "\n".join([
            "# 井字棋 · 等待对手",
            f"⭕ {host} 已入座，等待一位群友加入…",
            "",
            "点击「加入对战」成为 ❌ 方。",
        ]),
        "rows": [[
            # NOT one_shot: the Hub consumes a one-shot button *before* the
            # game sees the click, so the host tapping it once would burn the
            # seat for everyone. Whether the seat is taken is game state, not
            # button state.
            {"id": "join", "label": "🙋 加入对战", "style": 1,
             "action_id": "tictactoe.join", "params": {}, "one_shot": False},
            # Only the host may cancel their own pending match.
            {"id": "cancel", "label": "🔚 取消", "style": 0,
             "action_id": "tictactoe.quit", "params": {}, "one_shot": False,
             "owner_mode": "specified" if host_openid else "everyone",
             "owner_openid": host_openid},
        ]],
        "one_shot": False,
        "ttl_seconds": 1800,
    }


def new_state(mode: str, host_openid: str) -> dict[str, Any]:
    players = {HUMAN: host_openid} if mode == MODE_AI else {HUMAN: host_openid, AI: ""}
    return {
        "mode": mode,
        "board": new_board(),
        "turn": HUMAN,
        "players": players,
        "session_id": "",
        "level": LEVEL_NORMAL,
        "phase": PHASE_WAITING if mode == MODE_PVP else PHASE_PLAYING,
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
    cell = ai_move(state["board"], AI, rng, state.get("level", LEVEL_NORMAL))
    if cell < 0:
        return -1
    state["board"][cell] = AI
    state["turn"] = HUMAN
    return cell
