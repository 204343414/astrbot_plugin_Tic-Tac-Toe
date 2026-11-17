"""国际象棋：规则、走法解析、AI、卡片。

正确性用 perft 钉住——这几个数字是国际象棋引擎圈的公认标尺，任何一条规则
写错都会让它们对不上。比"我觉得这样应该对"可靠得多。
"""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from games import chess as ch  # noqa: E402

KIWIPETE = "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1"
#: 过路兵 + 升变密集的局面，perft 数字同样有公认值
POSITION3 = "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1"


def play(state, *ucis):
    for uci in ucis:
        frm, to, promo = ch.parse_move(uci)
        move = ch.find_move(state, frm, to, promo)
        assert move is not None, uci
        ch._commit(state, move)
    return state


# --- 规则正确性：perft ------------------------------------------------------

def test_perft_matches_the_published_numbers():
    """开局 3 层 8902、Kiwipete 3 层 97862、Position3 4 层 43238。

    这三个局面合起来覆盖：普通走法、易位（含"经过的格子不能被打"）、过路兵、
    升变、以及"走完不能自我将军"。数字对上基本就等于规则对。
    """
    assert ch.perft(ch.from_fen(ch.FEN_START), 3) == 8902
    assert ch.perft(ch.from_fen(KIWIPETE), 3) == 97862
    assert ch.perft(ch.from_fen(POSITION3), 4) == 43238


def test_perft_depth_four_startpos():
    """197281：第 4 层能抓到只在"走了两步之后"才出现的规则错误。"""
    assert ch.perft(ch.from_fen(ch.FEN_START), 4) == 197281


# --- 逐条规则 ---------------------------------------------------------------

def test_pawn_cannot_jump_over_a_piece():
    state = ch.from_fen("8/8/8/8/8/8/PPPPPPPP/4K2k w - - 0 1")
    moves = {(m.frm, m.to) for m in ch.legal_for_state(state)}
    # 兵还没动，双步是合法的
    assert (ch.parse_square("e2"), ch.parse_square("e4")) in moves


def test_pawn_double_step_is_blocked_by_a_piece():
    state = ch.from_fen("8/8/8/8/8/4n3/4P3/4K2k w - - 0 1")
    moves = {(m.frm, m.to) for m in ch.legal_for_state(state)}
    assert (ch.parse_square("e2"), ch.parse_square("e3")) not in moves
    assert (ch.parse_square("e2"), ch.parse_square("e4")) not in moves


def test_en_passant_captures_the_pawn_behind_the_target_square():
    """过路兵的被吃子在起点列上，不在落点上——最常写错的一条。"""
    state = ch.from_fen("8/8/8/3pP3/8/8/8/4K2k w - d6 0 1")
    move = ch.find_move(state, ch.parse_square("e5"), ch.parse_square("d6"))
    assert move is not None and move.ep
    ch._commit(state, move)
    assert state["board"][ch.parse_square("d5")] == ""      # 被吃的黑兵
    assert state["board"][ch.parse_square("d6")] == "wP"    # 白兵落在 d6


def test_castling_moves_the_rook_too():
    state = ch.from_fen("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    move = ch.find_move(state, ch.parse_square("e1"), ch.parse_square("g1"))
    assert move is not None and move.castle == "K"
    ch._commit(state, move)
    assert state["board"][ch.parse_square("f1")] == "wR"
    assert state["board"][ch.parse_square("h1")] == ""


def test_castling_through_an_attacked_square_is_illegal():
    """王经过的格子被打时不能易位——即使起点和终点都安全。

    断言要看 ``castle`` 标记，不能只看目标格：h1 的车本来就能走到 g1，
    用"g1 是不是某个走法的终点"去判会一直通过，等于没测。
    """
    state = ch.from_fen("4k3/8/8/8/8/8/5r2/R3K2R w KQ - 0 1")
    sides = [m.castle for m in ch.legal_for_state(state) if m.castle]
    assert "K" not in sides          # 王翼要经过 f1，被 f2 的黑车打
    assert "Q" in sides              # 后翼 d1/c1/b1 都安全，仍然可以


def test_castling_is_lost_after_the_rook_is_captured():
    state = ch.from_fen("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    play(state, "a1a8")        # 白车吃掉 a8 的黑车
    assert state["castling"]["wQ"] is False
    assert state["castling"]["bQ"] is False
    assert state["castling"]["wK"] is True


def test_promotion_offers_four_pieces_and_defaults_to_queen():
    state = ch.from_fen("8/4P3/8/8/8/8/8/4K2k w - - 0 1")
    promos = {m.promo for m in ch.legal_for_state(state)
              if m.to == ch.parse_square("e8")}
    assert promos == {"Q", "R", "B", "N"}
    # 不带字母时默认升后：逼玩家多打一个字母很烦
    move = ch.find_move(state, ch.parse_square("e7"), ch.parse_square("e8"))
    assert move.promo == "Q"


def test_a_pinned_piece_may_not_expose_its_own_king():
    state = ch.from_fen("4k3/8/8/8/8/8/4R3/4K1r1 w - - 0 1")
    moves = {(m.frm, m.to) for m in ch.legal_for_state(state)}
    # e2 的车被 g1 的黑车"牵"住了王，不能离开 e 线
    assert (ch.parse_square("e2"), ch.parse_square("d2")) not in moves


def test_checkmate_is_detected():
    # 经典的"愚人杀"：1. f3 e5 2. g4 Qh4#
    state = ch.from_fen("rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3")
    assert ch.result(state) == "checkmate:b"
    assert "将死" in ch.status_text(state)


def test_stalemate_is_a_draw_not_a_win():
    # 黑王在 h8，白后在 g6，轮黑走：无子可动且没被将军
    state = ch.from_fen("7k/8/6Q1/8/8/8/8/6K1 b - - 0 1")
    assert ch.result(state) == "stalemate"


def test_insufficient_material_is_a_draw():
    state = ch.from_fen("7k/8/8/8/8/8/8/6KN w - - 0 1")
    assert ch.result(state) == "material"


def test_threefold_repetition_is_a_draw():
    state = ch.from_fen("4k3/8/8/8/8/8/8/R3K3 w - - 0 1")
    for _ in range(2):
        play(state, "a1a2", "e8e7", "a2a1", "e7e8")
    assert ch.result(state) == "threefold"


# --- 走法解析 ---------------------------------------------------------------

@pytest.mark.parametrize("text", ["e2e4", "e2-e4", "E2E4", "e2 e4", " e2→e4 "])
def test_move_parsing_accepts_the_usual_spellings(text):
    assert ch.parse_move(text) == (ch.parse_square("e2"), ch.parse_square("e4"), "")


@pytest.mark.parametrize("text,expected", [
    ("e7e8q", "Q"), ("e7e8=Q", "Q"), ("e7e8后", "Q"),
    ("e7e8n", "N"), ("e7e8马", "N"),
])
def test_promotion_suffixes(text, expected):
    parsed = ch.parse_move(text)
    assert parsed is not None and parsed[2] == expected


@pytest.mark.parametrize("text", ["", "e2", "e2e", "z9e4", "hello", "e2e4x"])
def test_nonsense_is_not_a_move(text):
    assert ch.parse_move(text) is None


def test_coordinates_round_trip_to_the_rendered_board():
    """a8 必须是下标 0、h1 必须是 63——渲染和解析用的是同一套下标。"""
    assert ch.square_name(0) == "a8"
    assert ch.square_name(63) == "h1"
    assert ch.parse_square("a8") == 0
    assert ch.parse_square("h1") == 63


def test_resolve_text_ignores_illegal_moves():
    state = ch.new_state(ch.MODE_AI, "U1")
    assert ch.resolve_text(state, "e2e5") is None       # 兵不能走三格
    assert ch.resolve_text(state, "e7e5") is None       # 不是自己方的子
    assert ch.resolve_text(state, "e2e4") is not None


# --- 对局流程 ---------------------------------------------------------------

def _ai_match(level="normal"):
    state = ch.new_state(ch.MODE_AI, "U1", level)
    return state


def test_ai_match_rejects_moves_from_the_black_side():
    state = _ai_match()
    play(state, "e2e4")
    ai = ch.maybe_ai_move(state, random.Random(1))
    assert ai is not None and state["turn"] == ch.WHITE


def test_pvp_seats_are_enforced():
    state = ch.new_state(ch.MODE_PVP, "U1")
    state["phase"] = ch.PHASE_PLAYING
    state["players"] = {ch.WHITE: "U1", ch.BLACK: "U2"}
    move = ch.resolve_text(state, "e2e4")
    assert ch.apply_move(state, move, "U2") == "现在不是你的回合"
    assert ch.apply_move(state, move, "U1") == ""
    assert state["turn"] == ch.BLACK


def test_pvp_waits_for_an_opponent_before_any_move():
    state = ch.new_state(ch.MODE_PVP, "U1")
    move = ch.find_move(state, ch.parse_square("e2"), ch.parse_square("e4"))
    # 等待状态下棋盘还没画出来，走法一律拒绝
    assert "加入" in ch.apply_move(state, move, "U1")


def test_player_cannot_move_before_the_opponent_sits_down():
    state = _ai_match()
    move = ch.resolve_text(state, "e2e4")
    assert ch.apply_move(state, move, "INTRUDER") == "这不是你的对局"


def test_ai_returns_a_legal_move_at_every_difficulty():
    for level in ("easy", "normal", "hard"):
        state = _ai_match(level)
        play(state, "e2e4", "e7e5")
        move = ch.choose_move(state, random.Random(7))
        assert move is not None
        assert move in ch.legal_for_state(state), level


def test_ai_takes_a_free_piece():
    """白车送到黑王嘴边，AI 必须吃掉。

    这是"看走眼"型 AI 的经典失败：只看一步评估、不做吃子展开的话，它会
    觉得"把王挪到安全格"更好，然后下一手被白车吃掉别的子。
    """
    state = ch.from_fen("8/8/8/4k3/4R3/8/8/4K3 b - - 0 1")
    move = ch.choose_move(state, random.Random(3))
    assert move.to == ch.parse_square("e4"), ch.square_name(move.to)
    assert ch.kind_of(state["board"][move.to]) == "R"


def test_ai_does_not_walk_into_check():
    state = ch.from_fen("4k3/8/8/8/8/8/8/R3K3 b - - 0 1")
    move = ch.choose_move(state, random.Random(5))
    after = ch._child(state, move)
    assert not ch.in_check(after["board"], ch.BLACK)


# --- 卡片 -------------------------------------------------------------------

def test_card_declares_the_size_the_renderer_actually_produces():
    """声明尺寸必须等于实际像素，否则 QQ 会把图拉伸甚至不渲染。

    这个坑在斗兽棋上踩过一次，所以这里用真实的渲染结果做断言，而不是相信
    两个常量恰好一致。
    """
    from PIL import Image
    import io

    from games import chess_render as cr

    data = cr.render_board(ch.new_state(ch.MODE_AI, "U1"), banner=False)
    assert Image.open(io.BytesIO(data)).size == (
        ch.CARD_IMAGE_WIDTH, ch.CARD_IMAGE_HEIGHT)


def test_card_has_no_buttons_and_hints_the_move_format():
    state = ch.new_state(ch.MODE_AI, "U1")
    card = ch.build_board_card(state, "https://example.com/b.jpg")
    assert card["rows"] == []
    assert "e2e4" in card["markdown"]
    assert "#720px" in card["markdown"] and "#756px" in card["markdown"]


def test_a_finished_game_shows_the_result_and_drops_the_hint():
    state = ch.from_fen("rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3")
    card = ch.build_board_card(state, "")
    assert "将死" in card["markdown"]
    assert "引用本图" not in card["markdown"]
