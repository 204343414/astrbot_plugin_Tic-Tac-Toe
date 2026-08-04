"""Jungle / Animal Chess (斗兽棋) rules.

Board orientation
-----------------
The classic board is 7 columns x 9 rows with the dens at top and bottom. The
artwork for this plugin is **landscape**: 9 columns x 7 rows, red on the left
and blue on the right. That is the same board rotated 90 degrees, so every
published rule still applies under::

    landscape(row, col) == portrait(row=col, col=row)

Rows are numbered 1..7 top to bottom (R1..R7) and columns 1..9 left to right,
matching the labels the players see. Internally everything is 0-based.

Move syntax
-----------
A 15x15-style coordinate grid would be unreadable here, and 63 squares cannot
be buttons (QQ allows 25). Players quote the board image and name a piece plus
a direction -- ``鼠下``, ``虎上`` -- which is unambiguous because each side owns
exactly one of every animal.

Pure logic: no AstrBot, no Hub, no Pillow. Rendering lives in
``animalchess_render.py`` so the rules stay testable on their own.
"""
from __future__ import annotations

import random
from typing import Any

from . import lobby

ROWS = 7
COLS = 9

RED = "R"
BLUE = "B"

MODE_AI = "ai"
MODE_PVP = "pvp"

PHASE_WAITING = "waiting"
PHASE_PLAYING = "playing"

# --- pieces -----------------------------------------------------------------

RAT = "rat"
CAT = "cat"
DOG = "dog"
WOLF = "wolf"
LEOPARD = "leopard"
TIGER = "tiger"
LION = "lion"
ELEPHANT = "elephant"

#: Fighting power, strongest last. Rat beats elephant as a special case.
RANK = {
    RAT: 1, CAT: 2, DOG: 3, WOLF: 4,
    LEOPARD: 5, TIGER: 6, LION: 7, ELEPHANT: 8,
}
ANIMALS = tuple(sorted(RANK, key=RANK.get))

NAMES = {
    RAT: "鼠", CAT: "猫", DOG: "狗", WOLF: "狼",
    LEOPARD: "豹", TIGER: "虎", LION: "狮", ELEPHANT: "象",
}
#: Accepted aliases when parsing a move. Keys must stay single-meaning.
ALIASES = {
    "鼠": RAT, "老鼠": RAT, "耗子": RAT,
    "猫": CAT, "小猫": CAT,
    "狗": DOG, "犬": DOG,
    "狼": WOLF,
    "豹": LEOPARD, "豹子": LEOPARD,
    "虎": TIGER, "老虎": TIGER,
    "狮": LION, "狮子": LION,
    "象": ELEPHANT, "大象": ELEPHANT,
}

SIDE_NAMES = {RED: "红方", BLUE: "蓝方"}

# --- terrain ----------------------------------------------------------------

#: Two 2x3 rivers, rows 2-3 and 5-6, columns 4-6 (1-based labels).
WATER = frozenset(
    (row, col)
    for row in (1, 2, 4, 5)      # 0-based R2,R3,R5,R6
    for col in (3, 4, 5)         # 0-based C4,C5,C6
)

DENS = {RED: (3, 0), BLUE: (3, 8)}            # R4C1 / R4C9
TRAPS = {
    RED: frozenset({(2, 0), (4, 0), (3, 1)}),   # R3C1, R5C1, R4C2
    BLUE: frozenset({(2, 8), (4, 8), (3, 7)}),  # R3C9, R5C9, R4C8
}

#: Opening layout, mirrored between the two sides.
START = {
    RED: {
        LION: (0, 0), TIGER: (6, 0),
        DOG: (1, 1), CAT: (5, 1),
        RAT: (0, 2), LEOPARD: (2, 2), WOLF: (4, 2), ELEPHANT: (6, 2),
    },
    BLUE: {
        TIGER: (0, 8), LION: (6, 8),
        CAT: (1, 7), DOG: (5, 7),
        ELEPHANT: (0, 6), WOLF: (2, 6), LEOPARD: (4, 6), RAT: (6, 6),
    },
}

# --- directions -------------------------------------------------------------

#: R1 is the top row, so 上 decreases the row index.
DIRECTIONS = {
    "上": (-1, 0), "下": (1, 0), "左": (0, -1), "右": (0, 1),
}
DIRECTION_ALIASES = {
    "上": "上", "下": "下", "左": "左", "右": "右",
    "北": "上", "南": "下", "西": "左", "东": "右",
    "u": "上", "d": "下", "l": "左", "r": "右",
    "w": "上", "s": "下", "a": "左", "e": "右",
}

SPEC = lobby.ANIMALCHESS

from .lobby import LEVEL_EASY, LEVEL_HARD, LEVEL_NORMAL  # noqa: E402

#: Search depth per difficulty. Measured on the opening position, which is the
#: worst case: depth 5 costs ~0.7s, depth 6 jumps to ~4.3s and depth 7 to ~16s.
#: Anything past 5 is too slow to feel like a chat game, so 困难 stops there.
#: Verified head-to-head: 困难 beats 普通 15:1 over 16 games with alternating
#: colours, so the ladder is real and not just a bigger number.
LEVEL_DEPTH = {LEVEL_EASY: 1, LEVEL_NORMAL: 3, LEVEL_HARD: 5}
#: Chance the AI throws a move away, which is the only thing that makes a
#: search opponent beatable at all.
#:
#: These are *not* calibrated against a human. Self-play against a mostly
#: random opponent showed the depth advantage dominating so completely that
#: slip barely moved the win rate (0% at 0.12, still 8% at 0.35), which means
#: the proxy is too weak to calibrate against -- tuning harder would have been
#: fitting noise. Adjust once real players report how 普通 actually feels.
LEVEL_SLIP = {LEVEL_EASY: 0.45, LEVEL_NORMAL: 0.22, LEVEL_HARD: 0.0}


def other(side: str) -> str:
    return BLUE if side == RED else RED


def in_bounds(row: int, col: int) -> bool:
    return 0 <= row < ROWS and 0 <= col < COLS


def is_water(row: int, col: int) -> bool:
    return (row, col) in WATER


def label_of(row: int, col: int) -> str:
    """Human-facing square name, e.g. ``R4C1``."""
    return f"R{row + 1}C{col + 1}"


# --- state ------------------------------------------------------------------

def new_state(mode: str, host_openid: str, level: str = LEVEL_NORMAL) -> dict[str, Any]:
    """Fresh game. The host plays 红方, which moves first."""
    pieces: dict[tuple[int, int], tuple[str, str]] = {}
    for side, layout in START.items():
        for animal, square in layout.items():
            pieces[square] = (side, animal)
    players = {RED: host_openid} if mode == MODE_AI else {RED: host_openid, BLUE: ""}
    return {
        "game": "animalchess",
        "mode": mode,
        # (row, col) -> (side, animal). Kept as a dict because the board is
        # sparse: 16 pieces on 63 squares.
        "pieces": pieces,
        "turn": RED,
        "players": players,
        "labels": {},
        "level": level if level in LEVEL_DEPTH else LEVEL_NORMAL,
        "last_move": None,
        "winner": "",
        "win_reason": "",
        "session_id": "",
        "phase": PHASE_WAITING if mode == MODE_PVP else PHASE_PLAYING,
    }


def piece_at(state: dict[str, Any], row: int, col: int) -> tuple[str, str] | None:
    return state["pieces"].get((row, col))


def find_piece(state: dict[str, Any], side: str, animal: str) -> tuple[int, int] | None:
    for square, (owner, kind) in state["pieces"].items():
        if owner == side and kind == animal:
            return square
    return None


# --- capture rules ----------------------------------------------------------

def in_enemy_trap(state: dict[str, Any], square: tuple[int, int], side: str) -> bool:
    """True when ``side``'s piece stands in a trap belonging to the opponent."""
    return square in TRAPS[other(side)]


def can_capture(state: dict[str, Any], attacker: tuple[int, int],
                target: tuple[int, int]) -> bool:
    """Whether the piece on ``attacker`` may take the piece on ``target``."""
    source = piece_at(state, *attacker)
    victim = piece_at(state, *target)
    if source is None or victim is None:
        return False
    side, animal = source
    victim_side, victim_animal = victim
    if victim_side == side:
        return False

    attacker_wet = is_water(*attacker)
    victim_wet = is_water(*target)
    # A rat in the river is untouchable from land, and cannot strike the shore
    # itself -- including the elephant. Two rats in the water may still trade.
    if attacker_wet != victim_wet:
        return False

    # A piece standing in the enemy's trap has no fighting power at all, so
    # even a rat may take an elephant there.
    if in_enemy_trap(state, target, victim_side):
        return True

    if animal == RAT and victim_animal == ELEPHANT:
        return True
    if animal == ELEPHANT and victim_animal == RAT:
        return False
    return RANK[animal] >= RANK[victim_animal]


# --- movement ---------------------------------------------------------------

def _jump_destination(state: dict[str, Any], square: tuple[int, int],
                      delta: tuple[int, int]) -> tuple[int, int] | None:
    """Where a lion/tiger lands when leaping the river, or None.

    The leap continues in a straight line across every water square and stops
    on the first dry land. A rat of *either* colour sitting anywhere along the
    path blocks it -- that is the rat's whole defensive purpose.
    """
    row, col = square
    d_row, d_col = delta
    row, col = row + d_row, col + d_col
    if not in_bounds(row, col) or not is_water(row, col):
        return None                      # not actually facing the river
    while in_bounds(row, col) and is_water(row, col):
        blocker = piece_at(state, row, col)
        if blocker is not None:          # only a rat can be here, and it blocks
            return None
        row, col = row + d_row, col + d_col
    if not in_bounds(row, col):
        return None
    return (row, col)


def legal_moves(state: dict[str, Any], side: str) -> dict[str, list[tuple[str, tuple[int, int]]]]:
    """All legal moves for ``side`` as ``animal -> [(direction, target)]``."""
    result: dict[str, list[tuple[str, tuple[int, int]]]] = {}
    for square, (owner, animal) in list(state["pieces"].items()):
        if owner != side:
            continue
        options: list[tuple[str, tuple[int, int]]] = []
        for name, delta in DIRECTIONS.items():
            target = _resolve_target(state, square, animal, side, delta)
            if target is not None:
                options.append((name, target))
        if options:
            result[animal] = options
    return result


def _resolve_target(state: dict[str, Any], square: tuple[int, int], animal: str,
                    side: str, delta: tuple[int, int]) -> tuple[int, int] | None:
    """The square this piece reaches moving in ``delta``, or None if illegal."""
    row, col = square[0] + delta[0], square[1] + delta[1]

    if animal in (LION, TIGER) and in_bounds(row, col) and is_water(row, col):
        landing = _jump_destination(state, square, delta)
        if landing is None:
            return None
        row, col = landing
    elif not in_bounds(row, col):
        return None
    elif is_water(row, col) and animal != RAT:
        return None

    if (row, col) == DENS[side]:
        return None                      # never step into your own den
    occupant = piece_at(state, row, col)
    if occupant is not None:
        if occupant[0] == side:
            return None
        if not can_capture(state, square, (row, col)):
            return None
    return (row, col)


def parse_move(text: object) -> tuple[str, str] | None:
    """Parse ``鼠下`` / ``老虎 上`` into ``(animal, direction)``.

    Returns None when the text is not a move at all, which is what keeps
    ordinary chat from being mistaken for play.
    """
    raw = str(text or "").strip().replace(" ", "").replace("　", "")
    if not raw or len(raw) > 6:
        return None
    for suffix, direction in DIRECTION_ALIASES.items():
        if not raw.lower().endswith(suffix.lower()):
            continue
        head = raw[: len(raw) - len(suffix)].strip()
        animal = ALIASES.get(head)
        if animal is not None:
            return animal, direction
    return None


def apply_move(state: dict[str, Any], animal: str, direction: str,
               actor_openid: str) -> str:
    """Play a move; returns "" on success or a human-readable refusal."""
    if state.get("winner"):
        return "对局已结束"
    if state.get("phase") == PHASE_WAITING:
        return "还在等对手加入，点「加入对战」入座"

    side = state["turn"]
    holder = str((state.get("players") or {}).get(side) or "")
    if state["mode"] == MODE_AI:
        if side != RED:
            return "请等待 AI 落子"
        if holder and holder != actor_openid:
            return "这不是你的对局"
    elif holder and holder != actor_openid:
        return "现在不是你的回合"

    square = find_piece(state, side, animal)
    if square is None:
        return f"你的{NAMES[animal]}已经被吃掉了"
    delta = DIRECTIONS.get(direction)
    if delta is None:
        return "方向只能是上下左右"

    target = _resolve_target(state, square, animal, side, delta)
    if target is None:
        return _explain_refusal(state, square, animal, side, delta)

    captured = state["pieces"].pop(target, None)
    del state["pieces"][square]
    state["pieces"][target] = (side, animal)
    state["last_move"] = {
        "side": side, "animal": animal, "direction": direction,
        "from": square, "to": target,
        "captured": captured[1] if captured else "",
    }
    _update_outcome(state, side, target)
    if not state["winner"]:
        state["turn"] = other(side)
    return ""


def _explain_refusal(state: dict[str, Any], square: tuple[int, int], animal: str,
                     side: str, delta: tuple[int, int]) -> str:
    """Say *why* a move is illegal. A bare '不能这样走' teaches nobody."""
    row, col = square[0] + delta[0], square[1] + delta[1]
    if animal in (LION, TIGER) and in_bounds(row, col) and is_water(row, col):
        return "河里有鼠挡路，跳不过去" if _has_rat_in_path(state, square, delta) \
            else "这个方向跳不过河"
    if not in_bounds(row, col):
        return "已经到棋盘边缘了"
    if is_water(row, col) and animal != RAT:
        return f"{NAMES[animal]}不能下水"
    if (row, col) == DENS[side]:
        return "不能进入自己的兽穴"
    occupant = piece_at(state, row, col)
    if occupant is None:
        return "这一步走不了"
    if occupant[0] == side:
        return f"那里是自己的{NAMES[occupant[1]]}"
    if is_water(row, col) != is_water(*square):
        return "水陆之间不能互吃"
    return f"{NAMES[animal]}吃不掉{NAMES[occupant[1]]}"


def _has_rat_in_path(state: dict[str, Any], square: tuple[int, int],
                     delta: tuple[int, int]) -> bool:
    row, col = square[0] + delta[0], square[1] + delta[1]
    while in_bounds(row, col) and is_water(row, col):
        if piece_at(state, row, col) is not None:
            return True
        row, col = row + delta[0], col + delta[1]
    return False


def _update_outcome(state: dict[str, Any], side: str, target: tuple[int, int]) -> None:
    if target == DENS[other(side)]:
        state["winner"] = side
        state["win_reason"] = "den"
        return
    enemy = other(side)
    if not any(owner == enemy for owner, _ in state["pieces"].values()):
        state["winner"] = side
        state["win_reason"] = "wiped"
        return
    if not legal_moves(state, enemy):
        state["winner"] = side
        state["win_reason"] = "stuck"


def is_over(state: dict[str, Any]) -> bool:
    return bool(state.get("winner"))


# --- AI ---------------------------------------------------------------------

#: Material value. Deliberately *not* the capture rank: a rat that can take an
#: elephant and block both jumps is worth far more than "1".
VALUE = {
    RAT: 260, CAT: 90, DOG: 130, WOLF: 170,
    LEOPARD: 220, TIGER: 320, LION: 360, ELEPHANT: 400,
}
WIN_SCORE = 1_000_000


def _den_distance(square: tuple[int, int], den: tuple[int, int]) -> int:
    return abs(square[0] - den[0]) + abs(square[1] - den[1])


def evaluate(state: dict[str, Any], side: str) -> int:
    """Score the position from ``side``'s point of view.

    Material dominates, then proximity to the enemy den -- which is what the
    game is actually about, and without it the AI shuffles pieces forever.
    """
    if state.get("winner"):
        return WIN_SCORE if state["winner"] == side else -WIN_SCORE

    score = 0
    for square, (owner, animal) in state["pieces"].items():
        sign = 1 if owner == side else -1
        score += sign * VALUE[animal]
        # Closing on the enemy den is worth something to every piece.
        distance = _den_distance(square, DENS[other(owner)])
        score += sign * (14 - distance) * 6
        # Sitting in the enemy's trap is suicide unless it wins immediately.
        if in_enemy_trap(state, square, owner):
            score -= sign * VALUE[animal] // 2
    return score


def _apply_for_search(state: dict[str, Any], side: str, animal: str,
                      target: tuple[int, int]) -> dict[str, Any]:
    """A shallow copy with the move played. Cheap enough at these depths."""
    pieces = dict(state["pieces"])
    square = None
    for position, (owner, kind) in pieces.items():
        if owner == side and kind == animal:
            square = position
            break
    if square is None:
        return state
    pieces.pop(target, None)
    del pieces[square]
    pieces[target] = (side, animal)
    child = dict(state)
    child["pieces"] = pieces
    child["winner"] = ""
    child["turn"] = other(side)
    if target == DENS[other(side)]:
        child["winner"] = side
    elif not any(owner == other(side) for owner, _ in pieces.values()):
        child["winner"] = side
    return child


def _ordered_moves(state: dict[str, Any], side: str) -> list[tuple[str, tuple[int, int]]]:
    """Moves with captures first, so alpha-beta prunes far more."""
    moves: list[tuple[int, str, tuple[int, int]]] = []
    for animal, options in legal_moves(state, side).items():
        for _, target in options:
            victim = state["pieces"].get(target)
            priority = VALUE[victim[1]] if victim else 0
            if target == DENS[other(side)]:
                priority = WIN_SCORE
            moves.append((priority, animal, target))
    moves.sort(key=lambda item: -item[0])
    return [(animal, target) for _, animal, target in moves]


def _search(state: dict[str, Any], side: str, depth: int,
            alpha: int, beta: int, maximising: bool) -> int:
    if depth <= 0 or state.get("winner"):
        score = evaluate(state, side)
        # Discount wins by how far away they are, so a mate in one outranks a
        # mate in three. Without this every winning line scores WIN_SCORE, the
        # AI cannot tell them apart, and it will happily shuffle sideways while
        # standing next to the enemy den -- which is exactly what it did.
        if score >= WIN_SCORE:
            return score + depth
        if score <= -WIN_SCORE:
            return score - depth
        return score
    mover = side if maximising else other(side)
    moves = _ordered_moves(state, mover)
    if not moves:
        return -WIN_SCORE if maximising else WIN_SCORE

    if maximising:
        best = -WIN_SCORE * 2
        for animal, target in moves:
            child = _apply_for_search(state, mover, animal, target)
            best = max(best, _search(child, side, depth - 1, alpha, beta, False))
            alpha = max(alpha, best)
            if alpha >= beta:
                break
        return best
    best = WIN_SCORE * 2
    for animal, target in moves:
        child = _apply_for_search(state, mover, animal, target)
        best = min(best, _search(child, side, depth - 1, alpha, beta, True))
        beta = min(beta, best)
        if alpha >= beta:
            break
    return best


def ai_move(state: dict[str, Any], side: str,
            rng: random.Random | None = None,
            level: str = LEVEL_NORMAL) -> tuple[str, str] | None:
    """Pick a move as ``(animal, direction)``, or None when trapped."""
    rng = rng or random
    options = legal_moves(state, side)
    if not options:
        return None
    flat = [(animal, name, target)
            for animal, moves in options.items() for name, target in moves]

    slip = LEVEL_SLIP.get(level, 0.0)
    if slip and rng.random() < slip:
        animal, name, _ = rng.choice(flat)
        return animal, name

    depth = LEVEL_DEPTH.get(level, 3)
    best_score = -WIN_SCORE * 2
    best: list[tuple[str, str]] = []
    for animal, name, target in flat:
        child = _apply_for_search(state, side, animal, target)
        score = _search(child, side, depth - 1, -WIN_SCORE * 2, WIN_SCORE * 2, False)
        if score > best_score:
            best_score, best = score, [(animal, name)]
        elif score == best_score:
            best.append((animal, name))
    return rng.choice(best) if best else None


def maybe_ai_move(state: dict[str, Any], rng: random.Random | None = None) -> str:
    """Let the AI answer if it is its turn. Returns a description or ""."""
    if state["mode"] != MODE_AI or state["turn"] != BLUE or is_over(state):
        return ""
    choice = ai_move(state, BLUE, rng, state.get("level", LEVEL_NORMAL))
    if choice is None:
        state["winner"] = RED
        state["win_reason"] = "stuck"
        return ""
    animal, direction = choice
    refusal = _force_move(state, BLUE, animal, direction)
    return "" if refusal else f"{NAMES[animal]}{direction}"


def _force_move(state: dict[str, Any], side: str, animal: str, direction: str) -> str:
    """apply_move without the ownership checks, for the AI's own turn."""
    square = find_piece(state, side, animal)
    delta = DIRECTIONS.get(direction)
    if square is None or delta is None:
        return "非法"
    target = _resolve_target(state, square, animal, side, delta)
    if target is None:
        return "非法"
    captured = state["pieces"].pop(target, None)
    del state["pieces"][square]
    state["pieces"][target] = (side, animal)
    state["last_move"] = {
        "side": side, "animal": animal, "direction": direction,
        "from": square, "to": target,
        "captured": captured[1] if captured else "",
    }
    _update_outcome(state, side, target)
    if not state["winner"]:
        state["turn"] = other(side)
    return ""


# --- presentation helpers ---------------------------------------------------

def player_label(state: dict[str, Any], side: str) -> str:
    labels = state.get("labels") or {}
    label = str(labels.get(side) or "").strip()
    if label:
        return label
    openid = str((state.get("players") or {}).get(side) or "")
    return f"玩家…{openid[-4:]}" if openid else ""


def status_text(state: dict[str, Any]) -> str:
    winner = state.get("winner")
    if winner:
        reason = {
            "den": "攻入兽穴", "wiped": "吃光对手", "stuck": "对手无棋可走",
        }.get(state.get("win_reason", ""), "")
        if state["mode"] == MODE_AI:
            head = "你赢了！" if winner == RED else "AI 赢了。"
            return f"{head}（{reason}）" if reason else head
        return f"{SIDE_NAMES[winner]} {player_label(state, winner)} 获胜（{reason}）".replace("  ", " ")
    if state.get("phase") == PHASE_WAITING:
        return "等待对手加入…"
    turn = state["turn"]
    if state["mode"] == MODE_AI:
        return "轮到你走棋（红方）" if turn == RED else "AI 思考中…"
    return f"轮到 {SIDE_NAMES[turn]} {player_label(state, turn)} 走棋".replace("  ", " ").strip()


def move_hint(state: dict[str, Any]) -> str:
    """Drawn inside the picture -- there is no caption on the image message."""
    if is_over(state):
        return ""
    last = state.get("last_move")
    tail = ""
    if last:
        eaten = f"吃{NAMES[last['captured']]}" if last.get("captured") else ""
        tail = f"（上一手 {NAMES[last['animal']]}{last['direction']}{eaten}）"
    return f"点卡片上的动物+方向，然后发送{tail}"


def build_lobby_card(level: str = LEVEL_NORMAL) -> dict[str, Any]:
    """This game's entry card, built from the shared five-button layout."""
    return lobby.build_lobby_card(SPEC, level)


def build_waiting_card(state: dict[str, Any], host_label: str = "") -> dict[str, Any]:
    host_openid = str((state.get("players") or {}).get(RED) or "")
    return lobby.build_waiting_card(SPEC, host_label, host_openid)


# --- picture card -----------------------------------------------------------
#
# The board is a picture and the moves are buttons on the same message. That
# only became possible once the Hub could embed a hosted image in a Markdown
# card: QQ refuses rich media and a keyboard in one message, so before this
# the board had to be a separate image message that players quoted by hand.

#: QQ scales a Markdown image to the size declared in the link. The board is
#: 2135x1436, and 720 is the documented maximum width.
CARD_IMAGE_WIDTH = 720
CARD_IMAGE_HEIGHT = round(CARD_IMAGE_WIDTH * 1436 / 2135)   # keep the shape


def movable_animals(state: dict[str, Any], side: str = "") -> list[str]:
    """Which of ``side``'s animals are still on the board, weakest first.

    Only living pieces get a button: a captured animal's button would be a
    guaranteed rejection, and rejections cost a reply the group's quota can
    ill afford. Ordered by rank so the row does not reshuffle as pieces die --
    a button that moves under your finger between turns is worse than one
    that disappears.
    """
    side = side or state["turn"]
    alive = {animal for (owner, animal) in state["pieces"].values() if owner == side}
    return [animal for animal in ANIMALS if animal in alive]


def build_board_card(state: dict[str, Any], image_url: str) -> dict[str, Any]:
    """The board as one card: picture, animals, directions.

    Buttons are **type=2**: tapping appends text to the input box without
    sending, so a move is composed as 「鼠」+「下」and sent deliberately.
    That matters for three separate reasons:

    * type=1 callbacks are rate-limited and laggy, which a turn-based game
      taps constantly;
    * the sent message is a real user message, which refreshes the passive
      reply window -- a game driven purely by callbacks slowly starves;
    * a misclick is fixable before sending, because nothing is sent yet.

    ``reply`` makes the composed message quote this card, which is how the
    move is tied to the position it was played against.
    """
    side = state["turn"]
    animals = movable_animals(state, side)
    rows: list[list[dict[str, Any]]] = []
    # Four per row: QQ allows five, but a fifth squeezes the labels until the
    # text is clipped -- verified in the group, not assumed from the docs.
    for start in range(0, len(animals), 4):
        rows.append([
            {
                "id": f"pick_{animal}",
                "label": NAMES[animal],
                # Tencent appends a space after inserted text, so the message
                # ends up as "鼠 下" -- which parse_move already tolerates.
                "insert_text": NAMES[animal],
                "reply": True,
                "style": 0,
            }
            for animal in animals[start:start + 4]
        ])
    rows.append([
        # ASCII ids: the Hub's CARD_ID_RE only accepts [A-Za-z0-9_.:-], so a
        # Chinese id like "dir_上" is rejected outright. The label and the
        # inserted text stay Chinese -- only the identifier is transliterated.
        {"id": f"dir_{slug}", "label": label, "insert_text": name,
         "reply": True, "style": 1}
        for name, slug, label in (("上", "up", "⬆️ 上"),
                                  ("下", "down", "⬇️ 下"),
                                  ("左", "left", "⬅️ 左"),
                                  ("右", "right", "➡️ 右"))
    ])

    lines = [
        f"**{status_text(state)}**",
        f"![棋盘 #{CARD_IMAGE_WIDTH}px #{CARD_IMAGE_HEIGHT}px]({image_url})",
    ]
    last = state.get("last_move")
    if last:
        eaten = f" 吃{NAMES[last['captured']]}" if last.get("captured") else ""
        lines.append(f"上一手：{NAMES[last['animal']]}{last['direction']}{eaten}")
    if not is_over(state):
        lines.append("先点动物再点方向，然后发送。")
    return {
        "id": "animalchess_board",
        "markdown": "\n".join(lines),
        "rows": rows,
        # Every button is type=2 and never reaches the server, so there is
        # nothing for one_shot to consume; the card is replaced each turn.
        "one_shot": False,
        "ttl_seconds": 3600,
    }
