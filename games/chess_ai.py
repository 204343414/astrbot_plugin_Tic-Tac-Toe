"""把 Sunfish 接进来：局面 → 引擎 → 走法，以及"按概率犯傻"的三档难度。

设计
----
棋力只有一种：**能算多深算多深**。难度不是"更弱的算法"，而是"在最强算法
的基础上按概率故意走臭棋"。理由很实际——弱引擎的弱是很难形容的（它会把棋
下得又慢又怪），而"偶尔走一步昏招"恰好就是人类低手的表现，玩家也能看懂。

    简单：50% 概率随机乱走，引擎只给 0.12 秒
    普通：20% 概率随机乱走，引擎给 0.5 秒
    困难：不犯傻，引擎给 1.6 秒

"随机乱走"也是**从合法走法里挑**，不会走不能走的棋；只在能吃子/能升变时
多考虑一点，免得看起来像程序卡了。另外有一条硬规则：**已经算到杀棋时不犯傻**。
真能一步将死却因为掷骰子而放过，那不是"菜"，那是坏掉了。

为什么自己写适配层
------------------
Sunfish 的局面是"120 格字符板 + 走子方视角"，没有 FEN 入口（上游的 FEN 解析
在 sunfish_ui/ 里）。所以这里把 ``chess.to_fen`` 的输出翻成它的板子，并且
处理它的镜像约定：黑方走棋时引擎看到的是旋转过的棋盘，返回的走法要再转回来。
"""
from __future__ import annotations

import random
import time
from typing import Any

from . import chess as ch
from . import sunfish

#: 难度 → (犯傻概率, 思考秒数)。犯傻概率是"直接从合法走法里挑一个"的比例。
LEVEL_BLUNDER = {"easy": 0.50, "normal": 0.20, "hard": 0.0}
LEVEL_SECONDS = {"easy": 0.12, "normal": 0.5, "hard": 1.6}


def _board_from_fen(fen: str) -> str:
    """FEN 的行列描述 → sunfish 的 120 格棋盘。

    布局是 21 个空格做上边距，每行 10 列（8 格 + 2 个边框），行尾放换行。
    这和上游 ``sunfish_ui/uci.py::from_fen`` 的做法一致，只是这里不依赖那份
    代码——它不在 vendored 文件里。
    """
    placement = fen.split()[0]
    rows = []
    for chunk in placement.split("/"):
        row = ""
        for char in chunk:
            row += "." * int(char) if char.isdigit() else char
        rows.append(row)
    board = list(21 * " " + "  ".join(rows) + 21 * " ")
    board[9::10] = ["\n"] * 12
    return "".join(board)


def position_from_fen(fen: str) -> tuple[sunfish.Position, bool]:
    """返回 ``(引擎局面, 是否被旋转过)``。

    Sunfish 永远从"该走的一方"的视角看棋盘：黑方走棋时上游会调用
    ``pos.rotate()``。旋转过的局面返回的走法坐标也要镜像回去，这个布尔值
    就是为了记住这件事——忘了镜像会让 AI 走出``e2e4``这种"对面"的棋。
    """
    parts = fen.split()
    placement, color = parts[0], parts[1]
    castling = parts[2] if len(parts) > 2 else "-"
    enpas = parts[3] if len(parts) > 3 else "-"
    board = _board_from_fen(placement)
    wc = ("Q" in castling, "K" in castling)
    bc = ("k" in castling, "q" in castling)
    ep = sunfish.parse(enpas) if enpas != "-" else 0
    score = sum(sunfish.pst[c][i] for i, c in enumerate(board) if c.isupper())
    score -= sum(sunfish.pst[c.upper()][119 - i]
                 for i, c in enumerate(board) if c.islower())
    pos = sunfish.Position(board, score, wc, bc, ep, 0)
    if color == "b":
        return pos.rotate(), True
    return pos, False


def _engine_uci(state: dict[str, Any], seconds: float) -> tuple[str, int] | None:
    """跑一次搜索，返回 ``(uci 走法, 分数)``。失败返回 None。"""
    pos, rotated = position_from_fen(ch.to_fen(state))
    searcher = sunfish.Searcher()
    # sunfish 内部用的是 time.time()，不是 monotonic。混用会让它一进搜索就
    # 认为"时间到"，然后立刻返回第一步——症状是 AI 出手极快且乱走。
    now = time.time()
    searcher.deadline = now + seconds
    searcher.soft = now + seconds * 0.8

    best: str | None = None
    best_score = 0
    # 上游 UCI 循环的逻辑：只信"完整跑完的那一层"给出的走法，中途被打断的
    # 那一层可能停在一个荒谬的窗口里，走法不可靠。
    candidate: str | None = None
    settled_depth = 1
    try:
        for depth, gamma, score, move in searcher.search([pos]):
            if depth > settled_depth:
                best, settled_depth = candidate or best, depth
            if score >= gamma:
                if move is None:
                    break
                i, j = move.i, move.j
                if rotated:
                    i, j = 119 - i, 119 - j
                candidate = sunfish.render(i) + sunfish.render(j) + move.prom.lower()
                best_score = score
    except sunfish.Stop:
        best = candidate or best
    except Exception:
        return None
    final = best or candidate
    if not final:
        return None
    return final, best_score


def _to_move(state: dict[str, Any], uci: str) -> ch.Move | None:
    """把引擎给的 UCI 串对回我们自己的走法对象。

    只认我们自己规则引擎判合法的走法：**引擎和规则引擎任何一处不一致，都以
    规则为准**。宁可这步棋不算，也不要发出一张玩家看不懂的棋盘。
    """
    parsed = ch.parse_move(uci)
    if parsed is None:
        return None
    frm, to, promo = parsed
    return ch.find_move(state, frm, to, promo)


def _blunder_move(state: dict[str, Any], rng: random.Random) -> ch.Move | None:
    """从合法走法里挑一步"人会下的臭棋"。

    纯随机太像程序（会来回挪同一只兵），所以吃子和升变的权重高一点——低手
    确实爱贪吃。这一步只是"看起来像人"，不影响它坏。
    """
    moves = ch.legal_for_state(state)
    if not moves:
        return None
    weighted: list[ch.Move] = []
    for move in moves:
        weight = 1
        if state["board"][move.to]:
            weight = 3
        if move.promo:
            weight = 4
        weighted.extend([move] * weight)
    return rng.choice(weighted)


def choose_move(state: dict[str, Any], rng: random.Random | None = None) -> ch.Move | None:
    """按难度挑一步棋。这是国际象棋唯一的 AI 入口。"""
    rng = rng or random.Random()
    level = str(state.get("level") or "normal")
    if level not in LEVEL_BLUNDER:
        level = "normal"
    moves = ch.legal_for_state(state)
    if not moves:
        return None
    if len(moves) == 1:
        return moves[0]

    try:
        reply = _engine_uci(state, LEVEL_SECONDS[level])
    except Exception:
        # 引擎这一层任何问题都不该让对局停下来：这里能看到的只有"没走成"，
        # 那就当它没给建议，走随机合法走法。
        reply = None
    engine_move = _to_move(state, reply[0]) if reply else None
    if engine_move is None:
        # 引擎抽风（导入失败、时间被打断、坐标对不上）时不能把对局卡住。
        return rng.choice(moves)

    # 已经算到杀棋就别犯傻了：能一步将死却因为掷骰子放过，那是坏掉，不是菜。
    if reply and abs(reply[1]) >= sunfish.MATE_LOWER:
        return engine_move

    blunder = LEVEL_BLUNDER[level]
    if blunder and rng.random() < blunder:
        fallback = _blunder_move(state, rng)
        if fallback is not None and fallback != engine_move:
            return fallback
    return engine_move


def describe() -> str:
    """给日志/自检用的一句话。"""
    return (f"Sunfish {sunfish.version}："
            f"简单 {LEVEL_BLUNDER['easy']:.0%} 犯傻/{LEVEL_SECONDS['easy']}s，"
            f"普通 {LEVEL_BLUNDER['normal']:.0%} 犯傻/{LEVEL_SECONDS['normal']}s，"
            f"困难 0%/{LEVEL_SECONDS['hard']}s")
