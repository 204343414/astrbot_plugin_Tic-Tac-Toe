"""国际象棋：规则、走法解析、AI。纯 Python，不依赖 Pillow 或 AstrBot。

为什么单独一个模块
------------------
斗兽棋只有 4 类走法，井字棋没有走法；国际象棋的规则量比它们加起来还大，而且
**错了很难被发现**——一个漏掉的"过路兵"或"王车易位要求经过的格子不能被打"
会让整盘棋悄悄变成另一种游戏，玩家只会觉得"这棋怪怪的"。

所以这里的正确性用 perft 钉住：从标准开局数到第 3 层的合法走法数必须是
8902，从 Kiwipete 局面数到第 3 层必须是 97862。这两个数字是国际象棋引擎
圈公认的标尺，任何一条规则写错都会让它们对不上。

坐标约定
--------
和五子棋一样用"字母 + 数字"，但用的是国际象棋自己的记法：

    a8 = 0   b8 = 1   …  h8 = 7
    a1 = 56  b1 = 57  …  h1 = 63

即下标 0 在左上角（黑方底线），排列与渲染出来的图完全一致——玩家看着图打
``e2e4``，我们按下标找格子，中间不做第二次换算。

输入格式
--------
``e2e4``、``e2-e4``、``E2 E4`` 都认；升变可以写成 ``e7e8q``、``e7e8=Q``
或 ``e7e8后``。不带升变字母时**默认升后**——这是绝大多数人的本意，而拒绝
一步合法的棋只为了逼人补一个字母，是很烦人的。
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any

from . import lobby

# --- 常量 -------------------------------------------------------------------

SIZE = 8
FILES = "abcdefgh"
RANKS = "87654321"          # 下标 0 = 第 8 横线
WHITE = "w"
BLACK = "b"
COLORS = (WHITE, BLACK)

PIECE_KINDS = "KQRBNP"
#: 中文名，用于卡片正文与"上一手"。车马象的对应关系按中文棋界的习惯：
#: R=车（城堡/rook），N=马（骑士/knight），B=象（主教/bishop）。
NAMES = {"K": "王", "Q": "后", "R": "车", "B": "象", "N": "马", "P": "兵"}
COLOR_NAMES = {WHITE: "白方", BLACK: "黑方"}
MARKS = {WHITE: "⚪", BLACK: "⚫"}

MODE_AI = "ai"
MODE_PVP = "pvp"
PHASE_WAITING = "waiting"
PHASE_PLAYING = "playing"

SPEC = lobby.CHESS

#: 升变菜单：字母按 QQ 卡片按钮/文本输入的习惯排，后是默认。
PROMOTIONS = ("Q", "R", "B", "N")
PROMO_ALIASES = {"q": "Q", "r": "R", "b": "B", "n": "N",
                 "后": "Q", "车": "R", "象": "B", "马": "N"}


# --- 格子与坐标 -------------------------------------------------------------

def index_of(row: int, col: int) -> int:
    return row * SIZE + col


def row_of(index: int) -> int:
    return index // SIZE


def col_of(index: int) -> int:
    return index % SIZE


def square_name(index: int) -> str:
    if not 0 <= index < 64:
        return "??"
    return f"{FILES[col_of(index)]}{RANKS[row_of(index)]}"


def parse_square(text: str) -> int:
    """``"e4"`` → 下标；不合法返回 -1。大小写都收。"""
    text = str(text or "").strip().lower()
    if len(text) != 2:
        return -1
    if text[0] not in FILES or text[1] not in "12345678":
        return -1
    col = FILES.index(text[0])
    row = RANKS.index(text[1])
    return index_of(row, col)


def color_of(piece: str) -> str:
    return piece[:1] if piece else ""


def kind_of(piece: str) -> str:
    return piece[1:] if piece else ""


def enemy(color: str) -> str:
    return BLACK if color == WHITE else WHITE


# --- 走法 -------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Move:
    """一步棋。``castle`` 与 ``ep`` 是规则里的两个特例，必须显式标出来。

    没有它们的话，"车从 h1 走到 f1"和"王车易位"在数据上无法区分——前者不
    能动王，后者必须动。
    """

    frm: int
    to: int
    promo: str = ""        # "Q"/"R"/"B"/"N" 或 ""
    castle: str = ""       # "K"（王翼）/"Q"（后翼）或 ""
    ep: bool = False       # 吃过路兵

    def uci(self) -> str:
        text = f"{square_name(self.frm)}{square_name(self.to)}"
        return text + (self.promo.lower() if self.promo else "")

    def __str__(self) -> str:
        return self.uci()


# 各兵种的位移，按 (drow, dcol)。车/象/后由这些方向滑行任意格。
_KNIGHT_DELTAS = ((-2, -1), (-2, 1), (-1, -2), (-1, 2),
                  (1, -2), (1, 2), (2, -1), (2, 1))
_KING_DELTAS = ((-1, -1), (-1, 0), (-1, 1), (0, -1),
                (0, 1), (1, -1), (1, 0), (1, 1))
_ROOK_DIRS = ((-1, 0), (1, 0), (0, -1), (0, 1))
_BISHOP_DIRS = ((-1, -1), (-1, 1), (1, -1), (1, 1))


def new_board() -> list[str]:
    """标准开局。下标 0 = a8，所以黑方在数组前半段。"""
    board = [""] * 64
    back = ("R", "N", "B", "Q", "K", "B", "N", "R")
    for col, kind in enumerate(back):
        board[index_of(0, col)] = f"{BLACK}{kind}"
        board[index_of(7, col)] = f"{WHITE}{kind}"
    for col in range(SIZE):
        board[index_of(1, col)] = f"{BLACK}P"
        board[index_of(6, col)] = f"{WHITE}P"
    return board


def new_castling() -> dict[str, bool]:
    return {"wK": True, "wQ": True, "bK": True, "bQ": True}


def _on_board(row: int, col: int) -> bool:
    return 0 <= row < SIZE and 0 <= col < SIZE


# --- 攻击与将军判断 ---------------------------------------------------------

def _pawn_attacks(board: list[str], target: int, by: str) -> bool:
    """``by`` 方的兵是否攻击 ``target``。

    兵的攻击方向和走子方向相反：白兵往上（行号变小），黑兵往下。
    """
    row, col = row_of(target), col_of(target)
    step = 1 if by == WHITE else -1        # 白兵在 target 的下方一格
    source_row = row + step
    for dcol in (-1, 1):
        if _on_board(source_row, col + dcol):
            piece = board[index_of(source_row, col + dcol)]
            if piece == f"{by}P":
                return True
    return False


def _attacked(board: list[str], target: int, by: str) -> bool:
    """``by`` 方是否攻击 ``target`` 格。

    这是 `王不能走进将军`、`易位经过的格子不能被打`、以及 `将军/将死` 的共同
    基础，所以它必须把兵的特殊方向也算对——用走子方向去判兵会漏掉斜吃。
    """
    row, col = row_of(target), col_of(target)
    # 兵
    if _pawn_attacks(board, target, by):
        return True
    # 马
    for drow, dcol in _KNIGHT_DELTAS:
        r, c = row + drow, col + dcol
        if _on_board(r, c) and board[index_of(r, c)] == f"{by}N":
            return True
    # 王
    for drow, dcol in _KING_DELTAS:
        r, c = row + drow, col + dcol
        if _on_board(r, c) and board[index_of(r, c)] == f"{by}K":
            return True
    # 车/后 直线
    for drow, dcol in _ROOK_DIRS:
        r, c = row + drow, col + dcol
        while _on_board(r, c):
            piece = board[index_of(r, c)]
            if piece:
                if color_of(piece) == by and kind_of(piece) in ("R", "Q"):
                    return True
                break
            r += drow
            c += dcol
    # 象/后 斜线
    for drow, dcol in _BISHOP_DIRS:
        r, c = row + drow, col + dcol
        while _on_board(r, c):
            piece = board[index_of(r, c)]
            if piece:
                if color_of(piece) == by and kind_of(piece) in ("B", "Q"):
                    return True
                break
            r += drow
            c += dcol
    return False


def king_square(board: list[str], color: str) -> int:
    target = f"{color}K"
    try:
        return board.index(target)
    except ValueError:
        return -1


def in_check(board: list[str], color: str) -> bool:
    square = king_square(board, color)
    if square < 0:
        return False
    return _attacked(board, square, enemy(color))


# --- 走法生成 ---------------------------------------------------------------

def _pseudo_moves(board: list[str], color: str,
                  castling: dict[str, bool], ep_square: int) -> list[Move]:
    moves: list[Move] = []
    forward = -1 if color == WHITE else 1        # 白方向上（行号减小）

    for frm, piece in enumerate(board):
        if not piece or color_of(piece) != color:
            continue
        kind = kind_of(piece)
        row, col = row_of(frm), col_of(frm)

        if kind == "P":
            # 单步
            r = row + forward
            if _on_board(r, col) and not board[index_of(r, col)]:
                to = index_of(r, col)
                moves.extend(_pawn_moves(frm, to))
                # 双步：只从起始横线出发，且中间那格也要空
                start_row = 6 if color == WHITE else 1
                if row == start_row:
                    r2 = row + 2 * forward
                    if not board[index_of(r2, col)]:
                        moves.append(Move(frm, index_of(r2, col)))
            # 斜吃
            for dcol in (-1, 1):
                r, c = row + forward, col + dcol
                if not _on_board(r, c):
                    continue
                to = index_of(r, c)
                victim = board[to]
                if victim and color_of(victim) != color:
                    moves.extend(_pawn_moves(frm, to))
                elif to == ep_square and not victim:
                    moves.append(Move(frm, to, ep=True))

        elif kind == "N":
            for drow, dcol in _KNIGHT_DELTAS:
                r, c = row + drow, col + dcol
                if not _on_board(r, c):
                    continue
                to = index_of(r, c)
                if not board[to] or color_of(board[to]) != color:
                    moves.append(Move(frm, to))

        elif kind == "K":
            for drow, dcol in _KING_DELTAS:
                r, c = row + drow, col + dcol
                if not _on_board(r, c):
                    continue
                to = index_of(r, c)
                if not board[to] or color_of(board[to]) != color:
                    moves.append(Move(frm, to))
            moves.extend(_castle_moves(board, color, castling))

        else:
            dirs = _ROOK_DIRS if kind == "R" else (
                _BISHOP_DIRS if kind == "B" else _ROOK_DIRS + _BISHOP_DIRS)
            for drow, dcol in dirs:
                r, c = row + drow, col + dcol
                while _on_board(r, c):
                    to = index_of(r, c)
                    victim = board[to]
                    if not victim:
                        moves.append(Move(frm, to))
                    else:
                        if color_of(victim) != color:
                            moves.append(Move(frm, to))
                        break
                    r += drow
                    c += dcol
    return moves


def _pawn_moves(frm: int, to: int) -> list[Move]:
    """到达最后一横线时必须升变，所以一个走法会展开成四个。"""
    if row_of(to) in (0, 7):
        return [Move(frm, to, promo=kind) for kind in PROMOTIONS]
    return [Move(frm, to)]


def _castle_moves(board: list[str], color: str,
                  castling: dict[str, bool]) -> list[Move]:
    """易位的三个条件：权利还在、中间都是空格、经过的格子不被打。"""
    home_row = 7 if color == WHITE else 0
    king_from = index_of(home_row, 4)
    if board[king_from] != f"{color}K":
        return []
    moves: list[Move] = []
    if castling.get(f"{color}K"):
        f, g = index_of(home_row, 5), index_of(home_row, 6)
        rook = index_of(home_row, 7)
        if (not board[f] and not board[g]
                and board[rook] == f"{color}R"
                and not _attacked(board, king_from, enemy(color))
                and not _attacked(board, f, enemy(color))
                and not _attacked(board, g, enemy(color))):
            moves.append(Move(king_from, g, castle="K"))
    if castling.get(f"{color}Q"):
        d, c, b = (index_of(home_row, 3), index_of(home_row, 2),
                   index_of(home_row, 1))
        rook = index_of(home_row, 0)
        if (not board[d] and not board[c] and not board[b]
                and board[rook] == f"{color}R"
                and not _attacked(board, king_from, enemy(color))
                and not _attacked(board, d, enemy(color))
                and not _attacked(board, c, enemy(color))):
            moves.append(Move(king_from, c, castle="Q"))
    return moves


# --- 落子（引擎内部用，不碰插件状态） ---------------------------------------

def _apply(board: list[str], move: Move,
           castling: dict[str, bool]) -> tuple[list[str], dict[str, bool]]:
    """返回落子后的新棋盘与新易位权利。

    每次都复制棋盘：AI 要在同一个局面上试上百步棋，就地修改再回滚比复制更
    容易写错，而 64 个字符串的复制在 Python 里非常便宜。
    """
    new_board_ = list(board)
    piece = new_board_[move.frm]
    color = color_of(piece)
    kind = kind_of(piece)

    new_board_[move.to] = piece
    new_board_[move.frm] = ""
    if move.ep:
        # 被吃的兵不在落点上，而在它"刚走过两格"的那个格子上。
        victim_row = row_of(move.to) + (1 if color == WHITE else -1)
        new_board_[index_of(victim_row, col_of(move.to))] = ""
    if move.promo:
        new_board_[move.to] = f"{color}{move.promo}"
    if move.castle:
        home_row = 7 if color == WHITE else 0
        if move.castle == "K":
            new_board_[index_of(home_row, 5)] = new_board_[index_of(home_row, 7)]
            new_board_[index_of(home_row, 7)] = ""
        else:
            new_board_[index_of(home_row, 3)] = new_board_[index_of(home_row, 0)]
            new_board_[index_of(home_row, 0)] = ""

    rights = dict(castling)
    if kind == "K":
        rights[f"{color}K"] = False
        rights[f"{color}Q"] = False
    # 凡是"某个角上的格子被清空"，那一侧的易位权利就没了，
    # 不管是谁的车：自己把车挪走、或者对方把车吃掉。
    _drop_rook_rights(rights, move.frm)
    _drop_rook_rights(rights, move.to)
    return new_board_, rights


def _drop_rook_rights(rights: dict[str, bool], square: int) -> None:
    """车离开或被吃掉的那个角，对应的一侧易位权利作废。

    按"格子"判而不是按"哪一方的车"判，是因为这个函数要被两边共用：白车走开
    清白方的权，黑车被吃清黑方的权。之前只看走子方，导致"白车吃黑车"之后
    黑方仍然保留易位权利。
    """
    corners = {
        index_of(7, 0): "wQ", index_of(7, 7): "wK",
        index_of(0, 0): "bQ", index_of(0, 7): "bK",
    }
    key = corners.get(square)
    if key:
        rights[key] = False


def legal_moves(board: list[str], color: str, castling: dict[str, bool],
                ep_square: int) -> list[Move]:
    """伪合法走法去掉"走完自己被将军"的那些。"""
    result: list[Move] = []
    for move in _pseudo_moves(board, color, castling, ep_square):
        after, _ = _apply(board, move, castling)
        if not in_check(after, color):
            result.append(move)
    return result


def _ep_square(board: list[str], move: Move, piece: str) -> int:
    if kind_of(piece) == "P" and abs(row_of(move.to) - row_of(move.frm)) == 2:
        # 落点与起点中间那一格，就是下一手可以吃过路兵的位置。
        return index_of((row_of(move.to) + row_of(move.frm)) // 2, col_of(move.frm))
    return -1


# --- 插件用的局面对象 -------------------------------------------------------

def new_state(mode: str, host_openid: str, level: str = "normal") -> dict[str, Any]:
    players = ({WHITE: host_openid} if mode == MODE_AI
               else {WHITE: host_openid, BLACK: ""})
    return {
        "game": "chess",
        "mode": mode,
        "board": new_board(),
        "turn": WHITE,
        "castling": new_castling(),
        "ep": -1,
        "halfmove": 0,
        "fullmove": 1,
        "history": [],
        "players": players,
        "labels": {},
        "level": lobby.normalize_level(level),
        "last_move": None,
        "session_id": "",
        "phase": PHASE_WAITING if mode == MODE_PVP else PHASE_PLAYING,
    }


def position_key(state: dict[str, Any]) -> str:
    """用于"三次重复局面"和棋：棋盘 + 走子方 + 易位权利 + 过路兵格。"""
    return "|".join([
        "".join(piece or "--" for piece in state["board"]),
        state["turn"],
        "".join(k for k, v in sorted(state["castling"].items()) if v),
        str(state.get("ep", -1)),
    ])


def legal_for_state(state: dict[str, Any]) -> list[Move]:
    return legal_moves(state["board"], state["turn"],
                       state["castling"], int(state.get("ep", -1)))


def find_move(state: dict[str, Any], frm: int, to: int,
              promo: str = "") -> Move | None:
    """在合法走法里找出对应的那一步。

    直接生成再匹配，而不是"先试走再校验"：合法走法表已经排除了自我将军，
    在这里再写一遍判断只会多一处可能不一致的实现。
    """
    promo = promo.upper()
    for move in legal_for_state(state):
        if move.frm == frm and move.to == to:
            if move.promo:
                if not promo:
                    return move if move.promo == "Q" else None
                if move.promo == promo:
                    return move
            elif not promo:
                return move
    return None


def resolve_text(state: dict[str, Any], text: object) -> Move | None:
    """把 ``e2e4`` 这类文本落到具体的一步合规走法上。"""
    parsed = parse_move(text)
    if parsed is None:
        return None
    frm, to, promo = parsed
    return find_move(state, frm, to, promo)


def parse_move(text: object) -> tuple[int, int, str] | None:
    """纯语法解析，不看局面。返回 ``(起点, 终点, 升变字母)``。

    允许的分隔：``e2e4`` / ``e2-e4`` / ``e2 e4`` / ``e2→e4``。
    升变可写 ``e7e8q`` / ``e7e8=Q`` / ``e7e8后``，省略则视为升后。
    """
    raw = str(text or "").strip().lower()
    if not raw:
        return None
    for sep in ("→", "->", " ", "-", "=", "，", ",", ":"):
        raw = raw.replace(sep, "")
    promo = ""
    if len(raw) > 4 and raw[-1] in PROMO_ALIASES:
        promo = PROMO_ALIASES[raw[-1]]
        raw = raw[:-1]
    if len(raw) != 4:
        return None
    frm = parse_square(raw[:2])
    to = parse_square(raw[2:])
    if frm < 0 or to < 0:
        return None
    return frm, to, promo


def apply_move(state: dict[str, Any], move: Move, actor_openid: str) -> str:
    """落子并切换走子方。返回 ``""`` 表示成功，否则是要给玩家看的原因。"""
    if state.get("phase") == PHASE_WAITING:
        return "还在等对手加入，点「加入对战」入座"
    if is_over(state):
        return "对局已结束"
    color = state["turn"]
    if state["mode"] == MODE_PVP:
        holder = state["players"].get(color)
        if not holder:
            if actor_openid in state["players"].values():
                return "等待对方走棋"
            state["players"][color] = actor_openid
        elif holder != actor_openid:
            return "现在不是你的回合"
    else:
        if color != WHITE:
            return "请等待 AI 走棋"
        if state["players"].get(WHITE) != actor_openid:
            return "这不是你的对局"

    _commit(state, move)
    return ""


def _commit(state: dict[str, Any], move: Move) -> None:
    """真正落子。走子方/权限校验在 :func:`apply_move`，AI 走同一段落子代码。

    两处各写一份"落子后要更新什么"是最典型的 bug 来源：AI 那条路会悄悄漏掉
    过路兵格或易位权利，而这种错只有在你跟 AI 下到中局才看得出来。
    """
    color = state["turn"]
    piece = state["board"][move.frm]
    captured = state["board"][move.to]
    state["last_move"] = {
        "frm": move.frm, "to": move.to, "piece": piece,
        "captured": captured, "promo": move.promo,
        "castle": move.castle, "ep": move.ep,
    }
    state["ep"] = _ep_square(state["board"], move, piece)
    board, rights = _apply(state["board"], move, state["castling"])
    state["board"] = board
    state["castling"] = rights
    reset = kind_of(piece) == "P" or bool(captured)
    state["halfmove"] = 0 if reset else int(state.get("halfmove", 0)) + 1
    state["turn"] = enemy(color)
    if state["turn"] == WHITE:
        state["fullmove"] = int(state.get("fullmove", 1)) + 1
    # 记在**切换走子方之后**：position_key 里含走子方，先记后切会让同一个
    # 局面被记成两个不同的键，三次重复判定就永远不成立。
    state["history"] = list(state.get("history") or []) + [position_key(state)]


# --- 结束判定 ---------------------------------------------------------------

def _insufficient_material(board: list[str]) -> bool:
    """只剩王、或王 + 单马/单象，谁也赢不了。"""
    pieces = [p for p in board if p]
    if len(pieces) <= 1:
        return True
    if len(pieces) == 3:
        # 王 + 王 + 单马/单象：谁也杀不了谁。之前只判了"两子"的情形，
        # 于是王马对王会被算成"还在下"，一直下到五十回合才和棋。
        kinds = sorted(kind_of(p) for p in pieces)
        return kinds in (["B", "K", "K"], ["K", "K", "N"])
    if len(pieces) == 3:
        kinds = sorted(kind_of(p) for p in pieces)
        if kinds == ["B", "B", "K"]:
            # 同色格的王象相斗和棋；异色仍有可能杀，但极罕见，这里不细分。
            colors = [color_of(p) for p in pieces if kind_of(p) == "B"]
            if len(set(colors)) == 2:
                return True
    return False


def _threefold(state: dict[str, Any]) -> bool:
    key = position_key(state)
    history = list(state.get("history") or [])
    return history.count(key) >= 2      # 本次 + 之前两次


def result(state: dict[str, Any]) -> str:
    """``""`` 表示未结束，否则是 ``"checkmate:c"``/``"stalemate"``/… """
    moves = legal_for_state(state)
    color = state["turn"]
    if not moves:
        return f"checkmate:{enemy(color)}" if in_check(state["board"], color) \
            else "stalemate"
    if _insufficient_material(state["board"]):
        return "material"
    if int(state.get("halfmove", 0)) >= 100:
        return "fifty"
    if _threefold(state):
        return "threefold"
    return ""


def is_over(state: dict[str, Any]) -> bool:
    return bool(result(state))


def status_text(state: dict[str, Any]) -> str:
    outcome = result(state)
    if outcome.startswith("checkmate:"):
        winner = outcome.split(":", 1)[1]
        color = COLOR_NAMES[winner]
        if state["mode"] == MODE_AI:
            return "🎉 你将死了 AI！" if winner == WHITE else "🤖 AI 将死了你。"
        return f"🎉 {MARKS[winner]} {color} {player_label(state, winner)} 将死对手！"
    if outcome == "stalemate":
        return "🤝 逼和——无子可动，和棋。"
    if outcome == "material":
        return "🤝 双方兵力不足，和棋。"
    if outcome == "fifty":
        return "🤝 五十回合无吃子无兵动，和棋。"
    if outcome == "threefold":
        return "🤝 同一局面出现三次，和棋。"
    color = state["turn"]
    check = "，被将军！" if in_check(state["board"], color) else ""
    if state["mode"] == MODE_AI:
        return ("轮到你走棋 ⚪" if color == WHITE else "AI 思考中…") + check
    return f"轮到 {MARKS[color]} {player_label(state, color)} 走棋{check}"


def player_label(state: dict[str, Any], color: str) -> str:
    labels = state.get("labels") or {}
    label = str(labels.get(color) or "").strip()
    if label:
        return label
    openid = str((state.get("players") or {}).get(color) or "")
    return f"玩家…{openid[-4:]}" if openid else ""


def describe_move(state: dict[str, Any], move_info: dict[str, Any] | None) -> str:
    """把上一手写成人话：``马 b1→c3``、``兵 e7→e8 升后``。"""
    if not move_info:
        return ""
    piece = str(move_info.get("piece") or "")
    if move_info.get("castle"):
        side = "王翼" if move_info["castle"] == "K" else "后翼"
        return f"{COLOR_NAMES.get(color_of(piece), '')} {side}易位"
    text = (f"{NAMES.get(kind_of(piece), '')} "
            f"{square_name(move_info['frm'])}→{square_name(move_info['to'])}")
    if move_info.get("captured"):
        text += f" 吃{NAMES.get(kind_of(move_info['captured']), '子')}"
    if move_info.get("promo"):
        text += f" 升{NAMES[move_info['promo']]}"
    if move_info.get("ep"):
        text += "（吃过路兵）"
    return text


def move_hint(state: dict[str, Any]) -> str:
    if is_over(state):
        return ""
    last = describe_move(state, state.get("last_move"))
    tail = f"（上一手 {last}）" if last else ""
    return f"引用本图回复走法，例如 e2e4；升变写 e7e8q{tail}"


# --- 卡片 -------------------------------------------------------------------

def build_lobby_card(level: str = "normal") -> dict[str, Any]:
    return lobby.build_lobby_card(SPEC, level)


def build_waiting_card(state: dict[str, Any], host_label: str = "") -> dict[str, Any]:
    host_openid = str((state.get("players") or {}).get(WHITE) or "")
    return lobby.build_waiting_card(SPEC, host_label, host_openid)


#: 卡片里声明给 QQ 的图片尺寸。渲染器保证实际像素与此一致——声明和实际不符
#: 会导致图片被拉伸甚至完全不渲染，这个坑在斗兽棋上踩过。
CARD_IMAGE_WIDTH = 720
CARD_IMAGE_HEIGHT = 756


def build_board_card(state: dict[str, Any], image_url: str = "") -> dict[str, Any]:
    """棋盘卡片：一张图 + 说明文字。没有按钮——64 格装不下。

    走法靠"引用这张图 + 打字"完成：``e2e4``。这也意味着卡片本身不用每次换
    session，玩家引用的是最新那张即可。
    """
    lines = [f"**{status_text(state)}**"]
    if image_url:
        lines.append(
            f"![棋盘 #{CARD_IMAGE_WIDTH}px #{CARD_IMAGE_HEIGHT}px]({image_url})")
    last = describe_move(state, state.get("last_move"))
    if last:
        lines.append(f"上一手：{last}")
    if not is_over(state):
        lines.append(move_hint(state))
    return {
        "id": "chess_board",
        "markdown": "\n".join(lines),
        "rows": [],
        "one_shot": False,
        "ttl_seconds": 3600,
    }


# --- AI ---------------------------------------------------------------------
#
# 为什么不用"看一步吃子"了事：国际象棋的战术平均在 3~4 层之后才显现，只看
# 一步的 AI 会主动送子，玩家两分钟就摸清并失去兴趣。这里用 negamax + α-β 剪枝，
# 加上吃子排序，深度 4 就能挡住绝大多数"随手送"。
#
# 纯 Python，不做置换表：置换表要维护 Zobrist 哈希，代码量大不说，一旦有 bug
# 就是"AI 偶尔走一步莫名其妙的棋"，很难复现。慢一点，但每步棋都能解释。

#: 子力价值（厘兵）。王不参与估值——它没了就是结束，不是分少一点。
VALUES = {"P": 100, "N": 320, "B": 330, "R": 500, "Q": 900, "K": 0}

#: 位置表（白方视角，下标 0 = a8，与棋盘同序）。这些是引擎圈通用的"简化评估"
#: 表：兵鼓励往前顶、马往中心走、王在中局躲角落。抄现成的表比自己拍脑袋强，
#: 因为它已经避免了一堆反直觉的怪癖（比如王特别爱往边线跑）。
_PST = {
    "P": (
         0,  0,  0,  0,  0,  0,  0,  0,
        50, 50, 50, 50, 50, 50, 50, 50,
        10, 10, 20, 30, 30, 20, 10, 10,
         5,  5, 10, 25, 25, 10,  5,  5,
         0,  0,  0, 20, 20,  0,  0,  0,
         5, -5,-10,  0,  0,-10, -5,  5,
         5, 10, 10,-20,-20, 10, 10,  5,
         0,  0,  0,  0,  0,  0,  0,  0),
    "N": (
       -50,-40,-30,-30,-30,-30,-40,-50,
       -40,-20,  0,  0,  0,  0,-20,-40,
       -30,  0, 10, 15, 15, 10,  0,-30,
       -30,  5, 15, 20, 20, 15,  5,-30,
       -30,  0, 15, 20, 20, 15,  0,-30,
       -30,  5, 10, 15, 15, 10,  5,-30,
       -40,-20,  0,  5,  5,  0,-20,-40,
       -50,-40,-30,-30,-30,-30,-40,-50),
    "B": (
       -20,-10,-10,-10,-10,-10,-10,-20,
       -10,  0,  0,  0,  0,  0,  0,-10,
       -10,  0,  5, 10, 10,  5,  0,-10,
       -10,  5,  5, 10, 10,  5,  5,-10,
       -10,  0, 10, 10, 10, 10,  0,-10,
       -10, 10, 10, 10, 10, 10, 10,-10,
       -10,  5,  0,  0,  0,  0,  5,-10,
       -20,-10,-10,-10,-10,-10,-10,-20),
    "R": (
         0,  0,  0,  0,  0,  0,  0,  0,
         5, 10, 10, 10, 10, 10, 10,  5,
        -5,  0,  0,  0,  0,  0,  0, -5,
        -5,  0,  0,  0,  0,  0,  0, -5,
        -5,  0,  0,  0,  0,  0,  0, -5,
        -5,  0,  0,  0,  0,  0,  0, -5,
        -5,  0,  0,  0,  0,  0,  0, -5,
         0,  0,  0,  5,  5,  0,  0,  0),
    "Q": (
       -20,-10,-10, -5, -5,-10,-10,-20,
       -10,  0,  0,  0,  0,  0,  0,-10,
       -10,  0,  5,  5,  5,  5,  0,-10,
        -5,  0,  5,  5,  5,  5,  0, -5,
         0,  0,  5,  5,  5,  5,  0, -5,
       -10,  5,  5,  5,  5,  5,  0,-10,
       -10,  0,  5,  0,  0,  0,  0,-10,
       -20,-10,-10, -5, -5,-10,-10,-20),
    "K": (
       -30,-40,-40,-50,-50,-40,-40,-30,
       -30,-40,-40,-50,-50,-40,-40,-30,
       -30,-40,-40,-50,-50,-40,-40,-30,
       -30,-40,-40,-50,-50,-40,-40,-30,
       -20,-30,-30,-40,-40,-30,-30,-20,
       -10,-20,-20,-20,-20,-20,-20,-10,
        20, 20,  0,  0,  0,  0, 20, 20,
        20, 30, 10,  0,  0, 10, 30, 20),
}

MATE_SCORE = 100_000
#: 难度 → (搜索深度, 时间上限秒)。困难档用迭代加深，所以这里的深度是上限。
LEVEL_DEPTH = {"easy": 1, "normal": 3, "hard": 4}
LEVEL_TIME = {"easy": 0.4, "normal": 1.6, "hard": 4.0}


def _mirror(index: int) -> int:
    """白方视角表翻到黑方：上下镜像 63-index。左右对称的表这样做是成立的。"""
    return 63 - index


def evaluate(board: list[str]) -> int:
    """白方视角的静态估值（正数=白优）。"""
    score = 0
    for index, piece in enumerate(board):
        if not piece:
            continue
        kind = kind_of(piece)
        value = VALUES[kind]
        table = _PST.get(kind)
        if table:
            value += table[index] if color_of(piece) == WHITE else table[_mirror(index)]
        score += value if color_of(piece) == WHITE else -value
    return score


def _mvv_lva(board: list[str], move: Move) -> int:
    """吃子排序：先用小子吃大子。排序好坏直接决定 α-β 剪掉多少分支。"""
    victim = board[move.to]
    if not victim:
        return 0
    attacker = board[move.frm]
    return 10 * VALUES[kind_of(victim)] - VALUES[kind_of(attacker)]


def _ordered(board: list[str], moves: list[Move]) -> list[Move]:
    return sorted(moves, key=lambda m: (_mvv_lva(board, m), bool(m.promo)),
                  reverse=True)


class _Search:
    """一次搜索的状态。包成对象是为了让截止时间只有一个来源。"""

    def __init__(self, deadline: float) -> None:
        self.deadline = deadline
        self.nodes = 0
        self.aborted = False

    def time_up(self) -> bool:
        if self.aborted:
            return True
        self.nodes += 1
        if self.nodes % 512 == 0 and time.monotonic() >= self.deadline:
            self.aborted = True
        return self.aborted


def _quiesce(state: dict[str, Any], alpha: int, beta: int, color: str,
             search: _Search, depth: int = 0) -> int:
    """只展开吃子的"静态搜索"。

    跳过它，评估函数会在一次互吃的半路上停手：AI 以为自己白吃了一个车，
    其实下一步就被反吃。这是"看一步"型 AI 最典型的失误。
    """
    board = state["board"]
    sign = 1 if color == WHITE else -1
    stand = sign * evaluate(board)
    if depth >= 3 or search.time_up():
        return stand
    if stand >= beta:
        return beta
    alpha = max(alpha, stand)
    moves = [m for m in legal_moves(board, color, state["castling"], state.get("ep", -1))
             if board[m.to] or m.ep or m.promo]
    for move in _ordered(board, moves):
        nxt = _child(state, move)
        score = -_quiesce(nxt, -beta, -alpha, enemy(color), search, depth + 1)
        if score >= beta:
            return beta
        alpha = max(alpha, score)
    return alpha


def _child(state: dict[str, Any], move: Move) -> dict[str, Any]:
    """生成子局面。搜索只在"棋盘 / 走子方 / 易位权利 / 过路兵格"上用得到。"""
    board, rights = _apply(state["board"], move, state["castling"])
    return {
        "board": board,
        "turn": enemy(state["turn"]),
        "castling": rights,
        "ep": _ep_square(state["board"], move, state["board"][move.frm]),
    }


def _negamax(state: dict[str, Any], depth: int, alpha: int, beta: int,
             color: str, search: _Search) -> int:
    board = state["board"]
    moves = legal_moves(board, color, state["castling"], state.get("ep", -1))
    if not moves:
        # 将死比"无子可动"糟得多，分值要能拉开；depth 用来让"更快的将死"更高分。
        if in_check(board, color):
            return -MATE_SCORE + (10 - depth)
        return 0
    if depth <= 0:
        return _quiesce(state, alpha, beta, color, search)
    if search.time_up():
        return _quiesce(state, alpha, beta, color, search)

    best = -MATE_SCORE * 2
    for move in _ordered(board, moves):
        score = -_negamax(_child(state, move), depth - 1, -beta, -alpha,
                          enemy(color), search)
        if score > best:
            best = score
        alpha = max(alpha, score)
        if alpha >= beta:
            break
    return best


def choose_move(state: dict[str, Any], rng: random.Random | None = None) -> Move | None:
    """按当前难度挑一步棋。

    真正下棋的是 vendored 的 Sunfish（见 ``chess_ai``）；这里只是入口，并在
    引擎不可用时退回到本文件里那套自带的搜索——它虽然弱，但胜在只有几十行
    依赖，永远不会 import 失败。

    为什么用外部引擎：国际象棋的棋力几乎全在搜索细节（静态展开、剪枝窗口、
    置换表）上，自己写一个"能跑"的很容易，写一个"不送子"的很难。Sunfish 用
    几百行把这几点都做对了，直接用比复刻强。
    """
    rng = rng or random.Random()
    try:
        from . import chess_ai

        return chess_ai.choose_move(state, rng)
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "[Chess] Sunfish 不可用，退回内置搜索", exc_info=True)
        return _choose_builtin(state, rng)


def _choose_builtin(state: dict[str, Any], rng: random.Random | None = None) -> Move | None:
    """自带的 negamax + α-β，作为 Sunfish 缺席时的兜底。"""
    rng = rng or random.Random()
    level = str(state.get("level") or "normal")
    max_depth = LEVEL_DEPTH.get(level, 3)
    budget = LEVEL_TIME.get(level, 1.6)
    color = state["turn"]
    moves = legal_for_state(state)
    if not moves:
        return None
    if len(moves) == 1:
        return moves[0]

    # 轻松档：故意留出"看不全"的空间，否则新手第一盘就被将死。
    if level == "easy":
        if rng.random() < 0.35:
            return rng.choice(moves)
        search = _Search(time.monotonic() + budget)
        scored = []
        for move in _ordered(state["board"], moves):
            score = -_negamax(_child(state, move), 1, -MATE_SCORE * 2,
                              MATE_SCORE * 2, enemy(color), search)
            scored.append((score, move))
            if search.aborted:
                break
        scored.sort(key=lambda item: item[0], reverse=True)
        top = scored[0][0]
        # 同分的随手挑一个，别每次都走同一开局。
        candidates = [m for s, m in scored if s >= top - 15]
        return rng.choice(candidates)

    search = _Search(time.monotonic() + budget)
    best_move = moves[0]
    for depth in range(1, max_depth + 1):
        alpha, beta = -MATE_SCORE * 2, MATE_SCORE * 2
        local_best, local_score = None, -MATE_SCORE * 3
        for move in _ordered(state["board"], moves):
            score = -_negamax(_child(state, move), depth - 1, -beta, -alpha,
                              enemy(color), search)
            if search.aborted:
                break
            if score > local_score:
                local_score, local_best = score, move
            alpha = max(alpha, score)
        if search.aborted:
            break
        if local_best is not None:
            best_move = local_best
        if abs(local_score) > MATE_SCORE // 2:
            break           # 已经看到杀棋，再往深算没有意义
    return best_move


def maybe_ai_move(state: dict[str, Any], rng: random.Random | None = None) -> Move | None:
    """AI 该走就走一步，返回走法（``None`` 表示不该它走）。"""
    if state.get("mode") != MODE_AI or is_over(state):
        return None
    if state["turn"] != BLACK:
        return None
    move = choose_move(state, rng)
    if move is None:
        return None
    _commit(state, move)
    return move


# --- FEN（给测试和残局用） --------------------------------------------------

FEN_START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def from_fen(fen: str) -> dict[str, Any]:
    """读一个 FEN 成局面对象。

    主要是为了让规则测试能用**公认的标尺局面**（Kiwipete 之类），而不是只能
    从开局手搭。搭出来的局面覆盖不到易位、过路兵、升变这些最容易写错的地方。
    """
    parts = str(fen).strip().split()
    if len(parts) < 4:
        raise ValueError(f"FEN 字段不足: {fen!r}")
    rows, turn, castle, ep = parts[0], parts[1], parts[2], parts[3]
    halfmove = int(parts[4]) if len(parts) > 4 else 0
    fullmove = int(parts[5]) if len(parts) > 5 else 1

    board = [""] * 64
    for row, chunk in enumerate(rows.split("/")):
        col = 0
        for char in chunk:
            if char.isdigit():
                col += int(char)
                continue
            board[index_of(row, col)] = ("w" if char.isupper() else "b") + char.upper()
            col += 1
    rights = {"wK": "K" in castle, "wQ": "Q" in castle,
              "bK": "k" in castle, "bQ": "q" in castle}
    return {
        "game": "chess",
        "mode": MODE_AI,
        "board": board,
        "turn": WHITE if turn == "w" else BLACK,
        "castling": rights,
        "ep": parse_square(ep) if ep not in ("-", "") else -1,
        "halfmove": halfmove,
        "fullmove": fullmove,
        "history": [],
        "players": {WHITE: "U1"},
        "labels": {},
        "level": "normal",
        "last_move": None,
        "session_id": "",
        "phase": PHASE_PLAYING,
    }


def to_fen(state: dict[str, Any]) -> str:
    rows = []
    for row in range(SIZE):
        chunk, empty = "", 0
        for col in range(SIZE):
            piece = state["board"][index_of(row, col)]
            if not piece:
                empty += 1
                continue
            if empty:
                chunk += str(empty)
                empty = 0
            chunk += kind_of(piece) if color_of(piece) == WHITE else kind_of(piece).lower()
        rows.append(chunk + (str(empty) if empty else ""))
    castle = "".join(
        key for key in ("wK", "wQ", "bK", "bQ") if state["castling"].get(key)
    ).replace("wK", "K").replace("wQ", "Q").replace("bK", "k").replace("bQ", "q")
    ep = square_name(int(state.get("ep", -1))) if int(state.get("ep", -1)) >= 0 else "-"
    return " ".join(["/".join(rows), state["turn"], castle or "-", ep,
                     str(state.get("halfmove", 0)), str(state.get("fullmove", 1))])


def perft(state: dict[str, Any], depth: int) -> int:
    """数叶子节点。规则改动的回归标尺，数字对不上就是规则错了。"""
    if depth <= 0:
        return 1
    moves = legal_for_state(state)
    if depth == 1:
        return len(moves)
    return sum(perft(_child(state, move), depth - 1) for move in moves)
