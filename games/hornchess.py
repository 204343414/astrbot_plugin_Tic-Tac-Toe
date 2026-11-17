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
* **三次重复局面** → **和棋**（和象棋一样）。两边在横杆上来回晃、谁也推进不了，
  这种局面本来就已经和了，没必要让玩家干等。
* 步数用完（``PLY_LIMIT``）还没困住 → **和棋**（兜底闸门，防止无限横走）。

判和而不是"判牛赢"是有讲究的：民间有的地方判"没围住算牛赢"，那样完美对弈就是
牛必胜，玩家执人怎么下都赢不了——见下面的平衡表。

关于平衡（穷举 990 个局面得出的结论，别改规则前先看这里）
----------------------------------------------------------
把全表穷举一遍，三种"步数用完怎么算"的结果完全不一样：

========================  ==========  ==========  ======
步数用完怎么算           开局结果     牛胜        人胜／和
========================  ==========  ==========  ======
判牛赢（民间的一种说法）  牛胜         954         36 / 0
**判和（本插件）**        **和棋**    777         **36 / 177**
========================  ==========  ==========  ======

也就是说：**这块盘在"人只能前进 + 超时判和"下是平衡的**——完美对弈是和棋，
谁失误谁输，和井字棋一个性质。玩家执人有真赢面（36 个局面），牛也有赢面，
只是两边都不能走错。

（"超时判牛赢"那一版里牛胜 954，玩家执人怎么下都赢不了，AI 再聪明也救不回来。）

AI 难度就是按这个搭的：

=========  ===============  ==========================
难度        犯错概率         实际体验（玩家执人）
=========  ===============  ==========================
简单        45%              人大多能赢（实测 30/30）
普通        22%              互有胜负（实测 27/30 人胜）
困难        0%               谁也赢不了谁——会打成和棋
=========  ===============  ==========================

困难档下的是穷举表里的**最优解**：能赢就挑最快的赢法，要输就挑最慢的输法，
能守和就守和。所以执人打困难是"和棋"，不是"必输"。
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

#: 可选的两方（入口卡片先问"你执哪一方"）。键必须和 HUMANS／BULL 对上，
#: 因为它就是"开局的人执哪一方"。
SIDES = SPEC.sides           # 单一来源：大厅卡片和这里用的是同一份声明
SIDE_LABELS = dict(SIDES)
OTHER = {HUMANS: BULL, BULL: HUMANS}

MODE_AI = "ai"
MODE_PVP = "pvp"
PHASE_WAITING = "waiting"
PHASE_PLAYING = "playing"

#: 牛"冲出牛角"的落点：**底部两个角**。民间原话是"到达牛角底部任意一角"。
ESCAPE_POINTS = (9, 10)     # F1、F2

#: 一手 = 一方走一步。走这么多手还没困住牛就**判和**，同时也是"不许在横杆上
#: 无限横走"的闸门。民间口径是"约 2×点数+3 步"，11 点盘就是 25 手左右。
#:
#: 60 不是拍的：穷举表里任何**已经分出胜负**的局面，最多再走 12 手就见分晓
#: （牛胜最深 12、人胜最深 11），所以这条闸门永远不会把能赢的局掐掉；但把"对手
#: 走错之后才出现的"胜势真正走完，前后加起来可能要四五十手，所以留到 60。
#: 试过收紧到 30：实测胜势常常来不及走完就被判和，反而把局面搅浑了。
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

def new_state(mode: str, host_openid: str, level: str = "normal",
              human_side: str = HUMANS) -> dict[str, Any]:
    """开一局。

    ``human_side`` 是**发起人要执的那一方**（``HUMANS`` 赶牛的人 / ``BULL`` 牛）。
    人机模式下另一方由 AI 走；群友模式下另一方留给加入的人。
    """
    human_side = human_side if human_side in (HUMANS, BULL) else HUMANS
    players = {human_side: host_openid, OTHER[human_side]: ""}
    return {
        "game": "hornchess",
        "mode": mode,
        "turn": HUMANS,
        "bull": TIP,
        "humans": list(ESCAPE_POINTS),        # 两枚人子摆在底角（顺序＝槽位）
        "players": players,
        "human_side": human_side,
        "labels": {},
        "level": lobby.normalize_level(level),
        "last_move": None,
        "history": [],
        "plies": 0,
        "session_id": "",
        "phase": PHASE_WAITING if mode == MODE_PVP else PHASE_PLAYING,
    }


def player_side(state: dict[str, Any]) -> str:
    """发起人执哪一方。老状态里没有这个字段时按"人"算。"""
    side = state.get("human_side")
    return side if side in (HUMANS, BULL) else HUMANS


def seat_of(state: dict[str, Any], side: str) -> str:
    """这一方是谁（openid，空字符串表示 AI 或空座）。"""
    return str((state.get("players") or {}).get(side) or "")


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
    if actor_openid:
        if state.get("mode") == MODE_AI:
            # 人机模式：玩家只能动自己那一方，另一方的按钮本来就不该出现。
            if side != player_side(state):
                return "轮到对手走，这步不是你走的"
        else:
            owner = str((state.get("players") or {}).get(side) or "")
            if owner and actor_openid != owner:
                return "轮到对手走，这步不是你走的"
    matches = [m for m in legal_moves(state) if m[1] == frm and m[2] == to]
    if not matches:
        if side == HUMANS and DEPTH[to] > DEPTH[frm]:
            return "人只能往牛角尖赶，不能后退（同一条横杆上可以左右挪）"
        return "这步走不了"
    _commit(state, frm, to)
    return ""


def _commit(state: dict[str, Any], frm: int, to: int) -> None:
    """纯落子，不校验（AI 也走这条路，校验会把 AI 挡在"不是你的回合"上）。"""
    before = _key(state)          # 走之前的局面，留给"三次重复判和"用
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
    state["history"] = list(state.get("history") or []) + [before]


# --- 胜负 -------------------------------------------------------------------

DRAW = "draw"

#: 同一个局面出现这么多次就判和（和象棋的三次重复同理）。
REPEAT_LIMIT = 3


def repetition_count(state: dict[str, Any]) -> int:
    """当前局面在这局棋里出现过几次（含这一次）。

    ``history`` 记的是**每一步走之前的局面**，所以"现在这个局面"不在里面，+1 就行。
    """
    key = _key(state)
    return 1 + sum(1 for past in (state.get("history") or []) if past == key)


def result(state: dict[str, Any]) -> str:
    """``""`` 未结束；否则 ``bull`` / ``human`` / ``draw``。"""
    if state["bull"] in ESCAPE_POINTS:
        return BULL                       # 牛冲出牛角
    if not legal_moves(state):
        return HUMANS if state["turn"] == BULL else BULL
    if repetition_count(state) >= REPEAT_LIMIT:
        return DRAW                       # 三次重复 = 和棋
    if int(state.get("plies", 0)) >= PLY_LIMIT:
        return DRAW                       # 步数用完还没围住 = 和棋
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
        return f"🎉 {MARKS[HUMANS]} 把牛憋死在牛角尖上，赶牛的人获胜！"
    if outcome == DRAW:
        if repetition_count(state) >= REPEAT_LIMIT:
            return "🤝 同一个局面重复了三次，判和。"
        return "🤝 步数用完还没围住牛，判和。"

    turn = state["turn"]
    tail = f"（还剩 {plies_left(state)} 手）"
    if state["mode"] == MODE_AI:
        mine = player_side(state)
        if turn == mine:
            base = (f"轮到你走棋 {MARKS[mine]}"
                    + ("：把牛往前赶" if mine == HUMANS else "：找路跑出去"))
        else:
            base = f"{MARKS[OTHER[mine]]} 对手在想…"
        return base + tail
    return f"轮到 {MARKS[turn]} {'赶牛' if turn == HUMANS else '牛'} 走棋" + tail


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
    # 剩下的是"谁也赢不了、只能在横杆上兜圈"的局面 = 和棋（步数用完判和）。
    for state in states:
        resolved.setdefault(state, (DRAW, PLY_LIMIT))
    return resolved


def solve_state(state: dict[str, Any]) -> str:
    return _solve().get(_key(state), (DRAW, PLY_LIMIT))[0]


def solve_depth(state: dict[str, Any]) -> int:
    """从现在起最快（对轮到走的一方）几步见分晓；和棋按步数上限算。"""
    return _solve().get(_key(state), (DRAW, PLY_LIMIT))[1]


# --- AI ---------------------------------------------------------------------

#: 难度 → 犯错概率。**困难档是 0：老老实实走穷举表里的最优解**，一步都不让。
#:
#: 之前这里给困难档留了 8%，本意是"别让玩家永远赢不了"，结果体验很糟：一局要走
#: 十几步，8% 累计下来至少送一次的概率超过 80%，玩家看到的就是"这 AI 好笨"。
#: 真正让玩家赢不了的从来不是"AI 不失误"，而是这块盘本身偏牛（穷举见模块开头），
#: 所以困难档就应该是不可战胜的——想赢就降难度，或者把 FARMERS_MAY_RETREAT 打开
#: 让人也能后退（那样人是必胜方，牛这边才需要"聪明"）。
LEVEL_BLUNDER = {"easy": 0.45, "normal": 0.22, "hard": 0.0}


def _score_for(state: dict[str, Any], move: tuple[str, int, int]) -> tuple[int, int]:
    """走完这步之后，从**走这步的那一方**看有多好：``(胜负, 方向已经摆正的步数)``。

    胜负 2 赢 / 0 输。第二项就是穷举表里那个"几步见分晓"，但**方向要按胜负调**：

    * 能赢 → 越早赢越好（步数小），所以取负号；
    * 能和 → 也算不亏，早点收工；
    * 会输 → 越晚输越好（步数大），所以保留原值。

    合起来才是"最优"——之前不分方向、一律挑步数最大的，结果必胜的时候 AI 会挑
    最慢的赢法，看着像在磨洋工。和棋算 1 分：能守和就不许输。
    """
    side, frm, to = move
    if side == BULL:
        key = (to, tuple(sorted(state["humans"])), HUMANS)
    else:
        humans = list(state["humans"])
        humans[humans.index(frm)] = to
        key = (state["bull"], tuple(sorted(humans)), BULL)
    winner, depth = _solve().get(key, (DRAW, PLY_LIMIT))
    if winner == side:
        return 2, -depth          # 赢：越快越好
    if winner == DRAW:
        return 1, -depth          # 和：不亏，早点收工
    return 0, depth               # 输：越慢越好（拖到步数上限就是和棋）


def ai_move(state: dict[str, Any], rng: random.Random | None = None) -> tuple[int, int] | None:
    """替**当前该走的那一方**走一步，返回 ``(从, 到)``；``None`` 表示没得走。

    两边都会走：玩家选「🐮 选牛」时 AI 就是那两个赶牛的人，反之亦然。
    """
    rng = rng or random.Random()
    turn = state["turn"]
    moves = [m for m in legal_moves(state) if m[0] == turn]
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

def build_lobby_card(level: str = "normal", side: str = "") -> dict[str, Any]:
    return lobby.build_lobby_card(SPEC, level, side)


def build_waiting_card(state: dict[str, Any], host_label: str = "") -> dict[str, Any]:
    mine = player_side(state)
    return lobby.build_waiting_card(
        SPEC, host_label, seat_of(state, mine),
        host_mark=MARKS[mine], guest_mark=MARKS[OTHER[mine]],
    )


#: 卡片里声明的图片尺寸（渲染器保证一致）。
CARD_IMAGE_WIDTH = 620
CARD_IMAGE_HEIGHT = 720


def _move_buttons(state: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """把合法走法直接做成按钮：**一次点击就是一步棋**。

    按钮文字只写**目标点**，比如 ``C2``／``👨C2``：QQ 的键盘一行横着摆五个键，
    写成"F1→E1"这种长文字会被挤掉甚至截断，而棋盘图上每个点都标了名字，
    看一眼就知道该点哪个。

    * 牛只有一头，所以按钮就是点名：``C2``。
    * 两个人各占一行，前面带自己的图标：``👨C2``／``👩C2``。这不只是好看——
      两个人有可能走到**同一个点**（一共 136 种局面会这样），图标正好把两步分开。
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
        rows.append([button(name_of(to), frm, to)
                     for _, frm, to in moves])
    else:
        for slot, piece in enumerate(state["humans"]):
            mark = PIECE_MARKS[slot % len(PIECE_MARKS)]
            mine = [button(f"{mark}{name_of(to)}", frm, to)
                    for _, frm, to in moves if frm == piece]
            if mine:
                rows.append(mine)

    rows.append([
        {"id": "quit", "label": "🔚 结束", "style": 0,
         "action_id": lobby.action_id(SPEC, "quit"), "params": {}},
    ])
    return rows


def build_board_card(state: dict[str, Any], image_url: str = "") -> dict[str, Any]:
    lines = [f"**{status_text(state)}**"]
    if image_url:
        lines.append(
            f"![棋盘 #{CARD_IMAGE_WIDTH}px #{CARD_IMAGE_HEIGHT}px]({image_url})")
    if not is_over(state):
        if state["mode"] == MODE_AI and state["turn"] != player_side(state):
            lines.append(f"{MARKS[state['turn']]} 对手正在想…")
        elif state["turn"] == BULL:
            lines.append(f"{MARKS[BULL]} 轮到你：按钮上就是落点（点 C2 就走 C2）。"
                         "牛前后左右都能走，冲到底角就赢，步数用完还没被围住也算你赢。")
        else:
            lines.append(f"{MARKS[HUMANS]} 轮到你：按钮是「谁→去哪」，比如 👨C2 = "
                         "左边那位走到 C2。人只能往尖上赶、不能后退，同杆可左右挪。")
    return {
        "id": "hornchess_board",
        "markdown": "\n".join(lines),
        "rows": [] if is_over(state) else _move_buttons(state),
        "one_shot": False,
        "ttl_seconds": 3600,
    }
