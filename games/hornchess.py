"""牛角棋（钻牛角／赶牛）：规则、穷举求解、AI、卡片。纯 Python，不依赖 Pillow。

棋盘与规则
----------
民间画法是"牛角"：一个上尖下宽的三角形，里面有几条与底边平行的横线，再用
斜线把它们连起来（见 2021 年那篇讲"地头棋"的文章）。这里取 **4 层**：

    A          1 个点（牛角尖）
    B B        2 个点
    C C C      3 个点
    D D D D    4 个点（底线）        —— 共 10 个点

之所以是 4 层而不是民间的 5 层：QQ 键盘最多 5 行、每行 5 个按钮，10 个点加上
操作按钮正好塞得下；15 个点就会把"结束对局"挤掉。

双方
----
* **牛**（1 子）在牛角尖 A1，**可进可退**，走一步到相邻的空点。
* **赶牛的人**（2 子）在底线两个角 D1、D4，**只能进不能退**。
* 人先走。

胜负
----
* 牛走到任意底线点 → 牛赢（"冲出牛角"）。
* 牛无路可走 → 人赢（"憋死牛"）。
* 人无路可走 → 牛赢（对称的"憋死"）。

关于"只能进不能退"的一点说明
----------------------------
这棋看着简单，但**规则差一点，平衡就完全反过来**。我把全部局面穷举了一遍
（4 层棋盘 720 个局面），结论是：

* 人严格"只朝牛角尖走"：开局就是**牛必胜**，人类一方全程只有 5 个能赢的
  局面。原因很直白——人总共只有 6 步可走，走完动不了，而牛只要在下面等着。
* 人"不能后退、但同一层可以横走"：完美对弈变成**和棋**（谁失误谁输），
  和井字棋一个性质，人类玩家真有赢面。
* 牛的胜利条件必须是"到达**底部两个角**"（民间原话是"到达牛角底部任意
  一角"）。如果放宽成"到达底线任意一点"，牛几乎总能直接冲下去，人就白玩了。

所以默认取后两种组合。想要严格只前进的版本，把 ``STRICT_FORWARD`` 设成
True（插件配置 ``horn_strict_forward``），代价是人类基本赢不了。

AI
--
局面只有几百个，**穷举比搜索强**：开局时用逆向分析把所有局面的胜负和一次性
算出来并缓存，之后每一步直接查表。这比任何启发式都准，而且一次也算不了几毫秒。
难度同样用"按概率犯傻"来表达，和棋类小游戏里的国际象棋一致。
"""
from __future__ import annotations

import random
from functools import lru_cache
from typing import Any

from . import lobby

# --- 棋盘 -------------------------------------------------------------------

#: 每层的点数，从牛角尖到底线。
ROWS = (1, 2, 3, 4)
ROW_LETTERS = "ABCD"
TIP = 0
BASE_ROW = len(ROWS) - 1

#: 每个点的名字，A1 是牛角尖。下标即点的编号。
NAMES: tuple[str, ...] = tuple(
    f"{ROW_LETTERS[row]}{col + 1}"
    for row, count in enumerate(ROWS)
    for col in range(count)
)
#: 点名 → 下标
INDEX = {name: index for index, name in enumerate(NAMES)}
#: 每个点在第几层
DEPTH: tuple[int, ...] = tuple(
    row for row, count in enumerate(ROWS) for _ in range(count)
)

HUMANS = "h"
BULL = "b"
MARKS = {HUMANS: "🧑‍🌾", BULL: "🐂"}
SPEC = lobby.HORNCHESS

MODE_AI = "ai"
MODE_PVP = "pvp"
PHASE_WAITING = "waiting"
PHASE_PLAYING = "playing"

#: True 时人只能往牛角尖方向走（严格照民间说法，人类几乎赢不了）。
STRICT_FORWARD = False

#: 牛"冲出牛角"的落点：**底部两个角**。民间原话是"到达牛角底部任意一角"，
#: 而放宽成"底线任意一点"会让牛直接冲下去，人必输——穷举验证过。
ESCAPE_POINTS = (6, 9)      # D1、D4

#: 走这么多手还不分胜负就判和，防止无限绕圈。
PLY_LIMIT = 80


def name_of(index: int) -> str:
    return NAMES[index] if 0 <= index < len(NAMES) else "?"


@lru_cache(maxsize=None)
def neighbors(index: int) -> tuple[int, ...]:
    """相邻的点。三角形格子：同行左右、下一层两个、上一层两个。"""
    depth = DEPTH[index]
    col = index - sum(ROWS[:depth])
    found: list[int] = []
    base_next = sum(ROWS[:depth + 1])
    if col > 0:
        found.append(index - 1)
    if col + 1 < ROWS[depth]:
        found.append(index + 1)
    if depth + 1 < len(ROWS):
        # 下一层更宽：本点在下一层对应 col 和 col+1 两个位置
        for offset in (0, 1):
            if col + offset < ROWS[depth + 1]:
                found.append(base_next + col + offset)
    if depth - 1 >= 0:
        base_prev = sum(ROWS[:depth - 1])
        for offset in (-1, 0):
            if 0 <= col + offset < ROWS[depth - 1]:
                found.append(base_prev + col + offset)
    return tuple(sorted(found))


def column_of(index: int) -> int:
    return index - sum(ROWS[:DEPTH[index]])


def human_targets(index: int, strict: bool | None = None) -> tuple[int, ...]:
    """人这枚子能去哪些点。默认不能后退，但可以同层横走。"""
    strict = STRICT_FORWARD if strict is None else strict
    depth = DEPTH[index]
    if strict:
        return tuple(n for n in neighbors(index) if DEPTH[n] < depth)
    return tuple(n for n in neighbors(index) if DEPTH[n] <= depth)


# --- 状态 -------------------------------------------------------------------

def new_state(mode: str, host_openid: str, level: str = "normal") -> dict[str, Any]:
    players = ({HUMANS: host_openid} if mode == MODE_AI
               else {HUMANS: host_openid, BULL: ""})
    humans = sorted((INDEX["D1"], INDEX["D4"]))
    return {
        "game": "hornchess",
        "mode": mode,
        "turn": HUMANS,
        "bull": TIP,
        "humans": humans,
        "players": players,
        "labels": {},
        "level": lobby.normalize_level(level),
        "last_move": None,
        "history": [],
        "plies": 0,
        "session_id": "",
        "phase": PHASE_WAITING if mode == MODE_PVP else PHASE_PLAYING,
        "strict": STRICT_FORWARD,
    }


def _occupied(state: dict[str, Any]) -> set[int]:
    return {state["bull"], *state["humans"]}


def _key(state: dict[str, Any]) -> tuple:
    return (state["bull"], tuple(sorted(state["humans"])), state["turn"])


def legal_moves(state: dict[str, Any]) -> list[tuple[str, int, int]]:
    """所有合法走法：``(哪一方, 从, 到)``。

    人的走法带上"哪个子"的信息，因为两枚人的子走法不同；用起点区分就够了
    （起点唯一对应一枚子）。
    """
    occupied = _occupied(state)
    out: list[tuple[str, int, int]] = []
    if state["turn"] == BULL:
        for target in neighbors(state["bull"]):
            if target not in occupied:
                out.append((BULL, state["bull"], target))
    else:
        strict = bool(state.get("strict", STRICT_FORWARD))
        for piece in state["humans"]:
            for target in human_targets(piece, strict):
                if target not in occupied:
                    out.append((HUMANS, piece, target))
    return out


def apply_move(state: dict[str, Any], frm: int, to: int,
               actor_openid: str = "") -> str:
    """落一步。返回 ``""`` 表示成功，否则是给玩家看的原因。"""
    if state.get("phase") == PHASE_WAITING:
        return "还在等对手加入，点「加入对战」入座"
    if is_over(state):
        return "对局已结束"
    side = state["turn"]
    if state["mode"] == MODE_PVP:
        holder = state["players"].get(side)
        if not holder:
            if actor_openid in state["players"].values():
                return "等待对方走棋"
            state["players"][side] = actor_openid
        elif holder != actor_openid:
            return "现在不是你的回合"
    else:
        # 人机：玩家永远是人这边，牛由 AI 走
        if side != HUMANS:
            return "请等牛走完"
        if state["players"].get(HUMANS) != actor_openid:
            return "这不是你的对局"

    moves = legal_moves(state)
    if (side, frm, to) not in moves:
        return f"{name_of(frm)}→{name_of(to)} 走不了"
    _commit(state, frm, to)
    return ""


def _commit(state: dict[str, Any], frm: int, to: int) -> None:
    """真正落子，不做权限检查。

    AI 走子也走这里：早先把权限检查写在落子里，结果牛由 AI 走时被自己的
    "不是你的回合"挡住，对局卡死。权限是"玩家能不能动这枚子"的问题，
    和"把子挪过去"是两件事，分开就不会再互相绊住。
    """
    side = state["turn"]
    if side == BULL:
        state["bull"] = to
        state["turn"] = HUMANS
    else:
        humans = list(state["humans"])
        humans[humans.index(frm)] = to
        state["humans"] = sorted(humans)
        state["turn"] = BULL
    state["last_move"] = {"side": side, "frm": frm, "to": to}
    state["plies"] = int(state.get("plies", 0)) + 1
    state["history"] = list(state.get("history") or []) + [_key(state)]


# --- 胜负 -------------------------------------------------------------------

def result(state: dict[str, Any]) -> str:
    """``""`` 未结束；否则 ``bull`` / ``human`` / ``draw``。"""
    if state["bull"] in ESCAPE_POINTS:
        return BULL            # 牛冲出牛角（底角）
    if not legal_moves(state):
        return HUMANS if state["turn"] == BULL else BULL
    if int(state.get("plies", 0)) >= PLY_LIMIT:
        return "draw"
    history = list(state.get("history") or [])
    if history.count(_key(state)) >= 3:
        return "draw"
    return ""


def is_over(state: dict[str, Any]) -> bool:
    return bool(result(state))


def status_text(state: dict[str, Any]) -> str:
    outcome = result(state)
    if outcome == BULL:
        if state["bull"] in ESCAPE_POINTS:
            return "🐂 牛冲出牛角，牛方获胜！"
        return "🐂 赶牛的人无路可走，牛方获胜！"
    if outcome == HUMANS:
        return "🎉 牛被憋死在牛角尖，赶牛的人获胜！"
    if outcome == "draw":
        return "🤝 走了太多步，判和。"
    turn = state["turn"]
    if state["mode"] == MODE_AI:
        return "轮到你赶牛 🧑‍🌾" if turn == HUMANS else "牛在找出路 🐂…"
    return f"轮到 {MARKS[turn]} {'赶牛' if turn == HUMANS else '牛'} 走棋"


def player_label(state: dict[str, Any], side: str) -> str:
    labels = state.get("labels") or {}
    label = str(labels.get(side) or "").strip()
    if label:
        return label
    openid = str((state.get("players") or {}).get(side) or "")
    return f"玩家…{openid[-4:]}" if openid else ""


# --- 穷举求解（这是这个游戏的"AI 引擎"） ----------------------------------

@lru_cache(maxsize=2)
def _solve(strict: bool) -> dict[tuple, str]:
    """逆向分析出每个局面的结果。缓存起来，进程内只算一次。

    局面总数只有 720 个，与其写个笨搜索，不如把所有结果一次性算准。这也
    顺便回答了"这棋平衡吗"——见模块开头的说明。
    """
    from collections import deque
    from itertools import combinations

    points = list(range(len(NAMES)))
    states = []
    for bull in points:
        for humans in combinations(points, 2):
            if bull in humans:
                continue
            for turn in (HUMANS, BULL):
                states.append((bull, humans, turn))

    def moves_from(key: tuple) -> list[tuple]:
        bull, humans, turn = key
        occupied = {bull, *humans}
        out = []
        if turn == BULL:
            for target in neighbors(bull):
                if target not in occupied:
                    out.append((target, humans, HUMANS))
        else:
            for piece in humans:
                for target in human_targets(piece, strict):
                    if target in occupied:
                        continue
                    updated = list(humans)
                    updated[updated.index(piece)] = target
                    out.append((bull, tuple(sorted(updated)), BULL))
        return out

    children = {state: moves_from(state) for state in states}
    parents: dict[tuple, list[tuple]] = {state: [] for state in states}
    for state in states:
        for child in children[state]:
            if child in parents:
                parents[child].append(state)

    resolved: dict[tuple, str] = {}
    for state in states:
        bull, _, turn = state
        if bull in ESCAPE_POINTS:
            resolved[state] = BULL
        elif not children[state]:
            resolved[state] = HUMANS if turn == BULL else BULL

    queue = deque(resolved)
    remaining = {s: len(children[s]) for s in states if s not in resolved}
    while queue:
        state = queue.popleft()
        winner = resolved[state]
        for parent in parents[state]:
            if parent in resolved:
                continue
            if winner == parent[2]:
                # 轮到谁走，谁就能走到"自己赢"的局面
                resolved[parent] = winner
                queue.append(parent)
            else:
                remaining[parent] -= 1
                if remaining[parent] == 0:
                    resolved[parent] = winner
                    queue.append(parent)
    for state in states:
        resolved.setdefault(state, "draw")
    return resolved


def solve_state(state: dict[str, Any]) -> str:
    table = _solve(bool(state.get("strict", STRICT_FORWARD)))
    return table.get(_key(state), "draw")


# --- AI ---------------------------------------------------------------------

#: 难度 → 犯傻概率。困难档查表走最优，不下错棋。
LEVEL_BLUNDER = {"easy": 0.45, "normal": 0.15, "hard": 0.0}


def _score_for(state: dict[str, Any], move: tuple[str, int, int],
               table: dict[tuple, str]) -> int:
    """走到这个走法之后，从"牛"的角度看有多好。"""
    side, frm, to = move
    if side == BULL:
        nxt = _key_after(state, move)
    else:
        nxt = _key_after(state, move)
    outcome = table.get(nxt, "draw")
    # table 的结果是"最终谁赢"。牛赢 = 好，人赢 = 差，和 = 中。
    if outcome == BULL:
        return 2
    if outcome == HUMANS:
        return 0
    return 1


def _key_after(state: dict[str, Any], move: tuple[str, int, int]) -> tuple:
    side, frm, to = move
    if side == BULL:
        return (to, tuple(sorted(state["humans"])), HUMANS)
    humans = list(state["humans"])
    humans[humans.index(frm)] = to
    return (state["bull"], tuple(sorted(humans)), BULL)


def ai_move(state: dict[str, Any], rng: random.Random | None = None) -> tuple[int, int] | None:
    """牛走一步。返回 ``(从, 到)``，``None`` 表示没得走。

    三档难度的差别只有一个：**按概率不按最优走**。这个游戏局面小到可以查表
    全知，所以"看得更远"没有意义，能拉开差距的只有"会不会犯错"。
    """
    rng = rng or random.Random()
    moves = [m for m in legal_moves(state) if m[0] == BULL]
    if not moves:
        return None
    if len(moves) == 1:
        _, frm, to = moves[0]
        _commit(state, frm, to)
        return frm, to

    table = _solve(bool(state.get("strict", STRICT_FORWARD)))
    level = str(state.get("level") or "normal")
    blunder = LEVEL_BLUNDER.get(level, LEVEL_BLUNDER["normal"])

    scored = sorted(((_score_for(state, m, table), m) for m in moves),
                    key=lambda pair: pair[0], reverse=True)
    best = scored[0][0]
    top = [m for score, m in scored if score == best]

    if blunder and rng.random() < blunder:
        worse = [m for score, m in scored if score < best]
        if worse:
            chosen = rng.choice(worse)
        else:
            chosen = rng.choice(top)
    else:
        # 同分时优先往底线走：牛的目标是冲出去，不是绕圈。
        chosen = max(top, key=lambda m: DEPTH[m[2]])

    _, frm, to = chosen
    _commit(state, frm, to)
    return frm, to


# --- 卡片 -------------------------------------------------------------------

def build_lobby_card(level: str = "normal") -> dict[str, Any]:
    return lobby.build_lobby_card(SPEC, level)


def build_waiting_card(state: dict[str, Any], host_label: str = "") -> dict[str, Any]:
    host_openid = str((state.get("players") or {}).get(HUMANS) or "")
    return lobby.build_waiting_card(SPEC, host_label, host_openid)


#: 卡片里声明的图片尺寸（渲染器保证一致）。
CARD_IMAGE_WIDTH = 720
CARD_IMAGE_HEIGHT = 620


def _move_buttons(state: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """把合法走法直接做成按钮：**一次点击就是一步棋**。

    这是这个棋种值得单独做一个的原因——10 个点、每方最多 8 种走法，全部塞得下。
    斗兽棋要点两次（先选动物再选方向），国际象棋只能打字；这里点一下就走完。
    """
    moves = legal_moves(state)
    rows: list[list[dict[str, Any]]] = []

    def button(label: str, frm: int, to: int, style: int = 1) -> dict[str, Any]:
        return {
            "id": f"mv_{frm}_{to}",
            "label": label,
            "style": style,
            "action_id": lobby.action_id(SPEC, "move"),
            "params": {"frm": frm, "to": to},
        }

    if state["turn"] == BULL:
        # 牛最多 6 种走法，5 个一行折行（QQ 每行上限 5）
        buttons = [button(f"🐂 {name_of(frm)}→{name_of(to)}", frm, to)
                   for _, frm, to in moves]
        for start in range(0, len(buttons), 5):
            rows.append(buttons[start:start + 5])
    else:
        # 人的两枚子各占一行：点下去到底动了哪枚子，看一眼就知道
        for piece in sorted(state["humans"], key=lambda i: (DEPTH[i], i)):
            mine = [button(f"🧑‍🌾 {name_of(frm)}→{name_of(to)}", frm, to)
                    for _, frm, to in moves if frm == piece]
            if mine:
                rows.append(mine)

    rows.append([
        {"id": "quit", "label": "🔚 结束对局", "style": 0,
         "action_id": lobby.action_id(SPEC, "quit"), "params": {}},
    ])
    return rows


def build_board_card(state: dict[str, Any], image_url: str = "") -> dict[str, Any]:
    lines = [f"**{status_text(state)}**"]
    if image_url:
        lines.append(
            f"![棋盘 #{CARD_IMAGE_WIDTH}px #{CARD_IMAGE_HEIGHT}px]({image_url})")
    if not is_over(state):
        if state["mode"] == MODE_AI and state["turn"] == BULL:
            lines.append("牛正在找路…")
        else:
            lines.append("点下面的按钮走棋：牛可进退，人只能前进（不能退）。")
    return {
        "id": "hornchess_board",
        "markdown": "\n".join(lines),
        "rows": [] if is_over(state) else _move_buttons(state),
        "one_shot": False,
        "ttl_seconds": 3600,
    }
