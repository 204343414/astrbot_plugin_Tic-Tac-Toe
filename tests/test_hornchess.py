"""牛角棋：棋盘、规则、穷举求解、AI、按钮。

这棋规则只有三条，但**每一条错一点都会把平衡彻底翻转**（人必胜／牛必胜／无限
横走）。所以除了常规断言，这里还把穷举结论钉住：

* 用民间真盘（11 点 6 行）在"只能前进"的旧规则下，牛必胜；
* 加上"同一横杆可左右挪"，11 点盘仍然是牛必胜（穷举验证）；
* **4 行 7 点** 恰好是人真能困死牛、牛也真能跑的那一档 → 所以用 7 点盘。

另外独立实现了一套纯暴力解（``brute_force``）跟生产代码的逆向分析对拍，防止
表算错却自圆其说。
"""
import itertools
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from games import hornchess as hc  # noqa: E402


def fresh(level="normal", mode=None):
    return hc.new_state(mode or hc.MODE_AI, "U1", level)


def setup(bull, humans, turn, level="normal"):
    """摆一个局面。

    直接改字段而不是走 ``apply_move``：人机模式下 ``apply_move`` 会拦住
    "玩家替牛走棋"（这是对的），但测试需要摆出牛该走的局面。
    """
    state = fresh(level=level)
    state["bull"] = hc.INDEX[bull]
    state["humans"] = sorted(hc.INDEX[name] for name in humans)
    state["turn"] = turn
    return state


# --- 棋盘形状 ---------------------------------------------------------------

def test_the_board_is_a_four_row_horn():
    assert hc.ROWS == (1, 2, 2, 2)
    assert len(hc.NAMES) == 7
    assert hc.NAMES[0] == "A1" and hc.NAMES[-1] == "D2"


def test_rows_from_tip_to_base():
    assert hc.DEPTH == (0, 1, 1, 2, 2, 3, 3)
    assert [name for name, d in zip(hc.NAMES, hc.DEPTH) if d == 0] == ["A1"]
    assert [name for name, d in zip(hc.NAMES, hc.DEPTH) if d == 3] == ["D1", "D2"]


def test_neighbours_are_exactly_plus_or_minus_one_and_two():
    """民间记谱就这一条：每个点只连编号 ±1／±2 的点。"""
    for index in range(len(hc.NAMES)):
        expected = {j for j in (index - 2, index - 1, index + 1, index + 2)
                    if 0 <= j < len(hc.NAMES)}
        assert set(hc.neighbors(index)) == expected


def test_every_edge_is_reciprocal():
    """邻接必须对称，否则会出现"能过去回不来"的幽灵走法。"""
    for index in range(len(hc.NAMES)):
        for other in hc.neighbors(index):
            assert index in hc.neighbors(other), (hc.name_of(index), hc.name_of(other))


def test_the_tip_touches_exactly_the_two_points_below_it():
    assert set(hc.neighbors(hc.INDEX["A1"])) == {hc.INDEX["B1"], hc.INDEX["B2"]}


def test_the_base_corners_are_the_escape_points():
    assert hc.ESCAPE_POINTS == (hc.INDEX["D1"], hc.INDEX["D2"])


# --- 规则 -------------------------------------------------------------------

def test_the_bull_may_go_backwards_but_the_farmers_may_not():
    """牛可进可退，人只能往尖上赶——这是这棋的核心不对称。"""
    bull_targets = set(hc.neighbors(hc.INDEX["C1"]))
    assert hc.INDEX["B1"] in bull_targets       # 往上（往尖，进）
    assert hc.INDEX["D1"] in bull_targets       # 往下（往底角，退），牛可以

    human_targets = set(hc.forward_targets(hc.INDEX["C1"]))
    assert hc.INDEX["B1"] in human_targets      # 人可以前进
    assert hc.INDEX["C2"] in human_targets      # 同一条横杆可以左右挪
    assert hc.INDEX["D1"] not in human_targets  # 人不能后退


def test_farmers_may_shift_sideways_along_a_crossbar():
    """用户原话："不前进就可以横向一直反复移动"——横走必须合法且可逆。"""
    b1, b2 = hc.INDEX["B1"], hc.INDEX["B2"]
    assert b2 in hc.forward_targets(b1)
    assert b1 in hc.forward_targets(b2)
    assert hc.is_lateral(b1, b2) and hc.is_lateral(b2, b1)
    assert not hc.is_lateral(b1, hc.INDEX["A1"])

    # 底角之间也能横走（D1↔D2 是同一个横杆）
    assert hc.INDEX["D2"] in hc.forward_targets(hc.INDEX["D1"])



def test_lateral_shuttling_is_allowed_by_the_rules():
    """横着挪过去、再横着挪回来，规则都得认（这是用户点出来的玩法）。"""
    c1, c2 = hc.INDEX["C1"], hc.INDEX["C2"]
    state = setup("A1", ("B1", "C2"), hc.HUMANS)
    assert hc.apply_move(state, c2, c1) == ""
    bull_move = [m for m in hc.legal_moves(state) if m[0] == hc.BULL][0]
    assert hc.apply_move(state, bull_move[1], bull_move[2]) == ""
    assert hc.apply_move(state, c1, c2) == ""


def test_farmers_may_not_move_down():
    state = setup("A1", ("C1", "C2"), hc.HUMANS)
    refusal = hc.apply_move(state, hc.INDEX["C1"], hc.INDEX["D1"])
    assert refusal and "不能后退" in refusal


# --- 胜负 -------------------------------------------------------------------

def test_bull_wins_by_reaching_a_base_corner():
    state = setup("C1", ("B1", "B2"), hc.BULL)
    assert hc.apply_move(state, hc.INDEX["C1"], hc.INDEX["D1"]) == ""
    assert hc.result(state) == hc.BULL
    assert "冲出牛角" in hc.status_text(state)


def test_farmers_win_by_trapping_the_bull_on_the_tip():
    state = setup("A1", ("B1", "B2"), hc.BULL)
    assert hc.result(state) == hc.HUMANS
    assert "憋死" in hc.status_text(state)


def test_the_bull_wins_when_the_ply_limit_runs_out():
    """这条是"不许在横杆上无限横走"的闸门，缺了它人可以直接躺赢。"""
    state = setup("C1", ("B1", "C1"), hc.HUMANS)
    state["plies"] = hc.PLY_LIMIT
    assert hc.result(state) == hc.BULL
    assert "步数用完" in hc.status_text(state)


def test_status_text_counts_the_remaining_plies():
    state = fresh()
    assert str(hc.PLY_LIMIT) in hc.status_text(state)


# --- 穷举求解 ---------------------------------------------------------------

def test_the_solver_covers_every_position():
    table = hc._solve()
    expected = 7 * 15 * 2          # 7 个牛位置 × C(6,2) 个人位置 × 2 个走子方
    assert len(table) == expected == 210
    assert {winner for winner, _ in table.values()} <= {hc.HUMANS, hc.BULL}


def brute_force(bull, humans, turn, limit=hc.PLY_LIMIT):
    """独立实现：纯递归 + 步数上限，跟 ``_solve()`` 对拍用。"""
    from functools import lru_cache

    @lru_cache(maxsize=None)
    def walk(bull, humans, turn, left):
        if bull in hc.ESCAPE_POINTS:
            return hc.BULL
        if left <= 0:
            return hc.BULL
        occupied = {bull, *humans}
        if turn == hc.BULL:
            targets = [j for j in hc.neighbors(bull) if j not in occupied]
            if not targets:
                return hc.HUMANS
            if any(walk(j, humans, hc.HUMANS, left - 1) == hc.BULL for j in targets):
                return hc.BULL
            return hc.HUMANS
        moves = []
        for piece in humans:
            for j in hc.forward_targets(piece):
                if j in occupied:
                    continue
                updated = list(humans)
                updated[updated.index(piece)] = j
                moves.append(tuple(sorted(updated)))
        if not moves:
            return hc.BULL
        if any(walk(bull, hs, hc.BULL, left - 1) == hc.HUMANS for hs in moves):
            return hc.HUMANS
        return hc.BULL

    return walk(bull, humans, turn, limit)


@pytest.mark.parametrize("bull,humans,turn", [
    (0, (5, 6), hc.HUMANS),
    (0, (5, 6), hc.BULL),
    (0, (3, 4), hc.HUMANS),
    (2, (0, 5), hc.HUMANS),
    (6, (1, 3), hc.BULL),
    (4, (0, 1), hc.HUMANS),
])
def test_the_table_agrees_with_a_dumb_brute_force(bull, humans, turn):
    table = hc._solve()[(bull, humans, turn)][0]
    assert table == brute_force(bull, humans, turn)


def test_the_opening_is_a_win_for_the_farmers_but_they_must_play_it_right():
    """开局人必胜——但三手里只有两手是对的，走错就轮到牛赢。"""
    state = fresh()
    assert hc.solve_state(state) == hc.HUMANS
    assert hc.solve_depth(state) == 11          # 最快 11 手困死

    assert len(hc.legal_moves(state)) == 3
    verdicts = []
    for _, frm, to in hc.legal_moves(state):
        probe = fresh()
        assert hc.apply_move(probe, frm, to) == ""
        verdicts.append(hc.solve_state(probe))
    assert verdicts.count(hc.HUMANS) == 2
    assert verdicts.count(hc.BULL) == 1


def test_the_four_row_board_is_the_only_size_a_normal_human_can_win_on():
    """把"为什么是 7 点"钉住：加上横走以后，9／11 点盘照样是牛必胜。"""
    def build(count):
        depth = [0] + [1 + (i - 1) // 2 for i in range(1, count)]
        neighbours = {i: tuple(j for j in (i - 2, i - 1, i + 1, i + 2)
                                if 0 <= j < count) for i in range(count)}
        return depth, neighbours

    def farmers_win(count):
        from functools import lru_cache
        depth, neighbours = build(count)
        base = (count - 2, count - 1)
        limit = 3 * count + 6

        @lru_cache(maxsize=None)
        def walk(bull, humans, turn, left):
            if bull in base or left <= 0:
                return False
            occupied = {bull, *humans}
            if turn == hc.BULL:
                targets = [j for j in neighbours[bull] if j not in occupied]
                if not targets:
                    return True
                return all(walk(j, humans, hc.HUMANS, left - 1) for j in targets)
            moves = []
            for piece in humans:
                for j in neighbours[piece]:
                    if j in occupied or depth[j] > depth[piece]:
                        continue
                    updated = list(humans)
                    updated[updated.index(piece)] = j
                    moves.append(tuple(sorted(updated)))
            if not moves:
                return False
            return any(walk(bull, hs, hc.BULL, left - 1) for hs in moves)

        return walk(0, base, hc.HUMANS, limit)

    assert farmers_win(7) is True       # 本项目用的 4 行盘
    assert farmers_win(9) is False       # 5 行
    assert farmers_win(11) is False      # 6 行（民间标准盘）


# --- AI ---------------------------------------------------------------------

def test_the_hard_ai_never_loses_the_bull_when_it_can_avoid_it():
    """困难档不许犯傻：只要存在"不输"的一步，走一千次都要选它。"""
    rng = random.Random(7)
    # 这个局面牛有两步：B1 会送死，D1 才活（穷举表里查得到）。
    state = setup("C1", ("B2", "C2"), hc.BULL, level="hard")
    verdicts = {hc.name_of(m[2]): hc._solve()[(m[2], tuple(sorted(state["humans"])), hc.HUMANS)][0]
                for m in hc.legal_moves(state)}
    assert verdicts == {"B1": hc.HUMANS, "D1": hc.BULL}
    for _ in range(200):
        probe = setup("C1", ("B2", "C2"), hc.BULL, level="hard")
        frm, to = hc.ai_move(probe, rng)
        assert hc.name_of(to) == "D1"


def test_the_easy_ai_sometimes_blunders():
    rng = random.Random(3)
    picks = set()
    for _ in range(200):
        probe = setup("C1", ("B2", "C2"), hc.BULL, level="easy")
        picks.add(hc.ai_move(probe, rng))
    assert len(picks) > 1        # 简单档会挑次优的那一步


def test_ai_move_changes_the_state_and_hands_the_turn_back():
    state = setup("A1", ("D1", "D2"), hc.BULL)
    frm, to = hc.ai_move(state, random.Random(1))
    assert state["bull"] == to and frm == hc.INDEX["A1"]
    assert state["turn"] == hc.HUMANS
    assert state["last_move"] == {"side": hc.BULL, "frm": frm, "to": to}


def test_ai_move_returns_none_when_the_bull_is_stuck():
    state = setup("A1", ("B1", "B2"), hc.BULL)
    assert hc.ai_move(state) is None


# --- 按钮 -------------------------------------------------------------------

def test_farmer_buttons_cover_every_legal_move_once():
    state = fresh()
    rows = hc._move_buttons(state)
    buttons = [b for row in rows for b in row]
    moves = [(b["params"]["frm"], b["params"]["to"])
             for b in buttons if b["id"].startswith("mv_")]
    assert sorted(moves) == sorted((frm, to) for _, frm, to in hc.legal_moves(state))
    assert len(set(moves)) == len(moves)


def test_buttons_never_exceed_five_per_row():
    """QQ 键盘一行最多 5 个按钮。"""
    for bull, humans in itertools.product(range(7), itertools.combinations(range(7), 2)):
        if bull in humans:
            continue
        state = setup(hc.name_of(bull), tuple(hc.name_of(h) for h in humans), hc.HUMANS)
        for row in hc._move_buttons(state):
            assert len(row) <= 5


def test_the_quit_button_is_always_last():
    state = fresh()
    rows = hc._move_buttons(state)
    assert rows[-1][0]["id"] == "quit"


def test_board_card_declares_the_same_size_as_the_renderer():
    from games import hornchess_render as hr

    state = fresh()
    card = hc.build_board_card(state, "https://example.invalid/a.jpg")
    assert f"#{hc.CARD_IMAGE_WIDTH}px #{hc.CARD_IMAGE_HEIGHT}px" in card["markdown"]
    assert (hr.WIDTH, hr.HEIGHT) == (hc.CARD_IMAGE_WIDTH, hc.CARD_IMAGE_HEIGHT)


def test_board_card_drops_the_buttons_when_the_game_is_over():
    state = setup("A1", ("B1", "B2"), hc.BULL)
    assert hc.is_over(state)
    assert hc.build_board_card(state, "https://example.invalid/a.jpg")["rows"] == []
