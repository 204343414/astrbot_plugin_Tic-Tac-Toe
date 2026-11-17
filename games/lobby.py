"""The entry card shared by every board game in this plugin.

One shape, one code path
------------------------
Each game gets its **own** lobby: ``/井字棋`` opens the tic-tac-toe lobby,
``/五子棋`` opens the gomoku one. Neither is a menu of the other -- a game's
entry card is about that game, and making one of them the hub was exactly the
thing that looked wrong.

The layout is fixed at five buttons because that is what fits and what reads
well:

    [🤖 人机对战] [👥 群友对战]
    [轻松] [普通] [困难]

A game whose two sides play differently (牛角棋: one bull vs. two farmers) also
declares ``sides``. Then the entry card becomes a two-button question —
「选牛」/「选人」— and the five-button card above only appears once a side is
picked, carried in the button ``params`` so no server-side state is needed:

    /牛角棋 → [🐮 选牛] [👨‍👩 选人] → [🤖 人机] [👥 群友] / [难度] / [↩ 返回]

QQ allows at most 5 rows x 5 columns, so this leaves plenty of headroom; the
point of the constraint is consistency, not capacity. A new game only has to
declare a :class:`GameSpec` and implement the four action ids derived from its
key, and its lobby is identical to the others by construction.

Pure data: no AstrBot, no Hub, no Pillow.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

LEVEL_EASY = "easy"
LEVEL_NORMAL = "normal"
LEVEL_HARD = "hard"
AI_LEVELS = (LEVEL_EASY, LEVEL_NORMAL, LEVEL_HARD)
LEVEL_LABELS = {LEVEL_EASY: "轻松", LEVEL_NORMAL: "普通", LEVEL_HARD: "困难"}


@dataclass(frozen=True, slots=True)
class GameSpec:
    """Everything the shared lobby needs to know about a game.

    ``key`` drives the action ids, so a game that declares ``key="gomoku"``
    must register ``gomoku.start_ai``, ``gomoku.start_pvp``,
    ``gomoku.set_level`` and ``gomoku.join``.
    """

    key: str
    title: str
    #: One line telling players how moves are made -- buttons vs. quoting an
    #: image. Shown in the lobby so nobody has to guess after starting.
    how_to: str
    #: Mark shown next to the host on the waiting card (⭕ / ⚫).
    host_mark: str
    #: Mark the joining player takes (❌ / ⚪).
    guest_mark: str
    #: Optional ``((key, label), ...)`` for games whose two sides are not
    #: symmetric. When set, the entry card asks which side the player wants
    #: before offering 人机／群友. ``key`` must match what the game module
    #: understands (see ``hornchess.SIDES``).
    sides: tuple[tuple[str, str], ...] = ()


def action_id(spec: GameSpec, name: str) -> str:
    return f"{spec.key}.{name}"


def normalize_level(level: object) -> str:
    return level if level in AI_LEVELS else LEVEL_NORMAL


def build_side_card(spec: GameSpec, level: str = LEVEL_NORMAL) -> dict[str, Any]:
    """Step one for asymmetric games: 你要哪一方？"""
    level = normalize_level(level)
    return {
        "id": f"{spec.key}_sides",
        "markdown": "\n".join([
            f"# {spec.title} · 选边",
            f"AI 难度：**{LEVEL_LABELS[level]}**",
            "",
            spec.how_to,
            "",
            "**先选你要执哪一方**，再选人机还是群友。",
        ]),
        "rows": [[
            {"id": f"side_{key}", "label": label, "style": 1,
             "action_id": action_id(spec, "pick_side"),
             "params": {"side": key, "level": level}}
            for key, label in spec.sides
        ]],
        "one_shot": False,
        "ttl_seconds": 3600,
    }


def build_lobby_card(spec: GameSpec, level: str = LEVEL_NORMAL,
                     side: str = "") -> dict[str, Any]:
    """The entry card.

    For a game that declares ``sides`` this is two steps: with no ``side`` it
    shows the 选边 question, with a ``side`` it shows the usual five buttons
    (and remembers the side in every button's params).
    """
    level = normalize_level(level)
    if spec.sides and side not in [key for key, _ in spec.sides]:
        return build_side_card(spec, level)

    chosen = ""
    if spec.sides:
        chosen = side
    labels = dict(spec.sides)
    side_line = f"你执：**{labels[chosen]}**　（点「↩ 重新选边」换边）" if chosen else ""
    return {
        "id": f"{spec.key}_lobby",
        "markdown": "\n".join(
            [line for line in [
                f"# {spec.title}",
                f"AI 难度：**{LEVEL_LABELS[level]}**",
                side_line,
                "",
                spec.how_to,
            ] if line != ""]
        ),
        "rows": [
            [
                {"id": "start_ai", "label": "🤖 人机对战", "style": 1,
                 "action_id": action_id(spec, "start_ai"),
                 "params": {"level": level, "side": chosen}},
                {"id": "start_pvp", "label": "👥 群友对战", "style": 1,
                 "action_id": action_id(spec, "start_pvp"),
                 "params": {"side": chosen}},
            ],
            [
                {"id": f"level_{name}",
                 "label": f"{'✅ ' if name == level else ''}{LEVEL_LABELS[name]}",
                 "style": 0,
                 "action_id": action_id(spec, "set_level"),
                 "params": {"level": name, "side": chosen}}
                for name in AI_LEVELS
            ],
        ] + ([[
            {"id": "back", "label": "↩ 重新选边", "style": 0,
             "action_id": action_id(spec, "lobby"), "params": {}},
        ]] if chosen else []),
        # Not one_shot: the difficulty buttons are meant to be pressed more
        # than once, and the Hub burns a one-shot button before the game runs.
        "one_shot": False,
        "ttl_seconds": 3600,
    }


def build_waiting_card(spec: GameSpec, host_label: str = "",
                       host_openid: str = "", host_mark: str = "",
                       guest_mark: str = "") -> dict[str, Any]:
    """Shown after 「群友对战」: one seat taken, waiting for an opponent."""
    host = host_label or "发起者"
    seat, other = host_mark or spec.host_mark, guest_mark or spec.guest_mark
    return {
        "id": f"{spec.key}_waiting",
        "markdown": "\n".join([
            f"# {spec.title} · 等待对手",
            f"{seat} {host} 已入座，等待一位群友加入…",
            "",
            f"点击「加入对战」成为 {other} 方。",
        ]),
        "rows": [[
            # NOT one_shot: the Hub consumes a one-shot click *before* the game
            # sees it, so the host tapping it once would burn the seat for
            # everyone. Whether the seat is taken is game state, not button
            # state, and is checked in the join handler.
            {"id": "join", "label": "🙋 加入对战", "style": 1,
             "action_id": action_id(spec, "join"), "params": {},
             "one_shot": False},
            # Only the host may cancel their own pending match.
            {"id": "cancel", "label": "🔚 取消", "style": 0,
             "action_id": action_id(spec, "quit"), "params": {},
             "one_shot": False,
             "owner_mode": "specified" if host_openid else "everyone",
             "owner_openid": host_openid},
        ]],
        "one_shot": False,
        "ttl_seconds": 1800,
    }


TICTACTOE = GameSpec(
    key="tictactoe",
    title="井字棋",
    how_to="点棋盘按钮落子，三子连线即胜。",
    host_mark="⭕",
    guest_mark="❌",
)

ANIMALCHESS = GameSpec(
    key="animalchess",
    title="斗兽棋",
    how_to="棋盘是一张带按钮的卡片：**先点动物再点方向，然后发送**（如 鼠 下），攻入兽穴即胜。",
    host_mark="🔴",
    guest_mark="🔵",
)

GOMOKU = GameSpec(
    key="gomoku",
    title="五子棋",
    how_to="棋盘是图片：**引用棋盘图回复坐标**（如 H8）落子，五子连线即胜。",
    host_mark="⚫",
    guest_mark="⚪",
)

CHESS = GameSpec(
    key="chess",
    title="国际象棋",
    how_to=("棋盘是图片：**引用棋盘图回复走法**（如 e2e4，升变写 e7e8q），"
            "把对方的王将死即胜。"),
    host_mark="⚪",
    guest_mark="⚫",
)

HORNCHESS = GameSpec(
    key="hornchess",
    title="牛角棋",
    how_to=("**点按钮走棋**：牛（🐮）前后左右都行，赶牛的人（👨‍👩）只能往尖上赶、"
            "不能后退（同一条横杆可左右挪）。把牛逼到牛角尖无路可走就赢；"
            "牛冲到底角、或步数用完还没围住，则牛赢。"),
    # 牛角棋两边不对称（1 头牛 vs 2 个人），所以入口先问"你要执哪一方"。
    host_mark="👨‍👩",
    guest_mark="🐮",
    sides=(("b", "🐮 选牛"), ("h", "👨‍👩 选人")),
)
