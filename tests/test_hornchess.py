"""牛角棋：棋盘、规则、穷举求解、AI、按钮。

棋盘照用户实拍的民间真盘画：6 行 11 点、19 条线，连线规律是"每点只连编号
±1／±2"。规则只有三条，但**每一条错一点都会把平衡彻底翻转**（人必胜／牛必胜／
无限横走），所以除了常规断言，这里还把穷举结论钉住：

* 11 点盘、人只能前进（可同杆横走）、牛前后左右自由 → **牛只要不失误就赢**
  （990 个局面里牛胜 954、人胜 36）。所以"人能赢"必须靠牛犯错，AI 的困难档
  也得留一点犯错空间，否则玩家永远赢不了。

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

def test_the_board_is_the_six_row_horn_from_the_photo():
    """用户实拍的盘：高瘦牛角，6 行 11 个点。"""
    assert hc.ROWS == (1, 2, 2, 2, 2, 2)
    assert len(hc.NAMES) == 11
    assert hc.NAMES[0] == "A1" and hc.NAMES[-1] == "F2"


def test_rows_from_tip_to_base():
    assert hc.DEPTH == (0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5)
    assert [name for name, d in zip(hc.NAMES, hc.DEPTH) if d == 0] == ["A1"]
    assert [name for name, d in zip(hc.NAMES, hc.DEPTH) if d == 5] == ["F1", "F2"]


def test_the_board_has_the_nineteen_lines_the_real_board_has():
    """左列 5 条 + 右列 5 条 + 5 道横杆 + 4 条平行长斜线 = 19。"""
    edges = {(min(a, b), max(a, b))
             for a in range(len(hc.NAMES)) for b in hc.neighbors(a)}
    assert len(edges) == 19
    # 每一行的两个点之间必须是横杆（同杆可横走的规则靠它）
    for depth in range(1, len(hc.ROWS)):
        row = [i for i, d in enumerate(hc.DEPTH) if d == depth]
        assert tuple(row) in edges


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
    assert hc.ESCAPE_POINTS == (hc.INDEX["F1"], hc.INDEX["F2"])
    assert sorted(hc.new_state(hc.MODE_AI, "U1")["humans"]) == list(hc.ESCAPE_POINTS)


# --- 规则 -------------------------------------------------------------------

def test_the_bull_may_go_backwards_but_the_farmers_may_not():
    """牛可进可退，人只能往尖上赶——这是这棋的核心不对称。"""
    bull_targets = set(hc.neighbors(hc.INDEX["D1"]))
    assert hc.INDEX["C1"] in bull_targets       # 往上（往尖，进）
    assert hc.INDEX["E1"] in bull_targets       # 往下（往底角，退），牛可以

    human_targets = set(hc.forward_targets(hc.INDEX["D1"]))
    assert hc.INDEX["C1"] in human_targets      # 人可以前进
    assert hc.INDEX["D2"] in human_targets      # 同一条横杆可以左右挪
    assert hc.INDEX["E1"] not in human_targets  # 人不能后退
    assert hc.FARMERS_MAY_RETREAT is False


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
    state = setup("A1", ("D1", "D2"), hc.HUMANS)
    refusal = hc.apply_move(state, hc.INDEX["D1"], hc.INDEX["E1"])
    assert refusal and "不能后退" in refusal


# --- 胜负 -------------------------------------------------------------------

def test_bull_wins_by_reaching_a_base_corner():
    state = setup("E1", ("B1", "B2"), hc.BULL)
    assert hc.apply_move(state, hc.INDEX["E1"], hc.INDEX["F1"]) == ""
    assert hc.result(state) == hc.BULL
    assert "冲出牛角" in hc.status_text(state)


def test_farmers_win_by_trapping_the_bull_on_the_tip():
    state = setup("A1", ("B1", "B2"), hc.BULL)
    assert hc.result(state) == hc.HUMANS
    assert "憋死" in hc.status_text(state)


def test_the_bull_wins_when_the_ply_limit_runs_out():
    """这条是"不许在横杆上无限横走"的闸门，缺了它人可以直接躺赢。"""
    state = setup("D1", ("B1", "C1"), hc.HUMANS)
    state["plies"] = hc.PLY_LIMIT
    assert hc.result(state) == hc.BULL
    assert "步数用完" in hc.status_text(state)


def test_status_text_counts_the_remaining_plies():
    state = fresh()
    assert str(hc.PLY_LIMIT) in hc.status_text(state)


# --- 穷举求解 ---------------------------------------------------------------

def test_the_solver_covers_every_position():
    table = hc._solve()
    expected = 11 * 45 * 2         # 11 个牛位置 × C(10,2) 个人位置 × 2 个走子方
    assert len(table) == expected == 990
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
    (0, (9, 10), hc.HUMANS),
    (0, (9, 10), hc.BULL),
    (0, (5, 6), hc.HUMANS),
    (3, (0, 8), hc.HUMANS),
    (9, (1, 3), hc.BULL),
    (6, (0, 1), hc.HUMANS),
])
def test_the_table_agrees_with_a_dumb_brute_force(bull, humans, turn):
    table = hc._solve()[(bull, humans, turn)][0]
    assert table == brute_force(bull, humans, turn)


def test_on_the_eleven_point_board_the_bull_wins_perfect_play():
    """把"这棋到底平衡吗"钉死：完美对弈是牛胜，人赢只能靠牛犯错。

    这条看起来像在给"人赢不了"背书，其实是**产品决策的依据**：既然 11 点盘上
    牛不失误就赢，AI 的困难档就必须留一点犯错空间（见 LEVEL_BLUNDER），
    否则玩家永远赢不了——那不是难度是堵墙。
    """
    from collections import Counter

    table = hc._solve()
    tally = Counter(winner for winner, _ in table.values())
    assert tally[hc.BULL] == 954 and tally[hc.HUMANS] == 36

    state = fresh()
    assert hc.solve_state(state) == hc.BULL
    assert len(hc.legal_moves(state)) == 3      # 开局人只有三步可走
    for _, frm, to in hc.legal_moves(state):
        probe = fresh()
        assert hc.apply_move(probe, frm, to) == ""
        assert hc.solve_state(probe) == hc.BULL


def test_hard_is_flawless_and_the_weaker_levels_are_not():
    """困难档一步都不让；轻松／普通才是留给玩家赢的口子。"""
    assert hc.LEVEL_BLUNDER["hard"] == 0.0
    assert hc.LEVEL_BLUNDER["easy"] > hc.LEVEL_BLUNDER["normal"] > 0


def test_the_eleven_point_board_is_the_one_from_the_photo_even_if_it_favours_the_bull():
    """民间三种常见尺寸全跑一遍，把"11 点盘是牛优"记在测试里，别哪天改规则改出幻觉。"""
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

    assert farmers_win(5) is True        # 3 行：太短，人随便就围住
    assert farmers_win(7) is True        # 4 行：人真能围住
    assert farmers_win(9) is False       # 5 行
    assert farmers_win(11) is False      # 6 行——就是用户实拍这块盘


# --- AI ---------------------------------------------------------------------

def test_the_hard_ai_never_loses_the_bull_when_it_can_avoid_it():
    """困难档不许犯傻：只要存在"不输"的一步，走一千次都要选它。"""
    state = setup("C1", ("B2", "C2"), hc.BULL)
    verdicts = {hc.name_of(m[2]): hc._solve()[(m[2], tuple(sorted(state["humans"])), hc.HUMANS)][0]
                for m in hc.legal_moves(state)}
    assert hc.HUMANS in verdicts.values() and hc.BULL in verdicts.values()
    safe = {name for name, verdict in verdicts.items() if verdict == hc.BULL}

    # 困难档：300 次全都走活路，一次都不许错。
    rng = random.Random(7)
    picks = [hc.name_of(hc.ai_move(setup("C1", ("B2", "C2"), hc.BULL,
                                        level="hard"), rng)[1])
             for _ in range(300)]
    assert set(picks) <= safe

    # 简单档：明显会走错。
    rng = random.Random(7)
    easy = [hc.name_of(hc.ai_move(setup("C1", ("B2", "C2"), hc.BULL,
                                       level="easy"), rng)[1])
            for _ in range(300)]
    assert any(name not in safe for name in easy)


def test_hard_never_turns_a_result_around_on_any_position():
    """全表扫一遍：困难档在 990 个局面里一步都不能把胜负走反。

    这是"AI 到底是不是最优解"最硬的证据——不是抽样，是把每一个局面都走一遍。
    """
    rng = random.Random(11)
    flipped = []
    for (bull, humans, turn), (winner, _) in hc._solve().items():
        state = hc.new_state(hc.MODE_AI, "U1", "hard")
        state["bull"], state["humans"], state["turn"] = bull, list(humans), turn
        if hc.result(state):
            continue
        hc.ai_move(state, rng)
        # 走完轮到对手，所以"对手的结论"应该还是原来那个赢家
        if hc.solve_state(state) != winner:
            flipped.append((bull, humans, turn))
    assert flipped == []


def test_hard_takes_the_fastest_win_instead_of_dawdling():
    """必胜的时候要挑最短的那步——之前一律挑步数最大的，看着像在磨洋工。

    这个局面（牛在 C1，人在 A1／B2）牛有三条赢法：马上赢、2 步后赢、4 步后赢。
    """
    state = setup("C1", ("A1", "B2"), hc.BULL, level="hard")
    options = []
    for _, frm, to in hc.legal_moves(state):
        winner, depth = hc._solve()[(to, tuple(sorted(state["humans"])), hc.HUMANS)]
        options.append((hc.name_of(to), winner, depth))
    winning = [(name, depth) for name, winner, depth in options if winner == hc.BULL]
    assert len(winning) >= 2 and len({depth for _, depth in winning}) >= 2, options

    _, chosen = hc.ai_move(state, random.Random(3))
    assert hc.solve_state(state) == hc.BULL
    assert hc.solve_depth(state) == min(depth for _, depth in winning)
    assert hc.name_of(chosen) == min(winning, key=lambda pair: pair[1])[0]


def test_the_easy_ai_sometimes_blunders():
    rng = random.Random(3)
    picks = set()
    for _ in range(200):
        probe = setup("C1", ("B2", "C2"), hc.BULL, level="easy")
        picks.add(hc.ai_move(probe, rng))
    assert len(picks) > 1        # 简单档会挑次优的那一步


def test_ai_move_changes_the_state_and_hands_the_turn_back():
    state = setup("A1", ("F1", "F2"), hc.BULL)
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


def test_the_bull_buttons_are_just_the_destination():
    """牛只有一个，按钮就是点名（C2），不带"从哪儿到哪儿"。"""
    state = setup("A1", ("F1", "F2"), hc.BULL)
    rows = hc._move_buttons(state)
    labels = [b["label"] for b in rows[0]]
    assert labels == [hc.name_of(t) for _, _, t in hc.legal_moves(state)]
    assert all("→" not in label for label in labels)


def test_the_farmer_buttons_are_the_emoji_plus_the_destination():
    """人的按钮是 👨C2 / 👩C2 这种十几个字符以内的小键。"""
    state = fresh()
    rows = hc._move_buttons(state)
    buttons = [b for row in rows[:-1] for b in row]
    for button in buttons:
        assert "→" not in button["label"]
        assert len(button["label"]) <= 4          # emoji + 两个字符的点名
    # 两个人各占一行，图标和各自的槽位对得上
    for slot, row in enumerate(rows[:-1]):
        mark = hc.PIECE_MARKS[slot]
        for button in row:
            assert button["label"].startswith(mark)
            assert button["params"]["frm"] == state["humans"][slot]


def test_two_farmers_reaching_the_same_point_still_get_different_buttons():
    """136 种局面里两枚子能走到同一个点，图标必须把这两步分开。"""
    state = setup("A1", ("C1", "D1"), hc.HUMANS
                  )
    targets = {}
    for _, _, to in hc.legal_moves(state):
        targets[to] = targets.get(to, 0) + 1
    shared = [to for to, count in targets.items() if count > 1]
    assert shared, "这个局面本来是用来复现重名的"
    buttons = [b for row in hc._move_buttons(state)[:-1] for b in row]
    labels = [b["label"] for b in buttons]
    assert len(labels) == len(set(labels)) == len(buttons)


def test_buttons_never_exceed_five_per_row():
    """QQ 键盘一行最多 5 个按钮。"""
    for bull, humans in itertools.product(range(len(hc.NAMES)),
                                          itertools.combinations(range(len(hc.NAMES)), 2)):
        if bull in humans:
            continue
        state = setup(hc.name_of(bull), tuple(hc.name_of(h) for h in humans), hc.HUMANS)
        for row in hc._move_buttons(state):
            assert len(row) <= 5


def test_the_quit_button_is_always_last_and_short():
    state = fresh()
    rows = hc._move_buttons(state)
    assert rows[-1][0]["id"] == "quit"
    assert len(rows[-1][0]["label"]) <= 6          # 「🔚 结束」，别把键盘挤宽


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


# --- 出图：参照系坐标与 emoji 素材 ------------------------------------------

def test_the_picture_uses_every_point_of_the_reference_board():
    from games import hornchess_render as hr

    assert set(hr.POINTS) == set(hc.NAMES)
    assert all(0.0 < nx < 1.0 and 0.0 < ny < 1.0
               for nx, ny in hr.POINTS.values())
    assert len(hr.OUTLINE) >= 40                 # 轮廓是沿着参照图取的曲线，不是多边形
    assert all(0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0 for nx, ny in hr.OUTLINE)


def test_every_point_lands_inside_the_canvas():
    from games import hornchess_render as hr

    for index in range(len(hc.NAMES)):
        x, y = hr.point_xy(index)
        assert 0 < x < hr.WIDTH and 0 < y < hr.HEIGHT, (hc.name_of(index), x, y)


def test_the_rendered_picture_is_the_size_the_card_declares():
    from PIL import Image
    import io

    from games import hornchess_render as hr

    data = hr.render_board(fresh())
    image = Image.open(io.BytesIO(data))
    assert image.size == (hc.CARD_IMAGE_WIDTH, hc.CARD_IMAGE_HEIGHT)


def test_the_emoji_assets_ship_with_the_plugin():
    """字体画不出彩色 emoji，只能贴图——素材缺了棋子就变回汉字，所以钉住。"""
    from PIL import Image

    from games import hornchess_render as hr

    for asset in ("cow", *hc.PIECE_ASSETS):
        path = hr.ASSET_DIR / f"{asset}.png"
        assert path.exists(), path
        with Image.open(path) as image:
            assert image.mode == "RGBA"


def test_the_board_still_renders_without_the_emoji_assets(monkeypatch, tmp_path):
    from games import hornchess_render as hr

    monkeypatch.setattr(hr, "ASSET_DIR", tmp_path / "缺素材")
    hr._emoji.cache_clear()
    try:
        assert hr.render_board(fresh())[:2] == b"\xff\xd8"      # 还是张 JPEG
    finally:
        hr._emoji.cache_clear()


def test_the_two_farmers_keep_their_own_emoji_slots():
    """👨/👩 按槽位固定，互相越过对方也不会换脸（所以 humans 不排序）。"""
    assert hc.PIECE_MARKS == ("👨", "👩")
    assert hc.PIECE_ASSETS == ("man", "woman")

    state = fresh()
    man, woman = state["humans"]
    # 👩 先往前走一步，编号比 👨 更小——如果拿排序当槽位，两个图标就换脸了
    assert hc.apply_move(state, woman, hc.INDEX["E2"]) == ""
    assert state["humans"] == [man, hc.INDEX["E2"]]
    assert state["humans"][0] > state["humans"][1]      # 故意是"乱序"的
    # 状态键照样按位置排序，求解表不受槽位顺序影响
    assert hc.solve_state(state) in (hc.BULL, hc.HUMANS)
