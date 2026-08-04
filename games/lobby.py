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


def action_id(spec: GameSpec, name: str) -> str:
    return f"{spec.key}.{name}"


def normalize_level(level: object) -> str:
    return level if level in AI_LEVELS else LEVEL_NORMAL


def build_lobby_card(spec: GameSpec, level: str = LEVEL_NORMAL) -> dict[str, Any]:
    """The five-button entry card. Open to everyone: anyone may start a game."""
    level = normalize_level(level)
    return {
        "id": f"{spec.key}_lobby",
        "markdown": "\n".join([
            f"# {spec.title}",
            f"AI 难度：**{LEVEL_LABELS[level]}**",
            "",
            spec.how_to,
        ]),
        "rows": [
            [
                {"id": "start_ai", "label": "🤖 人机对战", "style": 1,
                 "action_id": action_id(spec, "start_ai"),
                 "params": {"level": level}},
                {"id": "start_pvp", "label": "👥 群友对战", "style": 1,
                 "action_id": action_id(spec, "start_pvp"), "params": {}},
            ],
            [
                {"id": f"level_{name}",
                 "label": f"{'✅ ' if name == level else ''}{LEVEL_LABELS[name]}",
                 "style": 0,
                 "action_id": action_id(spec, "set_level"),
                 "params": {"level": name}}
                for name in AI_LEVELS
            ],
        ],
        # Not one_shot: the difficulty buttons are meant to be pressed more
        # than once, and the Hub burns a one-shot button before the game runs.
        "one_shot": False,
        "ttl_seconds": 3600,
    }


def build_waiting_card(spec: GameSpec, host_label: str = "",
                       host_openid: str = "") -> dict[str, Any]:
    """Shown after 「群友对战」: one seat taken, waiting for an opponent."""
    host = host_label or "发起者"
    return {
        "id": f"{spec.key}_waiting",
        "markdown": "\n".join([
            f"# {spec.title} · 等待对手",
            f"{spec.host_mark} {host} 已入座，等待一位群友加入…",
            "",
            f"点击「加入对战」成为 {spec.guest_mark} 方。",
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
