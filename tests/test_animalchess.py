"""Jungle chess rules, parsing and rendering geometry.

The interesting rules here are the ones that contradict intuition -- a rat in
the river cannot be touched by an elephant standing beside it, and a piece in
the enemy's trap can be eaten by anything. Each is pinned separately, because
"the AI played an illegal move" is only debuggable if the rule it broke has a
name.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from games import animalchess as ac  # noqa: E402
from games import lobby  # noqa: E402


def bare(mode=ac.MODE_PVP):
    """An empty board with both seats filled, so only the rule under test acts."""
    state = ac.new_state(mode, "U1")
    state["pieces"] = {}
    state["phase"] = ac.PHASE_PLAYING
    state["players"] = {ac.RED: "U1", ac.BLUE: "U2"}
    return state


def put(state, row, col, side, animal):
    state["pieces"][(row, col)] = (side, animal)


# --- board geometry ---------------------------------------------------------

def test_board_is_nine_by_seven_landscape():
    assert (ac.COLS, ac.ROWS) == (9, 7)


def test_rivers_match_the_two_by_three_blocks():
    """R2-R3 and R5-R6, columns C4-C6, exactly as drawn."""
    expected = {(r, c) for r in (1, 2, 4, 5) for c in (3, 4, 5)}
    assert set(ac.WATER) == expected
    assert len(ac.WATER) == 12


def test_dens_and_traps_sit_where_the_artwork_shows_them():
    assert ac.DENS[ac.RED] == (3, 0)      # R4C1
    assert ac.DENS[ac.BLUE] == (3, 8)     # R4C9
    assert ac.TRAPS[ac.RED] == {(2, 0), (4, 0), (3, 1)}
    assert ac.TRAPS[ac.BLUE] == {(2, 8), (4, 8), (3, 7)}


def test_no_terrain_overlaps():
    """A square cannot be two things at once; overlap would break rendering."""
    traps = ac.TRAPS[ac.RED] | ac.TRAPS[ac.BLUE]
    dens = set(ac.DENS.values())
    assert not (traps & ac.WATER)
    assert not (dens & ac.WATER)
    assert not (dens & traps)


def test_opening_layout_is_sixteen_pieces_on_dry_land():
    state = ac.new_state(ac.MODE_AI, "U1")
    assert len(state["pieces"]) == 16
    for square in state["pieces"]:
        assert square not in ac.WATER
        assert square not in ac.DENS.values()


def test_each_side_owns_exactly_one_of_every_animal():
    """The move syntax depends on this: 鼠下 must identify a single piece."""
    state = ac.new_state(ac.MODE_AI, "U1")
    for side in (ac.RED, ac.BLUE):
        owned = sorted(a for s, a in state["pieces"].values() if s == side)
        assert owned == sorted(ac.ANIMALS)


def test_the_layout_is_mirrored_between_the_sides():
    for animal, (row, col) in ac.START[ac.RED].items():
        mirrored = ac.START[ac.BLUE][animal]
        assert mirrored == (ac.ROWS - 1 - row, ac.COLS - 1 - col), animal


# --- capture rules ----------------------------------------------------------

@pytest.mark.parametrize("stronger,weaker", [
    (ac.ELEPHANT, ac.LION), (ac.LION, ac.TIGER), (ac.TIGER, ac.LEOPARD),
    (ac.LEOPARD, ac.WOLF), (ac.WOLF, ac.DOG), (ac.DOG, ac.CAT), (ac.CAT, ac.RAT),
])
def test_rank_order_象狮虎豹狼狗猫鼠(stronger, weaker):
    state = bare()
    put(state, 0, 0, ac.RED, stronger)
    put(state, 0, 1, ac.BLUE, weaker)
    assert ac.can_capture(state, (0, 0), (0, 1)) is True
    assert ac.can_capture(state, (0, 1), (0, 0)) is False


def test_equal_animals_may_trade():
    state = bare()
    put(state, 0, 0, ac.RED, ac.WOLF)
    put(state, 0, 1, ac.BLUE, ac.WOLF)
    assert ac.can_capture(state, (0, 0), (0, 1)) is True
    assert ac.can_capture(state, (0, 1), (0, 0)) is True


def test_rat_eats_elephant_but_not_the_other_way():
    state = bare()
    put(state, 0, 0, ac.RED, ac.RAT)
    put(state, 0, 1, ac.BLUE, ac.ELEPHANT)
    assert ac.can_capture(state, (0, 0), (0, 1)) is True
    assert ac.can_capture(state, (0, 1), (0, 0)) is False


def test_never_captures_a_friendly_piece():
    state = bare()
    put(state, 0, 0, ac.RED, ac.LION)
    put(state, 0, 1, ac.RED, ac.CAT)
    assert ac.can_capture(state, (0, 0), (0, 1)) is False


# --- water ------------------------------------------------------------------

def test_land_cannot_touch_a_rat_in_the_river_and_vice_versa():
    """The rat's whole survival trick, and the rule most often gotten wrong."""
    state = bare()
    put(state, 1, 3, ac.BLUE, ac.RAT)        # in the water
    put(state, 1, 2, ac.RED, ac.ELEPHANT)    # on the bank
    assert ac.can_capture(state, (1, 2), (1, 3)) is False, "岸上不能吃水里"
    assert ac.can_capture(state, (1, 3), (1, 2)) is False, "水里的鼠不能吃岸上的象"


def test_two_rats_in_the_water_may_trade():
    state = bare()
    put(state, 1, 3, ac.RED, ac.RAT)
    put(state, 1, 4, ac.BLUE, ac.RAT)
    assert ac.can_capture(state, (1, 3), (1, 4)) is True


def test_only_the_rat_may_enter_the_river():
    state = bare()
    put(state, 1, 2, ac.RED, ac.RAT)
    assert ac._resolve_target(state, (1, 2), ac.RAT, ac.RED, (0, 1)) == (1, 3)
    state = bare()
    put(state, 1, 2, ac.RED, ac.WOLF)
    assert ac._resolve_target(state, (1, 2), ac.WOLF, ac.RED, (0, 1)) is None


# --- lion / tiger leaps -----------------------------------------------------

def test_lion_leaps_the_river_horizontally():
    state = bare()
    put(state, 1, 2, ac.RED, ac.LION)
    assert ac._resolve_target(state, (1, 2), ac.LION, ac.RED, (0, 1)) == (1, 6)


def test_lion_leaps_the_river_vertically():
    """Down column C4 from R1: rows R2-R3 are water, so it lands on R4."""
    state = bare()
    put(state, 0, 3, ac.RED, ac.LION)
    assert ac._resolve_target(state, (0, 3), ac.LION, ac.RED, (1, 0)) == (3, 3)


def test_a_leap_may_capture_a_weaker_piece_on_the_far_bank():
    state = bare()
    put(state, 1, 2, ac.RED, ac.LION)
    put(state, 1, 6, ac.BLUE, ac.WOLF)
    assert ac._resolve_target(state, (1, 2), ac.LION, ac.RED, (0, 1)) == (1, 6)


def test_a_leap_onto_a_stronger_piece_is_refused():
    state = bare()
    put(state, 1, 2, ac.RED, ac.TIGER)
    put(state, 1, 6, ac.BLUE, ac.ELEPHANT)
    assert ac._resolve_target(state, (1, 2), ac.TIGER, ac.RED, (0, 1)) is None


@pytest.mark.parametrize("rat_side", [ac.RED, ac.BLUE], ids=["own", "enemy"])
def test_a_rat_anywhere_in_the_path_blocks_the_leap(rat_side):
    """Either colour blocks -- the rule is about the water, not the owner."""
    state = bare()
    put(state, 1, 2, ac.RED, ac.LION)
    put(state, 1, 4, rat_side, ac.RAT)
    assert ac._resolve_target(state, (1, 2), ac.LION, ac.RED, (0, 1)) is None


def test_a_leap_needs_water_in_front_of_it():
    state = bare()
    put(state, 0, 0, ac.RED, ac.LION)
    assert ac._resolve_target(state, (0, 0), ac.LION, ac.RED, (0, 1)) == (0, 1)


# --- traps and dens ---------------------------------------------------------

def test_anything_eats_a_piece_standing_in_the_enemy_trap():
    state = bare()
    put(state, 2, 0, ac.BLUE, ac.ELEPHANT)   # blue elephant in a red trap
    put(state, 1, 0, ac.RED, ac.CAT)
    assert ac.can_capture(state, (1, 0), (2, 0)) is True, "陷阱里的象连猫都能吃"


def test_your_own_trap_does_not_weaken_you():
    state = bare()
    put(state, 2, 0, ac.RED, ac.ELEPHANT)    # red elephant in its own trap
    put(state, 1, 0, ac.BLUE, ac.CAT)
    assert ac.can_capture(state, (1, 0), (2, 0)) is False


def test_a_piece_may_never_enter_its_own_den():
    state = bare()
    put(state, 2, 0, ac.RED, ac.DOG)         # R3C1, directly above the red den
    assert ac._resolve_target(state, (2, 0), ac.DOG, ac.RED, (1, 0)) is None


def test_entering_the_enemy_den_wins_immediately():
    state = bare()
    put(state, 2, 8, ac.RED, ac.CAT)         # beside the blue den
    put(state, 6, 6, ac.BLUE, ac.LION)
    assert ac.apply_move(state, ac.CAT, "下", "U1") == ""
    assert state["winner"] == ac.RED
    assert state["win_reason"] == "den"


def test_capturing_the_last_enemy_piece_wins():
    state = bare()
    put(state, 0, 0, ac.RED, ac.LION)
    put(state, 0, 1, ac.BLUE, ac.CAT)
    assert ac.apply_move(state, ac.LION, "右", "U1") == ""
    assert state["winner"] == ac.RED
    assert state["win_reason"] == "wiped"


# --- move parsing -----------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("鼠下", (ac.RAT, "下")),
    ("虎上", (ac.TIGER, "上")),
    ("老虎 上", (ac.TIGER, "上")),
    ("大象右", (ac.ELEPHANT, "右")),
    ("狮子左", (ac.LION, "左")),
    ("　豹　下　", (ac.LEOPARD, "下")),
])
def test_parses_the_documented_move_syntax(text, expected):
    assert ac.parse_move(text) == expected


@pytest.mark.parametrize("text", [
    "", "H8", "今天天气不错", "鼠", "下", "鼠飞", "麒麟下",
    "我觉得这局鼠下不去了啊真的很难受",
])
def test_ordinary_chat_is_not_a_move(text):
    """A false positive here would hijack conversation, so be strict."""
    assert ac.parse_move(text) is None


# --- turn order and ownership ----------------------------------------------

def test_red_moves_first_and_turns_alternate():
    state = ac.new_state(ac.MODE_PVP, "U1")
    state["players"][ac.BLUE] = "U2"
    state["phase"] = ac.PHASE_PLAYING
    assert state["turn"] == ac.RED
    assert ac.apply_move(state, ac.DOG, "上", "U1") == ""
    assert state["turn"] == ac.BLUE


def test_a_player_cannot_move_out_of_turn():
    state = ac.new_state(ac.MODE_PVP, "U1")
    state["players"][ac.BLUE] = "U2"
    state["phase"] = ac.PHASE_PLAYING
    assert ac.apply_move(state, ac.DOG, "上", "U2") == "现在不是你的回合"


def test_no_move_is_accepted_before_an_opponent_is_seated():
    state = ac.new_state(ac.MODE_PVP, "U1")
    assert state["phase"] == ac.PHASE_WAITING
    assert ac.apply_move(state, ac.DOG, "上", "U1") == \
        "还在等对手加入，点「加入对战」入座"


def test_ai_mode_starts_playing_immediately():
    assert ac.new_state(ac.MODE_AI, "U1")["phase"] == ac.PHASE_PLAYING


def test_refusals_explain_themselves():
    """'不能这样走' teaches nobody; each refusal names the rule."""
    state = bare()
    put(state, 1, 2, ac.RED, ac.WOLF)
    assert ac.apply_move(state, ac.WOLF, "右", "U1") == "狼不能下水"

    state = bare()
    put(state, 0, 0, ac.RED, ac.CAT)
    assert ac.apply_move(state, ac.CAT, "上", "U1") == "已经到棋盘边缘了"

    state = bare()
    put(state, 0, 0, ac.RED, ac.CAT)
    put(state, 0, 1, ac.BLUE, ac.LION)
    assert ac.apply_move(state, ac.CAT, "右", "U1") == "猫吃不掉狮"

    state = bare()
    put(state, 1, 2, ac.RED, ac.LION)
    put(state, 1, 4, ac.BLUE, ac.RAT)
    assert ac.apply_move(state, ac.LION, "右", "U1") == "河里有鼠挡路，跳不过去"


def test_moving_a_captured_animal_is_refused_by_name():
    state = bare()
    put(state, 0, 0, ac.RED, ac.CAT)
    put(state, 6, 8, ac.BLUE, ac.LION)
    assert ac.apply_move(state, ac.ELEPHANT, "上", "U1") == "你的象已经被吃掉了"


# --- AI ---------------------------------------------------------------------

def test_ai_takes_a_free_win_by_entering_the_den():
    state = bare(ac.MODE_AI)
    state["turn"] = ac.BLUE
    put(state, 2, 0, ac.BLUE, ac.CAT)        # one step from the red den
    put(state, 6, 8, ac.RED, ac.LION)
    assert ac.ai_move(state, ac.BLUE, level=ac.LEVEL_HARD) == (ac.CAT, "下")


def test_ai_prefers_a_free_capture_over_a_quiet_move():
    """The victim must be *weaker*: a lion cannot eat an elephant.

    An earlier version of this test asserted exactly that illegal capture and
    the AI was right to refuse it -- which is the whole reason the assertion
    names the pieces rather than trusting a rank table by eye.

    Placed mid-board on purpose: next to a den the search correctly prefers a
    forced win over any capture, which would make this test measure the wrong
    thing.
    """
    state = bare(ac.MODE_AI)
    state["turn"] = ac.BLUE
    put(state, 0, 4, ac.BLUE, ac.LION)
    put(state, 0, 5, ac.RED, ac.WOLF)        # adjacent, weaker, undefended
    put(state, 6, 4, ac.RED, ac.CAT)
    animal, direction = ac.ai_move(state, ac.BLUE, level=ac.LEVEL_HARD)
    assert (animal, direction) == (ac.LION, "右")


def test_ai_never_returns_an_illegal_move():
    """Fuzzed: every AI move must survive the same validator a human faces."""
    import random

    rng = random.Random(20260804)
    for _ in range(12):
        state = ac.new_state(ac.MODE_PVP, "U1")
        state["players"][ac.BLUE] = "U2"
        state["phase"] = ac.PHASE_PLAYING
        for _ in range(40):
            if ac.is_over(state):
                break
            side = state["turn"]
            choice = ac.ai_move(state, side, rng, ac.LEVEL_EASY)
            if choice is None:
                break
            animal, direction = choice
            actor = state["players"][side]
            assert ac.apply_move(state, animal, direction, actor) == "", (
                f"AI 走出非法着法 {animal}{direction}"
            )


def test_a_win_now_beats_a_win_later():
    """Regression: undiscounted wins made every winning line look identical.

    With a cat one step from the enemy den, entering it and stepping sideways
    both scored WIN_SCORE, so the AI picked arbitrarily and wandered. Wins are
    now discounted by distance, which is what makes 'mate in one' meaningful.
    """
    state = bare(ac.MODE_AI)
    state["turn"] = ac.BLUE
    put(state, 2, 0, ac.BLUE, ac.CAT)        # one step above the red den
    put(state, 6, 8, ac.RED, ac.LION)

    scores = {}
    for animal, options in ac.legal_moves(state, ac.BLUE).items():
        for name, target in options:
            child = ac._apply_for_search(state, ac.BLUE, animal, target)
            scores[name] = ac._search(
                child, ac.BLUE, 4, -ac.WIN_SCORE * 2, ac.WIN_SCORE * 2, False)
    assert scores["下"] > scores["上"], "立即获胜必须严格优于绕远路"
    assert ac.ai_move(state, ac.BLUE, level=ac.LEVEL_HARD) == (ac.CAT, "下")


def test_difficulty_ladder_is_ordered():
    assert ac.LEVEL_DEPTH[ac.LEVEL_EASY] < ac.LEVEL_DEPTH[ac.LEVEL_HARD]
    assert ac.LEVEL_SLIP[ac.LEVEL_EASY] > ac.LEVEL_SLIP[ac.LEVEL_NORMAL] \
        > ac.LEVEL_SLIP[ac.LEVEL_HARD]
    assert ac.LEVEL_SLIP[ac.LEVEL_HARD] == 0.0
    assert set(ac.LEVEL_SLIP) == set(ac.LEVEL_DEPTH) == set(lobby.AI_LEVELS)


def test_a_full_ai_game_terminates():
    import random

    rng = random.Random(7)
    state = ac.new_state(ac.MODE_PVP, "U1")
    state["players"][ac.BLUE] = "U2"
    state["phase"] = ac.PHASE_PLAYING
    for _ in range(400):
        if ac.is_over(state):
            break
        choice = ac.ai_move(state, state["turn"], rng, ac.LEVEL_NORMAL)
        if choice is None:
            break
        ac._force_move(state, state["turn"], *choice)
    assert len(state["pieces"]) <= 16


# --- presentation -----------------------------------------------------------

def test_status_never_shows_a_raw_openid():
    state = ac.new_state(ac.MODE_PVP, "15CB6AB7A714145630DF8DEBD0CA9294")
    state["players"][ac.BLUE] = "BE4A096E28B40FEDEB3320E5E8D7C2A7"
    state["phase"] = ac.PHASE_PLAYING
    text = ac.status_text(state)
    assert "15CB6AB7" not in text and "BE4A0962" not in text


def test_move_hint_mentions_the_last_move_and_disappears_at_the_end():
    state = bare()
    put(state, 0, 0, ac.RED, ac.LION)
    put(state, 0, 1, ac.BLUE, ac.CAT)
    assert "鼠下" in ac.move_hint(state)
    ac.apply_move(state, ac.LION, "右", "U1")
    assert ac.move_hint(state) == "", "终局不再提示走法"


# --- lobby integration ------------------------------------------------------

def test_animalchess_inherits_the_shared_five_button_lobby():
    card = ac.build_lobby_card()
    assert card == lobby.build_lobby_card(ac.SPEC)
    labels = [b["label"] for row in card["rows"] for b in row]
    assert labels == ["🤖 人机对战", "👥 群友对战", "轻松", "✅ 普通", "困难"]
    for row in card["rows"]:
        for button in row:
            assert button["action_id"].startswith("animalchess.")


def test_every_animalchess_action_is_registered():
    source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text("utf-8")
    card = lobby.build_lobby_card(ac.SPEC)
    waiting = lobby.build_waiting_card(ac.SPEC, "小明", "U1")
    for row in card["rows"] + waiting["rows"]:
        for button in row:
            assert f'"{button["action_id"]}"' in source
    assert '"animalchess.lobby"' in source


# --- rendering geometry -----------------------------------------------------

def test_grid_matches_the_supplied_artwork_exactly():
    render = pytest.importorskip("games.animalchess_render", reason="需要 Pillow")
    assert (render.BOARD_WIDTH, render.BOARD_HEIGHT) == (2135, 1436)
    assert render.CELL_WIDTH == pytest.approx(2135 / 9)
    assert render.CELL_HEIGHT == pytest.approx(1436 / 7)
    # Full bleed: the last square must end exactly on the far edge.
    assert render.square_centre(6, 8)[0] == pytest.approx(2135 - render.CELL_WIDTH / 2)
    assert render.square_centre(6, 8)[1] == pytest.approx(1436 - render.CELL_HEIGHT / 2)


def test_first_square_centre_is_where_the_maths_says():
    render = pytest.importorskip("games.animalchess_render", reason="需要 Pillow")
    x, y = render.square_centre(0, 0)
    assert (round(x, 1), round(y, 1)) == (118.6, 102.6)


def test_the_board_renders_without_any_artwork():
    """Placeholders are blank on purpose; the game must still be playable."""
    render = pytest.importorskip("games.animalchess_render", reason="需要 Pillow")
    data = render.render_board(ac.new_state(ac.MODE_AI, "U1"))
    assert data.startswith(b"\x89PNG")
    assert len(data) > 5000


def test_a_blank_placeholder_counts_as_missing_art():
    """Otherwise the shipped transparent PNGs would render an invisible board."""
    render = pytest.importorskip("games.animalchess_render", reason="需要 Pillow")
    from PIL import Image

    assert render._is_blank(Image.new("RGBA", (8, 8), (0, 0, 0, 0))) is True
    assert render._is_blank(Image.new("RGBA", (8, 8), (12, 34, 56, 255))) is False


def test_placeholder_assets_exist_at_the_documented_paths():
    """The user overwrites these files directly, so the names are a contract."""
    import os

    render = pytest.importorskip("games.animalchess_render", reason="需要 Pillow")
    assert os.path.exists(render.BOARD_ASSET)
    for animal in ac.ANIMALS:
        assert os.path.exists(render.piece_asset_path(animal)), animal


def test_both_sides_share_one_image_per_animal():
    render = pytest.importorskip("games.animalchess_render", reason="需要 Pillow")
    assert render.piece_asset_path(ac.RAT).endswith("rat.png")
    assert "red" not in render.piece_asset_path(ac.RAT)


def test_piece_art_keeps_its_aspect_ratio():
    """Forcing every piece into a square squashed non-1:1 artwork.

    The image is scaled to *fit* the cell box and centred on a transparent
    square, so each piece occupies the same footprint on the board while its
    proportions stay exactly as drawn.
    """
    render = pytest.importorskip("games.animalchess_render", reason="需要 Pillow")
    from PIL import Image

    for size_in, expected_ratio in (((400, 200), 2.0), ((120, 480), 0.25),
                                    ((256, 256), 1.0)):
        art = Image.new("RGBA", size_in, (255, 0, 0, 255))
        fitted = render._fit_square(art, 100)
        assert fitted.size == (100, 100), "外框必须是正方形，保证每格占位一致"
        box = fitted.getbbox()
        width, height = box[2] - box[0], box[3] - box[1]
        assert width / height == pytest.approx(expected_ratio, rel=0.02), (
            f"{size_in} 被拉伸了"
        )
        assert max(width, height) == 100, "应缩放到刚好贴合，不留多余空白"


def test_a_square_piece_is_untouched():
    render = pytest.importorskip("games.animalchess_render", reason="需要 Pillow")
    from PIL import Image

    fitted = render._fit_square(Image.new("RGBA", (256, 256), (0, 0, 255, 255)), 64)
    assert fitted.size == (64, 64)


def test_loading_a_piece_never_distorts_it():
    """The loader must go through the aspect-preserving path, not resize()."""
    source = Path(__file__).resolve().parents[1].joinpath(
        "games/animalchess_render.py").read_text("utf-8")
    loader = source[source.index("def _load_piece"):]
    loader = loader[: loader.index("def clear_asset_cache")]
    assert "_fit_square(loaded, size)" in loader
    assert ".resize((size, size)" not in loader, "直接拉成正方形会压扁棋子"
