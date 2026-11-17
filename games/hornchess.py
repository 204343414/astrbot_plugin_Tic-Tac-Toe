"""牛角棋（钻牛角／赶牛）：规则、穷举求解、AI、卡片。纯 Python，不依赖 Pillow。

棋盘
----
照民间真盘画：一个**高瘦的牛角**，左边一条（微弯的）竖边、右边一条外张的斜边，
中间五道横杆，再用一组**互相平行的长斜线**把横杆连起来。6 行共 11 个点：

        A1                 ← 牛角尖：人把牛赶到这里堵死就赢
        │  ╲
        B1──B2
        │  ╱│  ╲
        C1──C2
        │  ╱│  ╲
        D1──D2
        │  ╱│  ╲
        E1──E2
        │  ╱│  ╲
        F1──F2             ← 底角：牛冲到这里就跑了

连线规律沿用民间记谱：**每个点只连编号 ±1 / ±2 的点**。编号一变，整张图的连线
就自动出来了：左列是 A1-B1-C1-D1-E1-F1（编号 1,2,4,6,8,10），右列是
A1-B2-C2-D2-E2-F2（编号 1,3,5,7,9,11），横杆两端编号差 1，斜线两端差 1 或 2。
一共 19 条线，和实拍棋盘一一对得上，不用手写邻接表。

双方
----
* **牛**（1 子）在牛角尖 A1，**前后左右都能走**，一步走到相邻的空点。
* **赶牛的人**（2 子）在底角 F1、F2，**只能往尖上赶，不能后退**；
  但**同一条横杆上可以左右挪**（B1↔B2、C1↔C2……），挪来挪去都行。
* 人先走。

这条"同杆可横走、前进不可退"是民间原话（"不前进就在横杆上来回晃，往前走了就
退不回来"），也是这个棋的关键：横走能腾挪，但一直横走不动就把时间耗光了。

胜负
----
* 牛无路可走 → **人赢**（把牛憋死在牛角尖上）。
* 牛走到 F1／F2 → **牛赢**（冲出牛角）。人要往尖上赶就必然要让开底角，
  所以"该不该让、什么时候让"才是这个棋真正的博弈点。
* 步数用完（``PLY_LIMIT``）还没困住 → **牛赢**（民间"多少步没围住就算牛赢"）。
  这条必须有，否则人可以在横杆上无限横走，一局棋直接摆烂。

关于平衡（穷举 990 个局面得出的结论，别改规则前先看这里）
----------------------------------------------------------
11 点盘上牛**前后左右自由**、人**不能后退**，穷举结果是：990 个局面里
牛胜 954、人胜 36——**牛只要不犯错就永远跑得掉**。所以这个棋里"人赢"实际
取决于牛犯错：

=========  ===============  ==========================
难度        牛的犯错概率      实际体验
=========  ===============  ==========================
简单        45%              人大多能围住
普通        22%              互有胜负
困难        8%               牛几乎不失误，很难赢
=========  ===============  ==========================

（困难档也留一点犯错空间，否则在 11 点盘上是"永远赢不了"，那不是难度是堵墙。）
想要"人能凭本事赢"的版本，把 ``FARMERS_MAY_RETREAT`` 改成 ``True``：穷举显示
人可进退时局面直接翻过来，人是必胜方。

AI
--
局面只有 990 个，**穷举比搜索强**：逆向分析一次性把所有局面的胜负和"几步见分晓"
算出来并缓存，之后每一步直接查表，比任何启发式都准，算一次也就几十毫秒。
牛的同分走法优先选"活得更久"的那一步——它当然也知道拖到步数用完就赢。
"""
from __future__ import annotations

import random
from functools import lru_cache
from typing import Any

from . import lobby

# --- 棋盘 -------------------------------------------------------------------

#: 每行的点数，从牛角尖到底角。
ROWS = (1, 2, 2, 2, 2, 2)
ROW_LETTERS = "ABCDEF"
TIP = 0
BASE_ROW = len(ROWS) - 1

#: 每个点的名字，A1 是牛角尖。下标即点的编号（0 起的民间编号）。
NAMES: tuple[str, ...] = tuple(
    f"{ROW_LETTERS[row]}{col + 1}"
    for row, count in enumerate(ROWS)
    for col in range(count)
)
#: 点名 → 下标
INDEX = {name: index for index, name in enumerate(NAMES)}
#: 每个点在第几行（0 = 牛角尖）
DEPTH: tuple[int, ...] = tuple(
    row for row, count in enumerate(ROWS) for _ in range(count)
)
COUNT = len(NAMES)

HUMANS = "h"
BULL = "b"
MARKS = {HUMANS: "👨‍👩", BULL: "🐮"}

#: 两枚人子的图标：按 ``state["humans"]`` 里的槽位固定，走得再乱也不会扮演串。
PIECE_MARKS = ("👨", "👩")
#: 对应的贴图文件名（字体画不出彩色 emoji，出图时贴 ``assets/emoji/*.png``）。
PIECE_ASSETS = ("man", "woman")
SPEC = lobby.HORNCHESS

MODE_AI = "ai"
MODE_PVP = "pvp"
PHASE_WAITING = "waiting"
PHASE_PLAYING = "playing"

#: 牛"冲出牛角"的落点：**底部两个角**。民间原话是"到达牛角底部任意一角"。
ESCAPE_POINTS = (9, 10)     # F1、F2

#: 一手 = 一方走一步。走这么多手还没困住牛就判牛赢（民间规矩），
#: 同时也是"不许在横杆上无限横走"的闸门。民间给的口径是"约 2×点数+3 步"，
#: 11 点盘就是 25 手左右，这里放宽到 60 手，别让人还没围上就被判输。
PLY_LIMIT = 60

#: True 时人也能后退。默认 False（民间规矩：人只能往前赶）。
#: 想让人"凭本事赢"就把它改成 True——穷举显示那时人是必胜方。
FARMERS_MAY_RETREAT = False


def name_of(index: int) -> str:
    return NAMES[index] if 0 <= index < len(NAMES) else "?"


@lru_cache(maxsize=None)
def neighbors(index: int) -> tuple[int, ...]:
    """相邻的点：编号 ±1 / ±2。民间记谱就这一条，画出来正好是牛角盘。"""
    return tuple(j for j in (index - 2, index - 1, index + 1, index + 2)
                 if 0 <= j < COUNT)


def column_of(index: int) -> int:
    return index - sum(ROWS[:DEPTH[index]])


@lru_cache(maxsize=None)
def forward_targets(index: int) -> tuple[int, ...]:
    """人这枚子能去的点：**往前（行号变小）**，或**同一行左右挪**；不能后退。"""
    depth = DEPTH[index]
    if FARMERS_MAY_RETREAT:
        return neighbors(index)
    return tuple(j for j in neighbors(index) if DEPTH[j] <= depth)


def is_lateral(index: int, target: int) -> bool:
    """这步是不是"横走"（同一行左右挪，可以来回）。"""
    return DEPTH[index] == DEPTH[target]


# --- 状态 -------------------------------------------------------------------

def new_state(mode: str, host_openid: str, level: str = "normal") -> dict[str, Any]:
    players = ({HUMANS: host_openid} if mode == MODE_AI
               else {HUMANS: host_openid, BULL: ""})
    return {
        "game": "hornchess",
        "mode": mode,
        "turn": HUMANS,
        "bull": TIP,
        "humans": list(ESCAPE_POINTS),        # 两枚人子摆在底角（顺序＝槽位）
        "players": players,
        "labels": {},
        "level": lobby.normalize_level(level),
        "last_move": None,
        "history": [],
        "plies": 0,
        "session_id": "",
        "phase": PHASE_WAITING if mode == MODE_PVP else PHASE_PLAYING,
    }


def _occupied(state: dict[str, Any]) -> set[int]:
    return {state["bull"], *state["humans"]}


def _key(state: dict[str, Any]) -> tuple:
    return (state["bull"], tuple(sorted(state["humans"])), state["turn"])


def legal_moves(state: dict[str, Any]) -> list[tuple[str, int, int]]:
    """所有合法走法：``(哪一方, 从, 到)``。"""
    occupied = _occupied(state)
    out: list[tuple[str, int, int]] = []
    if state["turn"] == BULL:
        for target in neighbors(state["bull"]):
            if target not in occupied:
                out.append((BULL, state["bull"], target))
    else:
        for piece in state["humans"]:
            for target in forward_targets(piece):
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
    if side == BULL and actor_openid:
        owner = str((state.get("players") or {}).get(BULL) or "")
        if owner and actor_openid != owner:
            return "现在轮到牛走，这步不是你走的"
    matches = [m for m in legal_moves(state) if m[1] == frm and m[2] == to]
    if not matches:
        if side == HUMANS and DEPTH[to] > DEPTH[frm]:
            return "人只能往牛角尖赶，不能后退（同一条横杆上可以左右挪）"
        return "这步走不了"
    _commit(state, frm, to)
    return ""


def _commit(state: dict[str, Any], frm: int, to: int) -> None:
    """纯落子，不校验（AI 也走这条路，校验会把 AI 挡在"不是你的回合"上）。"""
    side = state["turn"]
    if side == BULL:
        state["bull"] = to
        state["turn"] = HUMANS
    else:
        humans = list(state["humans"])
        humans[humans.index(frm)] = to
        # 不排序：``humans`` 的下标就是"这是人1还是人2"，排序了图标会互换。
        state["humans"] = humans
        state["turn"] = BULL
    state["last_move"] = {"side": side, "frm": frm, "to": to}
    state["plies"] = int(state.get("plies", 0)) + 1
    state["history"] = list(state.get("history") or []) + [_key(state)]


# --- 胜负 -------------------------------------------------------------------

def result(state: dict[str, Any]) -> str:
    """``""`` 未结束；否则 ``bull`` / ``human``。"""
    if state["bull"] in ESCAPE_POINTS:
        return BULL                       # 牛冲出牛角
    if not legal_moves(state):
        return HUMANS if state["turn"] == BULL else BULL
    if int(state.get("plies", 0)) >= PLY_LIMIT:
        return BULL                       # 拖到步数用完 = 牛撑住了
    return ""


def is_over(state: dict[str, Any]) -> bool:
    return bool(result(state))


def plies_left(state: dict[str, Any]) -> int:
    return max(0, PLY_LIMIT - int(state.get("plies", 0)))


def status_text(state: dict[str, Any]) -> str:
    outcome = result(state)
    if outcome == BULL:
        if state["bull"] in ESCAPE_POINTS:
            return f"{MARKS[BULL]} 牛从底角冲出牛角，牛方获胜！"
        if not legal_moves(state):
            return f"{MARKS[BULL]} 赶牛的人动不了了，牛方获胜！"
        return f"{MARKS[BULL]} 步数用完牛还没被围住，牛方获胜！"
    if outcome == HUMANS:
        return "🎉 牛被憋死在牛角尖上，赶牛的人获胜！"
    turn = state["turn"]
    tail = f"（还剩 {plies_left(state)} 手）"
    if state["mode"] == MODE_AI:
        base = ("轮到你赶牛 👨‍👩" if turn == HUMANS
                else f"{MARKS[BULL]} 牛在找出路…")
    else:
        base = f"轮到 {MARKS[turn]} {'赶牛' if turn == HUMANS else '牛'} 走棋"
    return base + tail


def player_label(state: dict[str, Any], side: str) -> str:
    labels = state.get("labels") or {}
    label = str(labels.get(side) or "").strip()
    if label:
        return label
    openid = str((state.get("players") or {}).get(side) or "")
    return f"玩家…{openid[-4:]}" if openid else ""


# --- 穷举求解（这就是这个棋的"引擎"） --------------------------------------

@lru_cache(maxsize=1)
def _solve() -> dict[tuple, tuple[str, int]]:
    """逆向分析：每个局面 ``(牛在哪, 人在哪, 轮到谁)`` → ``(谁赢, 几步见分晓)``。

    局面总数 = 11 个牛位置 × C(10,2) 个人位置 × 2 个走子方 = 990 个，与其写个笨搜索
    不如一次算准。顺带也回答了"这棋到底平不平衡"（见模块开头的表）。

    步数是有意义的：牛靠"拖"也能赢（步数用完算牛赢），所以 AI 会挑离被困最远的
    那一步，玩家也能看见还剩几手。
    """
    from collections import deque
    from itertools import combinations

    states: list[tuple] = []
    for bull in range(COUNT):
        for humans in combinations([i for i in range(COUNT) if i != bull], 2):
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
                for target in forward_targets(piece):
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

    resolved: dict[tuple, tuple[str, int]] = {}
    for state in states:
        bull, _, turn = state
        if bull in ESCAPE_POINTS:
            resolved[state] = (BULL, 0)
        elif not children[state]:
            resolved[state] = (HUMANS if turn == BULL else BULL, 0)

    # 按步数从小到大推进：轮到谁走，谁先遇到"自己赢"的子局面就用它；
    # 所有子局面都是对方赢，才会认输——这样算出来的步数就是"最快/最慢几步"。
    queue = deque(sorted(resolved, key=lambda s: resolved[s][1]))
    remaining = {s: len(children[s]) for s in states if s not in resolved}
    while queue:
        state = queue.popleft()
        winner, depth = resolved[state]
        for parent in parents[state]:
            if parent in resolved:
                continue
            if parent[2] == winner:
                resolved[parent] = (winner, depth + 1)
                queue.append(parent)
            else:
                remaining[parent] -= 1
                if remaining[parent] == 0:
                    resolved[parent] = (winner, depth + 1)
                    queue.append(parent)
    # 剩下的是"谁也赢不了"的循环局面：拖到步数用完，算牛赢。
    for state in states:
        resolved.setdefault(state, (BULL, PLY_LIMIT))
    return resolved


def solve_state(state: dict[str, Any]) -> str:
    return _solve().get(_key(state), (BULL, PLY_LIMIT))[0]


def solve_depth(state: dict[str, Any]) -> int:
    """从现在起最快（对轮到走的一方）几步见分晓。"""
    return _solve().get(_key(state), (BULL, PLY_LIMIT))[1]


# --- AI ---------------------------------------------------------------------

#: 难度 → 牛的犯错概率。11 点盘上"完美走"的牛不可战胜（穷举见模块开头），
#: 所以困难档也得留一点犯错空间，否则玩家永远赢不了——那不是难度。
LEVEL_BLUNDER = {"easy": 0.45, "normal": 0.22, "hard": 0.08}


def _score_for(state: dict[str, Any], move: tuple[str, int, int]) -> tuple[int, int]:
    """走完这步之后，从"牛"的角度看有多好：先比胜负，再比谁能拖更久。"""
    side, frm, to = move
    if side == BULL:
        key = (to, tuple(sorted(state["humans"])), HUMANS)
    else:
        humans = list(state["humans"])
        humans[humans.index(frm)] = to
        key = (state["bull"], tuple(sorted(humans)), BULL)
    winner, depth = _solve().get(key, (BULL, PLY_LIMIT))
    rank = 2 if winner == BULL else 0
    # 牛要拖：同分时步数越大越好（对牛来说"越晚见分晓"越有利）。
    return rank, depth


def ai_move(state: dict[str, Any], rng: random.Random | None = None) -> tuple[int, int] | None:
    """牛走一步，返回 ``(从, 到)``；``None`` 表示没得走。"""
    rng = rng or random.Random()
    moves = [m for m in legal_moves(state) if m[0] == BULL]
    if not moves:
        return None
    if len(moves) == 1:
        _, frm, to = moves[0]
        _commit(state, frm, to)
        return frm, to

    scored = sorted(((_score_for(state, m), m) for m in moves),
                    key=lambda pair: pair[0], reverse=True)
    best = scored[0][0]
    top = [m for score, m in scored if score == best]
    level = str(state.get("level") or "normal")
    blunder = LEVEL_BLUNDER.get(level, LEVEL_BLUNDER["normal"])

    if blunder and rng.random() < blunder:
        worse = [m for score, m in scored if score < best]
        chosen = rng.choice(worse) if worse else rng.choice(top)
    else:
        chosen = rng.choice(top)

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
CARD_IMAGE_WIDTH = 620
CARD_IMAGE_HEIGHT = 720


def _move_buttons(state: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """把合法走法直接做成按钮：**一次点击就是一步棋**。

    11 个点、每方最多 4 种走法，全部塞得下（QQ 键盘一行 5 个按钮也够）。
    横走和前进在按钮上用不同名字（点名）区分，玩家一看就能对上图。
    """
    moves = legal_moves(state)
    rows: list[list[dict[str, Any]]] = []

    def button(label: str, frm: int, to: int) -> dict[str, Any]:
        return {
            "id": f"mv_{frm}_{to}",
            "label": label,
            "style": 1,
            "action_id": lobby.action_id(SPEC, "move"),
            "params": {"frm": frm, "to": to},
        }

    if state["turn"] == BULL:
        rows.append([button(f"{MARKS[BULL]} {name_of(frm)}→{name_of(to)}", frm, to)
                     for _, frm, to in moves])
    else:
        for slot, piece in enumerate(state["humans"]):
            mark = PIECE_MARKS[slot % len(PIECE_MARKS)]
            mine = [button(f"{mark} {name_of(frm)}→{name_of(to)}", frm, to)
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
            lines.append("人只能往尖上赶、不能后退，同一横杆上可以左右挪；"
                         "牛前后左右都行，冲到底角就跑了。")
    return {
        "id": "hornchess_board",
        "markdown": "\n".join(lines),
        "rows": [] if is_over(state) else _move_buttons(state),
        "one_shot": False,
        "ttl_seconds": 3600,
    }
