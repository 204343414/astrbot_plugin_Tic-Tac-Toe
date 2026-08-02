"""Rules, AI and card rendering. No AstrBot needed."""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from game import (  # noqa: E402
    AI, HUMAN, MODE_AI, MODE_PVP, ai_move, apply_move, autoplay_forced_move,
    build_card, is_full, is_over, maybe_ai_move, new_state, render_board_text,
    winner,
)


def board_from(text: str) -> list[str]:
    """'O.X|...|...' -> flat board."""
    flat = text.replace("|", "")
    return [{"O": HUMAN, "X": AI, ".": ""}[c] for c in flat]


# --- rules ------------------------------------------------------------------

def test_detects_all_win_lines():
    assert winner(board_from("OOO|...|...")) == HUMAN
    assert winner(board_from("...|XXX|...")) == AI
    assert winner(board_from("O..|O..|O..")) == HUMAN
    assert winner(board_from("X..|.X.|..X")) == AI
    assert winner(board_from("..X|.X.|X..")) == AI
    assert winner(board_from("OX.|...|...")) == ""


def test_draw_is_over_without_a_winner():
    full = board_from("OXO|OXX|XOO")
    assert is_full(full) and not winner(full) and is_over(full)


def test_cannot_play_an_occupied_cell():
    state = new_state(MODE_AI, "A")
    assert apply_move(state, 0, "A") == ""
    assert apply_move(state, 0, "A") == "该位置已被占据"


def test_cannot_play_out_of_range():
    state = new_state(MODE_AI, "A")
    assert apply_move(state, 9, "A") == "位置无效"
    assert apply_move(state, -1, "A") == "位置无效"


def test_cannot_play_after_game_over():
    state = new_state(MODE_PVP, "A")
    state["board"] = board_from("OOO|...|...")
    assert apply_move(state, 4, "A") == "对局已结束"


def test_ai_game_rejects_a_stranger():
    state = new_state(MODE_AI, "A")
    assert apply_move(state, 0, "B") == "这不是你的对局"


# --- AI ---------------------------------------------------------------------

def test_ai_takes_the_win():
    board = board_from("XX.|OO.|...")
    assert ai_move(board, AI) == 2


def test_ai_blocks_an_immediate_loss():
    board = board_from("OO.|X..|...")
    assert ai_move(board, AI) == 2, "must block rather than wander off"


def test_ai_prefers_winning_over_blocking():
    board = board_from("XX.|OO.|...")
    assert ai_move(board, AI) == 2


def test_ai_takes_centre_when_free():
    assert ai_move(board_from("X..|...|..."), AI) == 4


def test_ai_never_picks_an_occupied_cell():
    rng = random.Random(0)
    board = board_from("OXO|XOX|..X")
    for _ in range(20):
        assert board[ai_move(board, AI, rng)] == ""


def test_ai_returns_minus_one_on_full_board():
    assert ai_move(board_from("OXO|OXX|XOO"), AI) == -1


def test_maybe_ai_move_is_noop_in_pvp():
    state = new_state(MODE_PVP, "A")
    state["turn"] = AI
    assert maybe_ai_move(state) == -1


def test_ai_replies_after_the_human_moves():
    state = new_state(MODE_AI, "A")
    apply_move(state, 0, "A")
    assert maybe_ai_move(state) >= 0
    assert state["turn"] == HUMAN
    assert state["board"].count(AI) == 1


# --- PvP turn taking --------------------------------------------------------

def test_second_player_joins_by_moving():
    state = new_state(MODE_PVP, "A")
    assert apply_move(state, 0, "A") == ""
    assert apply_move(state, 1, "B") == ""
    assert state["players"][AI] == "B"


def test_pvp_rejects_playing_out_of_turn():
    state = new_state(MODE_PVP, "A")
    apply_move(state, 0, "A")
    apply_move(state, 1, "B")
    assert apply_move(state, 2, "B") == "现在不是你的回合"


def test_host_cannot_take_both_seats():
    state = new_state(MODE_PVP, "A")
    apply_move(state, 0, "A")
    assert apply_move(state, 1, "A") == "等待对手落子"


# --- card rendering ---------------------------------------------------------

def test_only_free_cells_are_playable():
    """Occupied squares remain on the grid but are no longer playable."""
    state = new_state(MODE_AI, "A")
    state["board"] = board_from("OX.|...|...")
    by_id = {b["id"]: b for row in build_card(state)["rows"] for b in row}
    assert by_id["cell0"]["action_id"] == "tictactoe.occupied"
    assert by_id["cell1"]["action_id"] == "tictactoe.occupied"
    assert by_id["cell2"]["action_id"] == "tictactoe.move"
    assert by_id["cell8"]["action_id"] == "tictactoe.move"


def test_every_cell_button_is_one_shot():
    state = new_state(MODE_AI, "A")
    for row in build_card(state)["rows"]:
        for button in row:
            if button["action_id"] == "tictactoe.move":
                assert button["one_shot"] is True, "a square must not be replayable"


def test_cells_are_locked_to_the_player_whose_turn_it_is():
    state = new_state(MODE_AI, "A")
    cells = [b for row in build_card(state)["rows"] for b in row
             if b["action_id"] == "tictactoe.move"]
    assert cells and all(b["owner_openid"] == "A" for b in cells)
    assert all(b["owner_mode"] == "specified" for b in cells)


def test_pvp_lock_follows_the_turn():
    state = new_state(MODE_PVP, "A")
    apply_move(state, 0, "A")
    state["players"][AI] = "B"
    cells = [b for row in build_card(state)["rows"] for b in row
             if b["action_id"] == "tictactoe.move"]
    assert cells and all(b["owner_openid"] == "B" for b in cells)


def test_open_seat_lets_anyone_click():
    """Before the second player joins, the board must not be locked to nobody."""
    state = new_state(MODE_PVP, "A")
    apply_move(state, 0, "A")          # now it is X's turn, seat still empty
    cells = [b for row in build_card(state)["rows"] for b in row
             if b["action_id"] == "tictactoe.move"]
    assert cells and all(b["owner_mode"] == "everyone" for b in cells)


def test_finished_game_shows_restart_and_no_cells():
    state = new_state(MODE_AI, "A")
    state["board"] = board_from("OOO|...|...")
    card = build_card(state)
    ids = [b["id"] for row in card["rows"] for b in row]
    assert not any(i.startswith("cell") for i in ids)
    assert ids == ["quit"]
    assert card["rows"][0][0]["action_id"] == "tictactoe.restart"
    assert "你赢了" in card["markdown"]


def test_card_is_not_card_level_one_shot():
    """Looking at a stale board must not kill the match."""
    assert build_card(new_state(MODE_AI, "A"))["one_shot"] is False


def test_card_respects_qq_5x5_limits():
    card = build_card(new_state(MODE_AI, "A"))
    assert len(card["rows"]) <= 5
    assert all(len(row) <= 5 for row in card["rows"])


def test_board_text_marks_the_winning_line():
    text = render_board_text(board_from("OOO|...|..."))
    assert text.startswith("**⭕** **⭕** **⭕**")


def test_full_ai_game_always_terminates():
    """Play a whole game with a scripted human; it must end cleanly."""
    rng = random.Random(7)
    state = new_state(MODE_AI, "A")
    for _ in range(9):
        if is_over(state["board"]):
            break
        free = [i for i, c in enumerate(state["board"]) if not c]
        apply_move(state, rng.choice(free), "A")
        maybe_ai_move(state, rng)
    assert is_over(state["board"])
    build_card(state)  # must still render


# --- server-side rules are the source of truth ------------------------------

def test_rules_hold_without_any_button():
    """A button is only a shortcut; typing /下棋 must be equally safe."""
    state = new_state(MODE_PVP, "A")
    assert apply_move(state, 0, "A") == ""
    # occupied square refused even though no button was involved
    assert apply_move(state, 0, "B") == "该位置已被占据"
    # out-of-turn refused
    apply_move(state, 1, "B")
    assert apply_move(state, 2, "B") == "现在不是你的回合"


def test_refusal_reasons_are_human_readable():
    state = new_state(MODE_AI, "A")
    assert apply_move(state, 99, "A") == "位置无效"
    assert apply_move(state, 0, "B") == "这不是你的对局"


# --- 3x3 grid stability -----------------------------------------------------

def test_board_is_always_a_full_3x3_grid():
    """A shrinking grid makes players mis-tap. Occupied squares stay in place."""
    state = new_state(MODE_AI, "A")
    for played in range(9):
        if is_over(state["board"]):
            break
        grid = [r for r in build_card(state)["rows"]
                if all(b["id"].startswith("cell") for b in r)]
        assert len(grid) == 3, f"落子 {played} 手后行数应为 3"
        assert all(len(r) == 3 for r in grid), f"落子 {played} 手后每行应为 3"
        free = [i for i, c in enumerate(state["board"]) if not c]
        apply_move(state, free[0], "A")
        maybe_ai_move(state)


def test_cells_keep_their_position_index():
    """cell4 must always be the centre, whatever has been played."""
    state = new_state(MODE_AI, "A")
    apply_move(state, 0, "A")
    grid = [b for r in build_card(state)["rows"] for b in r
            if b["id"].startswith("cell")]
    assert [b["id"] for b in grid] == [f"cell{i}" for i in range(9)]


def test_occupied_squares_show_the_mark_and_a_no_op_action():
    state = new_state(MODE_AI, "A")
    apply_move(state, 0, "A")
    button = next(b for r in build_card(state)["rows"] for b in r
                  if b["id"] == "cell0")
    assert button["label"] == "⭕", "已落子的格子应显示棋子"
    assert button["action_id"] == "tictactoe.occupied"
    assert button["one_shot"] is False, "占位按钮不该被消费"
    assert button["owner_mode"] == "everyone", "任何人误点都只得到提示"


def test_free_squares_still_show_their_number():
    state = new_state(MODE_AI, "A")
    button = next(b for r in build_card(state)["rows"] for b in r
                  if b["id"] == "cell4")
    assert button["label"] == "5"
    assert button["action_id"] == "tictactoe.move"


def test_grid_disappears_only_when_the_game_ends():
    state = new_state(MODE_AI, "A")
    state["board"] = board_from("OOO|...|...")
    ids = [b["id"] for r in build_card(state)["rows"] for b in r]
    assert ids == ["quit"], "终局只留控制按钮"


# --- terser card / fewer messages -------------------------------------------

def test_running_card_has_no_ascii_board_in_the_body():
    """The grid is on the buttons; repeating it as text is noise."""
    state = new_state(MODE_AI, "A")
    apply_move(state, 0, "A")
    maybe_ai_move(state)
    body = build_card(state)["markdown"]
    # "·" only ever appears in the rendered grid; the ⭕ here is the status line.
    assert "·" not in body, "对局中不应重复渲染棋盘"
    assert body.splitlines() == ["# 井字棋", "轮到你落子 ⭕"]


def test_final_card_shows_the_board_since_buttons_are_gone():
    state = new_state(MODE_AI, "A")
    state["board"] = board_from("OOO|...|...")
    body = build_card(state)["markdown"]
    assert "⭕" in body, "终局无按钮，正文需保留棋盘"
    assert "你赢了" in body


def test_last_free_cell_is_played_automatically():
    state = new_state(MODE_PVP, "A")
    state["board"] = board_from("OXO|XOX|XO.")
    state["turn"] = AI
    assert autoplay_forced_move(state) == 8
    assert is_full(state["board"])


def test_autoplay_does_nothing_with_two_cells_left():
    state = new_state(MODE_PVP, "A")
    state["board"] = board_from("OXO|XOX|X..")
    assert autoplay_forced_move(state) == -1


def test_autoplay_does_nothing_once_won():
    state = new_state(MODE_AI, "A")
    state["board"] = board_from("OOO|XX.|XXO")
    assert autoplay_forced_move(state) == -1


def test_autoplay_respects_whose_turn_it_is():
    state = new_state(MODE_PVP, "A")
    state["board"] = board_from("OXO|XOX|XO.")
    state["turn"] = HUMAN
    autoplay_forced_move(state)
    assert state["board"][8] == HUMAN
