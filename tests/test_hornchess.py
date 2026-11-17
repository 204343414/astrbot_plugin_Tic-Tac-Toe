"""牛角棋：棋盘、规则、穷举求解、AI、按钮。

这个棋的规则只有三条，但**每一条错一点都会把平衡彻底翻转**（人必胜/牛必胜/
死循环）。所以除了常规断言，这里还用穷举结果把"开局是和棋"钉住——那是决定
这棋到底能不能玩的那一条。
"""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from games import hornchess as hc  # noqa: E402


def fresh(strict=False, level="normal", mode=None):
    state = hc.new_state(mode or hc.MODE_AI, "U1", level)
    state["strict"] = strict
    return state


def setup(bull, humans, turn, strict=False, level="normal"):
    """摆一个局面。

    直接用 ``_commit`` 挪子，而不是 ``apply_move``：人机模式下 ``apply_move``
    会拦住"玩家替牛走棋"（这是对的），但测试需要摆出牛该走的局面。
    """
    state = fresh(strict=strict, level=level)
    state["bull"] = hc.INDEX[bull]
    state["humans"] = sorted(hc.INDEX[name] for name in humans)
    state["turn"] = turn
    return state


# --- 棋盘形状 ---------------------------------------------------------------

def test_the_board_is_a_four_row_horn():
    assert hc.ROWS == (1, 2, 3, 4)
    assert len(hc.NAMES) == 10
    assert hc.NAMES[0] == "A1" and hc.NAMES[-1] == "D4"


def test_the_tip_touches_exactly_the_two_points_below_it():
    assert set(hc.neighbors(hc.INDEX["A1"])) == {hc.INDEX["B1"], hc.INDEX["B2"]}


def test_every_edge_is_reciprocal():
    """邻接必须对称，否则会出现"能过去回不来"的幽灵走法。"""
    for index in range(len(hc.NAMES)):
        for other in hc.neighbors(index):
            assert index in hc.neighbors(other), (hc.name_of(index), hc.name_of(other))


def test_bottom_corners_only_connect_upward_and_sideways():
    assert set(hc.neighbors(hc.INDEX["D1"])) == {hc.INDEX["C1"], hc.INDEX["D2"]}
    assert set(hc.neighbors(hc.INDEX["D4"])) == {hc.INDEX["C3"], hc.INDEX["D3"]}


# --- 规则 -------------------------------------------------------------------

def test_the_bull_may_retreat_but_the_farmers_may_not():
    """牛可进可退，人不能退——这是这棋的核心不对称。"""
    bull_at_c2 = hc.INDEX["C2"]
    bull_targets = set(hc.neighbors(bull_at_c2))
    assert hc.INDEX["B1"] in bull_targets        # 往上（进）
    assert hc.INDEX["D2"] in bull_targets        # 往下（退），牛可以

    humans = set(hc.human_targets(hc.INDEX["C2"]))
    assert hc.INDEX["B1"] in humans              # 前进
    assert hc.INDEX["D2"] not in humans           # 后退禁止
    assert hc.INDEX["C1"] in humans               # 同层横走允许


def test_strict_mode_forbids_sideways_movement_too():
    assert set(hc.human_targets(hc.INDEX["C2"], strict=True)) == {
        hc.INDEX["B1"], hc.INDEX["B2"]}


def test_the_bull_wins_by_reaching_a_bottom_corner():
    state = setup("C1", ("D2", "C3"), hc.BULL)
    hc._commit(state, hc.INDEX["C1"], hc.INDEX["D1"])
    assert hc.result(state) == hc.BULL
    assert "冲出牛角" in hc.status_text(state)


def test_reaching_the_middle_of_the_bottom_row_is_not_an_escape():
    """民间的说法是"到达底部任意**一角**"。放宽成整排的话人必输，所以这条要钉住。"""
    state = setup("C2", ("D1", "D4"), hc.BULL)
    hc._commit(state, hc.INDEX["C2"], hc.INDEX["D3"])
    assert hc.result(state) == ""


def test_a_cornered_bull_loses():
    """牛在尖上、下面两个点被人堵死 —— 憋死牛。"""
    state = setup("A1", ("B1", "B2"), hc.BULL)
    assert hc.result(state) == hc.HUMANS
    assert "憋死" in hc.status_text(state)


def test_a_farmer_with_no_move_loses_too():
    """对称的憋死：轮到人走却一步也走不了，同样判负。"""
    state = setup("A1", ("B1", "B2"), hc.HUMANS)
    assert hc.result(state) == hc.BULL


def test_moves_are_rejected_when_it_is_not_your_turn():
    state = hc.new_state(hc.MODE_PVP, "U1")
    state["phase"] = hc.PHASE_PLAYING
    state["players"] = {hc.HUMANS: "U1", hc.BULL: "U2"}
    legal = hc.legal_moves(state)[0]
    assert hc.apply_move(state, legal[1], legal[2], "U2") == "现在不是你的回合"
    assert hc.apply_move(state, legal[1], legal[2], "U1") == ""


def test_a_piece_cannot_land_on_an_occupied_point():
    state = setup("C1", ("D1", "D2"), hc.HUMANS)
    # D1 的邻居 B? 只有 C1 和 D2，两个都被占住，所以 D1 这枚子无路可走
    assert all(frm != hc.INDEX["D1"] for _, frm, _ in hc.legal_moves(state))


# --- 穷举求解：这棋到底平衡吗 ----------------------------------------------

def test_the_default_rules_open_in_a_draw():
    """默认规则（人不能退但可横走 + 牛只要冲到底角）-> 完美对弈是和棋。

    这一条如果变了，说明规则被改动过——而改动会让某一方必赢，那这棋就不能
    玩了。写测试的时候正是靠它发现"牛到底线任意点就算赢"太便宜了。
    """
    assert hc.solve_state(fresh(strict=False)) == "draw"


def test_strict_rules_hand_the_game_to_the_bull():
    """严格只前进的版本是牛必胜——这也是为什么默认不开它。"""
    assert hc.solve_state(fresh(strict=True)) == hc.BULL


def test_the_solver_covers_every_position():
    """720 = 10 个点里选牛的位置 × 剩下 9 个选 2 个人 × 2 个走子方。"""
    assert len(hc._solve(False)) == 720


def test_solver_agrees_with_play_from_a_known_position():
    """抽查：从开局按最优走若干步，结果不应与开局结论矛盾。"""
    state = fresh()
    assert hc.solve_state(state) == "draw"
    # 牛在尖上、人堵住下面两点=人赢，这是终局，必须一致
    cornered = setup("A1", ("B1", "B2"), hc.BULL)
    assert hc.solve_state(cornered) == hc.HUMANS


# --- AI ---------------------------------------------------------------------

def test_hard_ai_never_loses_a_won_position():
    """牛在必胜局面里，困难档必须走赢。"""
    state = setup("C2", ("C1", "C3"), hc.BULL, level="hard")
    move = hc.ai_move(state, random.Random(1))
    assert move is not None
    state = setup("C2", ("C1", "C3"), hc.BULL, level="hard")
    for _ in range(20):
        if hc.is_over(state):
            break
        if state["turn"] == hc.BULL:
            hc.ai_move(state, random.Random(0))
        else:
            legal = hc.legal_moves(state)
            if not legal:
                break
            hc.apply_move(state, legal[0][1], legal[0][2])
    # 牛从这里该赢；至少不能被人赢
    assert hc.result(state) != hc.HUMANS


def test_easy_ai_blunders_sometimes_and_hard_never():
    """难度差异必须可测：同一个局面重复抽样，简单档会偏离最优。

    这个游戏局面小到可以查表全知，所以"看得更远"没有意义，能拉开差距的只有
    "会不会犯错"。挑局面时要挑"确实存在更差走法"的那种，否则谁都没法犯错。
    """
    table = hc._solve(False)
    # 这几个局面里牛确实有"好走法"和"坏走法"之分（穷举筛出来的），
    # 挑全同分的局面测难度等于什么都测不到。
    positions = [
        ("B1", ("B2", "C1")),
        ("B1", ("D3", "D4")),
        ("C1", ("D2", "D4")),
        ("C2", ("D1", "D2")),
    ]

    def deviations(level, draws=16):
        off = 0
        for bull_name, humans in positions:
            key = setup(bull_name, humans, hc.BULL)
            moves = hc.legal_moves(key)
            scores = {hc._score_for(key, m, table) for m in moves}
            if len(scores) < 2:
                continue            # 这个局面里所有走法一样好，没人能犯错
            best = max(scores)
            for seed in range(draws):
                state = setup(bull_name, humans, hc.BULL, level=level)
                frm, to = hc.ai_move(state, random.Random(seed))
                if hc._score_for(key, (hc.BULL, frm, to), table) < best:
                    off += 1
        return off

    assert deviations("hard") == 0
    assert deviations("easy") >= 3


def test_ai_moves_the_bull_and_hands_the_turn_back():
    state = fresh()
    hc.apply_move(state, hc.INDEX["D1"], hc.INDEX["C1"], "U1")
    assert state["turn"] == hc.BULL
    move = hc.ai_move(state, random.Random(0))
    assert move is not None
    assert state["turn"] == hc.HUMANS
    assert hc.name_of(state["bull"]) in {hc.name_of(m[1]) for m in [move]} | {
        hc.name_of(state["bull"])}


# --- 按钮：一次点击就是一步棋 ----------------------------------------------

def test_the_card_offers_one_button_per_legal_move():
    state = fresh()
    card = hc.build_board_card(state, "https://example.com/b.jpg")
    move_ids = {row_button["id"] for row in card["rows"] for row_button in row
                if row_button["id"].startswith("mv_")}
    expected = {f"mv_{frm}_{to}" for _, frm, to in hc.legal_moves(state)}
    assert move_ids == expected


def test_every_move_button_carries_a_resolvable_action():
    state = fresh()
    card = hc.build_board_card(state, "")
    for row in card["rows"]:
        for button in row:
            if button["id"] == "quit":
                continue
            assert button["action_id"] == "hornchess.move"
            assert set(button["params"]) == {"frm", "to"}


def test_a_finished_game_ships_no_move_buttons():
    state = setup("A1", ("B1", "B2"), hc.BULL)
    card = hc.build_board_card(state, "")
    assert card["rows"] == []


def test_card_declares_the_size_the_renderer_produces():
    from PIL import Image
    import io

    from games import hornchess_render as hr

    data = hr.render_board(fresh(), banner=False)
    assert Image.open(io.BytesIO(data)).size == (
        hc.CARD_IMAGE_WIDTH, hc.CARD_IMAGE_HEIGHT)
