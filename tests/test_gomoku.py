"""Gomoku rules, coordinates and rendering."""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from games import gomoku as g  # noqa: E402


def place(board, coord, mark):
    board[g.parse_coordinate(coord)] = mark


# --- coordinates ------------------------------------------------------------

def test_parses_letter_number_coordinates():
    assert g.parse_coordinate("A1") == 0
    assert g.parse_coordinate("H8") == g.index_of(7, 7)
    assert g.parse_coordinate("O15") == g.SIZE * g.SIZE - 1


def test_coordinate_parsing_is_forgiving_about_case_and_spaces():
    assert g.parse_coordinate(" h8 ") == g.parse_coordinate("H8")
    assert g.parse_coordinate("h 8") == g.parse_coordinate("H8")


def test_rejects_out_of_range_coordinates():
    for bad in ("Z9", "A0", "A16", "", "88", "H", None, "H8H"):
        assert g.parse_coordinate(bad) == -1, bad


def test_coordinates_round_trip():
    for index in (0, 7, 112, g.SIZE * g.SIZE - 1):
        assert g.parse_coordinate(g.format_coordinate(index)) == index


# --- win detection ----------------------------------------------------------

def test_detects_five_in_each_direction():
    cases = {
        "horizontal": ["D8", "E8", "F8", "G8", "H8"],
        "vertical": ["H4", "H5", "H6", "H7", "H8"],
        "diagonal": ["D4", "E5", "F6", "G7", "H8"],
        "anti": ["H4", "G5", "F6", "E7", "D8"],
    }
    for name, coords in cases.items():
        board = g.new_board()
        for coord in coords:
            place(board, coord, g.BLACK)
        assert g.winner(board) == g.BLACK, name
        assert len(g.winning_line(board)) == 5, name


def test_four_in_a_row_is_not_a_win():
    board = g.new_board()
    for coord in ("D8", "E8", "F8", "G8"):
        place(board, coord, g.BLACK)
    assert g.winner(board) == ""


def test_a_line_broken_by_the_opponent_is_not_a_win():
    board = g.new_board()
    for coord in ("D8", "E8", "G8", "H8"):
        place(board, coord, g.BLACK)
    place(board, "F8", g.WHITE)
    assert g.winner(board) == ""


def test_wrapping_across_a_row_edge_is_not_a_win():
    """O1..A2 are adjacent in the flat list but not on the board."""
    board = g.new_board()
    for coord in ("L1", "M1", "N1", "O1", "A2"):
        place(board, coord, g.BLACK)
    assert g.winner(board) == ""


# --- moves ------------------------------------------------------------------

def test_rejects_occupied_and_out_of_range():
    state = g.new_state(g.MODE_AI, "U1")
    assert g.apply_move(state, g.parse_coordinate("H8"), "U1") == ""
    assert "已经有子" in g.apply_move(state, g.parse_coordinate("H8"), "U1")
    assert g.apply_move(state, -1, "U1") == "坐标超出棋盘范围"


def test_ai_game_rejects_other_players():
    state = g.new_state(g.MODE_AI, "U1")
    assert g.apply_move(state, g.parse_coordinate("H8"), "U2") == "这不是你的对局"


def test_pvp_waits_for_a_seated_opponent_before_any_move():
    """No move is legal until someone takes the ⚪ seat on the waiting card.

    Seating used to happen implicitly on the first move by a stranger. That is
    now a card click, so the board picture is only spent on a real match --
    and, more importantly, so the flow reads identically to tic-tac-toe.
    """
    state = g.new_state(g.MODE_PVP, "U1")
    assert state["phase"] == g.PHASE_WAITING
    assert g.apply_move(state, g.parse_coordinate("H8"), "U1") == \
        "还在等对手加入，点「加入对战」入座"

    state["players"][g.WHITE] = "U2"
    state["phase"] = g.PHASE_PLAYING
    assert g.apply_move(state, g.parse_coordinate("H8"), "U1") == ""
    assert g.apply_move(state, g.parse_coordinate("H9"), "U2") == ""
    assert g.apply_move(state, g.parse_coordinate("H10"), "U2") == "现在不是你的回合"


def test_ai_mode_starts_playing_immediately():
    assert g.new_state(g.MODE_AI, "U1")["phase"] == g.PHASE_PLAYING


def test_last_move_is_tracked_for_the_marker():
    state = g.new_state(g.MODE_AI, "U1")
    g.apply_move(state, g.parse_coordinate("H8"), "U1")
    assert state["last_move"] == g.parse_coordinate("H8")


# --- AI ---------------------------------------------------------------------

def test_ai_completes_its_own_five():
    board = g.new_board()
    for coord in ("D8", "E8", "F8", "G8"):
        place(board, coord, g.WHITE)
    # Either end completes the five; both are correct.
    assert g.ai_move(board, g.WHITE, level=g.LEVEL_HARD) in {
        g.parse_coordinate("C8"), g.parse_coordinate("H8")
    }


def test_ai_blocks_an_open_four():
    board = g.new_board()
    for coord in ("D8", "E8", "F8", "G8"):
        place(board, coord, g.BLACK)
    blocked = g.ai_move(board, g.WHITE, level=g.LEVEL_HARD)
    assert blocked in {g.parse_coordinate("C8"), g.parse_coordinate("H8")}


def test_ai_opens_in_the_centre():
    assert g.ai_move(g.new_board(), g.WHITE, level=g.LEVEL_HARD) == g.index_of(7, 7)


def test_ai_never_returns_an_occupied_cell():
    rng = random.Random(3)
    state = g.new_state(g.MODE_AI, "U1")
    for _ in range(30):
        if g.is_over(state["board"]):
            break
        free = g.free_cells(state["board"])
        g.apply_move(state, rng.choice(free), "U1")
        index = g.maybe_ai_move(state, rng)
        if index >= 0:
            assert state["board"][index] == g.WHITE


def test_difficulty_slip_is_ordered():
    assert g.LEVEL_SLIP[g.LEVEL_EASY] > g.LEVEL_SLIP[g.LEVEL_NORMAL] > g.LEVEL_SLIP[g.LEVEL_HARD]
    assert g.LEVEL_SLIP[g.LEVEL_HARD] == 0.0


def test_a_full_game_terminates():
    rng = random.Random(11)
    state = g.new_state(g.MODE_AI, "U1")
    for _ in range(g.SIZE * g.SIZE):
        if g.is_over(state["board"]):
            break
        g.apply_move(state, rng.choice(g.free_cells(state["board"])), "U1")
        g.maybe_ai_move(state, rng)
    assert g.is_over(state["board"])


# --- presentation -----------------------------------------------------------

def test_status_never_shows_a_raw_openid():
    openid = "15CB6AB7A714145630DF8DEBD0CA9294"
    state = g.new_state(g.MODE_PVP, openid)
    state["players"][g.WHITE] = "2561FB890E2E9EE221A68C42E1718D09"
    text = g.status_text(state)
    assert openid not in text and openid[-6:] not in text


def test_status_uses_injected_labels():
    state = g.new_state(g.MODE_PVP, "U1")
    state["labels"] = {g.BLACK: "小明"}
    assert "小明" in g.status_text(state)


def test_move_hint_mentions_the_last_move():
    state = g.new_state(g.MODE_AI, "U1")
    assert "H8" in g.move_hint(state) or "例如" in g.move_hint(state)
    g.apply_move(state, g.parse_coordinate("D4"), "U1")
    assert "D4" in g.move_hint(state)


def test_no_hint_once_the_game_is_over():
    state = g.new_state(g.MODE_AI, "U1")
    for coord in ("H4", "H5", "H6", "H7", "H8"):
        place(state["board"], coord, g.BLACK)
    assert g.move_hint(state) == ""


# --- rendering --------------------------------------------------------------

def test_board_renders_to_png():
    render = pytest.importorskip("games.gomoku_render", reason="需要 Pillow")
    state = g.new_state(g.MODE_PVP, "U1")
    state["players"][g.WHITE] = "U2"
    state["labels"] = {g.BLACK: "小明", g.WHITE: "小红"}
    place(state["board"], "H8", g.BLACK)
    state["last_move"] = g.parse_coordinate("H8")
    data = render.render_board(state)
    assert data.startswith(b"\x89PNG"), "应输出 PNG"
    assert len(data) < 500_000, "棋盘图不该过大"


def test_render_survives_missing_avatars():
    render = pytest.importorskip("games.gomoku_render", reason="需要 Pillow")
    state = g.new_state(g.MODE_AI, "U1")
    assert render.render_board(state, avatars={g.BLACK: b"not-an-image"})


def test_font_can_draw_chinese_when_available():
    """Nicknames are usually Chinese; tofu boxes would make the board useless."""
    render = pytest.importorskip("games.gomoku_render", reason="需要 Pillow")
    font = render._font(20)
    if getattr(font, "path", None):
        assert render._can_render_cjk(font), f"选中的字体无法渲染中文: {font.path}"


def test_avatar_url_uses_the_qq_app_cdn():
    render = pytest.importorskip("games.gomoku_render", reason="需要 Pillow")
    url = render.avatar_url("102824564", "ABCDEF")
    assert url == "https://thirdqq.qlogo.cn/qqapp/102824564/ABCDEF/640"
    assert render.avatar_url("", "ABCDEF") == ""


# --- rendering details the user asked for -----------------------------------

def test_labels_only_on_top_and_left():
    render = pytest.importorskip("games.gomoku_render", reason="需要 Pillow")
    source = Path(__file__).resolve().parents[1].joinpath(
        "games/gomoku_render.py").read_text("utf-8")
    body = source[source.index("for i in range(g.SIZE):"):]
    body = body[: body.index("radius = ")]
    assert body.count("draw.text(") == 2, "四边都标注会显得杂乱，只保留上/左"
    assert "BOARD_PX + 24" not in body and "BOARD_PX + 28" not in body


def test_move_hint_is_drawn_inside_the_image():
    pytest.importorskip("games.gomoku_render", reason="需要 Pillow")
    source = Path(__file__).resolve().parents[1].joinpath(
        "games/gomoku_render.py").read_text("utf-8")
    assert "g.move_hint(state)" in source, "提示语应画进图片而非正文"


def test_the_hint_is_never_also_sent_as_the_image_caption():
    """It goes in the picture *or* the body, never both.

    Passing text= to send_image_message printed the hint a second time in the
    chat body -- which is precisely what drawing it into the image was meant to
    replace.
    """
    source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text("utf-8")
    call = source[source.index("await hub.send_image_message("):]
    call = call[: call.index(")")]
    assert "text=" not in call, "棋盘图不应再带正文说明，提示语已在图内"
    assert "move_hint" not in call


def test_tofu_detection_rejects_a_font_without_cjk():
    """A font lacking CJK still draws a box, so bbox size proves nothing."""
    render = pytest.importorskip("games.gomoku_render", reason="需要 Pillow")
    from PIL import ImageFont
    try:
        dejavu = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
    except Exception:
        pytest.skip("测试环境没有 DejaVu")
    assert render._can_render_cjk(dejavu) is False, "豆腐块不算能渲染中文"


# --- the bundled font -------------------------------------------------------

def test_a_cjk_font_ships_with_the_plugin():
    """No system font, no apt-get: Chinese must work out of the box.

    Scanning system fonts and silently switching the whole board to English is
    what produced "为什么我的棋盘是英文的" on a stock Docker image.
    """
    render = pytest.importorskip("games.gomoku_render", reason="需要 Pillow")
    import os

    path = render.bundled_font_path()
    assert os.path.exists(path), "内置字体缺失，Docker 部署会退化成方框或英文"
    assert os.path.getsize(path) > 1_000_000


def test_the_bundled_font_alone_renders_chinese():
    """Proven by loading *only* the bundled file, with system fonts ignored."""
    render = pytest.importorskip("games.gomoku_render", reason="需要 Pillow")
    original = render._discover_font_paths
    render._FONT_CACHE.clear()
    render._discover_font_paths = lambda: [render.bundled_font_path()]
    try:
        assert render.has_cjk_font() is True
        assert render._fit("轮到你落子") == "轮到你落子"
        state = g.new_state(g.MODE_AI, "U1")
        state["labels"] = {g.BLACK: "无所事事"}
        assert render.render_board(state).startswith(b"\x89PNG")
    finally:
        render._discover_font_paths = original
        render._FONT_CACHE.clear()


def test_the_bundled_font_covers_every_word_the_board_draws():
    """Each glyph checked individually -- one missing char is one tofu box."""
    render = pytest.importorskip("games.gomoku_render", reason="需要 Pillow")
    from PIL import ImageFont

    font = ImageFont.truetype(render.bundled_font_path(), 24)
    missing = bytes(font.getmask("\ue000"))
    words = "黑白轮到你落子赢了平局思考中引用本图回复坐标例如上手待加入对战难度"
    absent = [ch for ch in words
              if not bytes(font.getmask(ch)) or bytes(font.getmask(ch)) == missing]
    assert absent == [], f"内置字体缺字形: {absent}"


def test_system_fonts_still_win_when_present():
    """The bundle is a floor, not a preference: a nicer host font is used."""
    render = pytest.importorskip("games.gomoku_render", reason="需要 Pillow")
    paths = render._discover_font_paths()
    assert paths[-1] == render.bundled_font_path(), "内置字体应排在系统字体之后"


def test_board_degrades_to_ascii_without_a_cjk_font():
    render = pytest.importorskip("games.gomoku_render", reason="需要 Pillow")
    # Only reachable on a corrupt install now that a CJK font is bundled; kept
    # so the degraded path still produces a readable board rather than crashing.
    import os
    if not os.path.exists("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        pytest.skip("测试环境没有 DejaVu")
    state = g.new_state(g.MODE_AI, "U1")
    state["labels"] = {g.BLACK: "无所事事"}
    original = render._discover_font_paths
    render._FONT_CACHE.clear()
    render._discover_font_paths = lambda: [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    ]
    try:
        assert render.has_cjk_font() is False
        assert render.render_board(state).startswith(b"\x89PNG")
        assert render._fit("无所事事abc") == "abc", "无 CJK 字体时应剔除中文"
    finally:
        render._discover_font_paths = original
        render._FONT_CACHE.clear()
