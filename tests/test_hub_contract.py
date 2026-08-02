"""Contract tests against the real Hub validator.

These are the tests that justify keeping the game in its own plugin: if the
board can be driven purely through the Hub's public ephemeral-card API, the API
is genuinely general. Skipped when the Hub is not importable.
"""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from game import (  # noqa: E402
    MODE_AI, MODE_PVP, apply_move, build_card, is_over, maybe_ai_move, new_state,
)

ep = pytest.importorskip(
    "qqofficial_hub.ephemeral",
    reason="需要 astrbot_plugin_qqofficial_hub 在 PYTHONPATH 上",
)

ORIGIN = "qq_official:GroupMessage:G1"


def test_every_board_state_passes_hub_validation():
    rng = random.Random(3)
    state = new_state(MODE_AI, "A")
    while not is_over(state["board"]):
        ep.validate_card(build_card(state))
        free = [i for i, c in enumerate(state["board"]) if not c]
        apply_move(state, rng.choice(free), "A")
        maybe_ai_move(state, rng)
    ep.validate_card(build_card(state))          # final board renders too


def test_hub_blocks_another_player_from_moving():
    state = new_state(MODE_AI, "A")
    card = ep.bind_initiator(ep.validate_card(build_card(state)), "A")
    record = ep.build_record(ORIGIN, card, "s1")
    with pytest.raises(ep.EphemeralError) as err:
        ep.resolve_click(record, ORIGIN, "cell0", "B")
    assert err.value.code == ep.CODE_FORBIDDEN


def test_hub_blocks_replaying_the_same_square():
    state = new_state(MODE_AI, "A")
    card = ep.bind_initiator(ep.validate_card(build_card(state)), "A")
    record = ep.build_record(ORIGIN, card, "s1")
    button = ep.resolve_click(record, ORIGIN, "cell0", "A")
    ep.apply_consumption(record, button)
    with pytest.raises(ep.EphemeralError) as err:
        ep.resolve_click(record, ORIGIN, "cell0", "A")
    assert err.value.code == ep.CODE_DUPLICATE
    # other squares on the same card stay playable
    assert ep.resolve_click(record, ORIGIN, "cell4", "A")["id"] == "cell4"


def test_pvp_open_seat_card_is_clickable_by_anyone():
    state = new_state(MODE_PVP, "A")
    apply_move(state, 0, "A")
    card = ep.validate_card(build_card(state))
    # owner_mode="everyone" must survive bind_initiator without an initiator
    bound = ep.bind_initiator(card, "")
    record = ep.build_record(ORIGIN, bound, "s1")
    assert ep.resolve_click(record, ORIGIN, "cell4", "B")["id"] == "cell4"


def test_move_params_survive_the_round_trip():
    state = new_state(MODE_AI, "A")
    card = ep.bind_initiator(ep.validate_card(build_card(state)), "A")
    record = ep.build_record(ORIGIN, card, "s1")
    button = ep.resolve_click(record, ORIGIN, "cell7", "A")
    assert button["params"] == {"cell": 7}
    assert button["action_id"] == "tictactoe.move"


def test_button_data_does_not_leak_the_move():
    """Real parameters must stay in the server-side snapshot."""
    card = ep.validate_card(build_card(new_state(MODE_AI, "A")))
    for row in ep.to_keyboard_rows(card, "NONCE"):
        for button in row["buttons"]:
            assert "cell\":" not in button["action"]["data"]
            assert button["action"]["type"] == 1
