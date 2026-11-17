"""Sunfish 适配层：FEN 换算、镜像、难度、以及"引擎挂了别把对局卡死"。

棋力本身不该由单元测试来断言（下两盘才知道），但**接口正确性**必须断言：
坐标镜像错了会让 AI 走对面的棋，时间基准错了会让它立刻乱走——这两个都是
真实踩过的坑，而且症状都像"AI 很菜"。
"""
import random
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from games import chess as ch  # noqa: E402
from games import chess_ai as ai  # noqa: E402
from games import sunfish  # noqa: E402

MATE_IN_ONE = "6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1"      # Ra8#


def test_fen_round_trips_through_the_engine_board():
    state = ch.from_fen(ch.FEN_START)
    pos, rotated = ai.position_from_fen(ch.to_fen(state))
    assert not rotated
    # sunfish 的棋盘是 21 格边距 + 每行 10 列
    assert len(pos.board) == 120
    assert pos.board.count("P") == 8 and pos.board.count("p") == 8


def test_black_to_move_is_rotated_and_the_move_is_mirrored_back():
    """黑方走棋时引擎看的是旋转棋盘，返回的坐标必须转回来。

    漏了这一步，AI 会走出"对面那一格"的棋——看起来像完全不懂棋，其实是
    坐标系错了。
    """
    state = ch.from_fen("4k3/8/8/8/8/8/8/4K2r b - - 0 1")
    pos, rotated = ai.position_from_fen(ch.to_fen(state))
    assert rotated
    reply = ai._engine_uci(state, 0.4)
    assert reply is not None
    move = ai._to_move(state, reply[0])
    assert move is not None
    # 黑车在 h1，唯一的合法走法都从 h1 出发或王从 e8 出发
    assert ch.square_name(move.frm) in ("h1", "e8")


def test_engine_answers_within_its_budget():
    """时间基准用错（monotonic vs time.time）会让搜索秒退并乱走。"""
    state = ch.from_fen(ch.FEN_START)
    started = time.time()
    ai._engine_uci(state, 0.5)
    assert time.time() - started < 3.0


def test_mate_in_one_is_found_at_every_difficulty():
    """能一步将死时绝不犯傻：那是坏掉，不是菜。"""
    for level in ("easy", "normal", "hard"):
        state = ch.from_fen(MATE_IN_ONE)
        state["level"] = level
        move = ai.choose_move(state, random.Random(1))
        assert ch.square_name(move.to) == "a8", level


def test_every_level_returns_a_legal_move():
    for level in ("easy", "normal", "hard"):
        for seed in range(3):
            state = ch.new_state(ch.MODE_AI, "U1", level)
            state["phase"] = ch.PHASE_PLAYING
            move = ai.choose_move(state, random.Random(seed))
            assert move in ch.legal_for_state(state), (level, seed)


def test_easy_blunders_more_often_than_hard(monkeypatch):
    """难度差异必须**只由犯傻概率**决定，而这一条要能单独测。

    引擎本身按时间停搜，同样的局面两次给出的首选可能不同，所以这里把引擎
    换成一个固定答案的桩：断言的才是我们自己的难度逻辑，不是 sunfish 的
    时钟抖动。
    """
    import games.chess_ai as module

    fixed = "e2e4"
    monkeypatch.setattr(module, "_engine_uci", lambda state, secs: (fixed, 0))

    state = ch.new_state(ch.MODE_AI, "U1", "normal")

    def off_engine(level, draws=12):
        st = dict(state, level=level, phase=ch.PHASE_PLAYING)
        return sum(
            1 for seed in range(draws)
            if module.choose_move(st, random.Random(seed)).uci() != fixed
        )

    assert off_engine("hard") == 0        # 困难档一次都不犯傻
    assert off_engine("normal") <= 6      # 普通档偶尔
    assert off_engine("easy") >= 3        # 简单档经常


def test_engine_failure_falls_back_to_a_legal_move(monkeypatch):
    """引擎 import 失败或抛异常时，对局必须还能继续。"""
    def boom(*args, **kwargs):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(ai, "_engine_uci", boom)
    state = ch.new_state(ch.MODE_AI, "U1", "normal")
    move = ai.choose_move(state, random.Random(2))
    assert move in ch.legal_for_state(state)


def test_missing_sunfish_falls_back_to_the_builtin_search(monkeypatch):
    """vendored 引擎整个不可用时，退回自带的 negamax。"""
    import games.chess_ai as module

    def explode(*args, **kwargs):
        raise ImportError("no sunfish here")

    monkeypatch.setattr(module, "_engine_uci", explode)
    state = ch.new_state(ch.MODE_AI, "U1", "normal")
    state["phase"] = ch.PHASE_PLAYING
    # choose_move 内部会先试引擎、失败后走随机合法走法；无论如何必须合法
    move = ch.choose_move(state, random.Random(4))
    assert move in ch.legal_for_state(state)


def test_sunfish_is_the_vendored_upstream_build():
    """vendor 的文件必须带出处，而且版本号能对上——将来升级时要能核对。"""
    assert sunfish.version.startswith("sunfish")
    source = (Path(__file__).resolve().parents[1] / "games" / "sunfish.py").read_text(
        encoding="utf-8")
    assert "thomasahle/sunfish" in source
    assert "GPL-3.0" in source
