"""Rules, AI and card rendering. No AstrBot needed."""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from game import (  # noqa: E402
    AI, HUMAN, MODE_AI, MODE_PVP, ai_move, apply_move, build_card, is_full,
    is_over, maybe_ai_move, new_state, render_board_text, winner,
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

def test_card_only_offers_free_cells():
    state = new_state(MODE_AI, "A")
    state["board"] = board_from("OX.|...|...")
    ids = {b["id"] for row in build_card(state)["rows"] for b in row}
    assert "cell0" not in ids and "cell1" not in ids
    assert "cell2" in ids and "cell8" in ids


def test_every_cell_button_is_one_shot():
    state = new_state(MODE_AI, "A")
    for row in build_card(state)["rows"]:
        for button in row:
            if button["id"].startswith("cell"):
                assert button["one_shot"] is True, "a square must not be replayable"


def test_cells_are_locked_to_the_player_whose_turn_it_is():
    state = new_state(MODE_AI, "A")
    cells = [b for row in build_card(state)["rows"] for b in row
             if b["id"].startswith("cell")]
    assert all(b["owner_openid"] == "A" for b in cells)
    assert all(b["owner_mode"] == "specified" for b in cells)


def test_pvp_lock_follows_the_turn():
    state = new_state(MODE_PVP, "A")
    apply_move(state, 0, "A")
    state["players"][AI] = "B"
    cells = [b for row in build_card(state)["rows"] for b in row
             if b["id"].startswith("cell")]
    assert all(b["owner_openid"] == "B" for b in cells)


def test_open_seat_lets_anyone_click():
    """Before the second player joins, the board must not be locked to nobody."""
    state = new_state(MODE_PVP, "A")
    apply_move(state, 0, "A")          # now it is X's turn, seat still empty
    cells = [b for row in build_card(state)["rows"] for b in row
             if b["id"].startswith("cell")]
    assert all(b["owner_mode"] == "everyone" for b in cells)


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
