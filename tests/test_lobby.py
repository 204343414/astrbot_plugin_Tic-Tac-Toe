"""The entry card is one shape shared by every game.

These tests exist because the two games drifted apart once already: 井字棋's
lobby had grown into a four-button menu that also launched 五子棋, while 五子棋
had no card at all. The contract is now structural, so a third game cannot
reintroduce the asymmetry.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from games import gomoku as gk  # noqa: E402
from games import lobby  # noqa: E402
from games import tictactoe as ttt  # noqa: E402

ALL_SPECS = (ttt.SPEC, gk.SPEC)


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda s: s.key)
def test_lobby_is_exactly_five_buttons(spec):
    """Two modes on top, three difficulties below -- nothing else."""
    rows = lobby.build_lobby_card(spec)["rows"]
    assert [len(row) for row in rows] == [2, 3]
    assert sum(len(row) for row in rows) == 5


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda s: s.key)
def test_lobby_labels_are_identical_across_games(spec):
    """The buttons say the same thing everywhere; only the title differs."""
    labels = [b["label"] for row in lobby.build_lobby_card(spec)["rows"] for b in row]
    assert labels == ["🤖 人机对战", "👥 群友对战", "轻松", "✅ 普通", "困难"]


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda s: s.key)
def test_lobby_only_calls_its_own_actions(spec):
    """A game's card never launches another game -- that was the complaint."""
    card = lobby.build_lobby_card(spec)
    others = {s.key for s in ALL_SPECS} - {spec.key}
    for row in card["rows"]:
        for button in row:
            assert button["action_id"].startswith(f"{spec.key}.")
            assert button["action_id"].split(".")[0] not in others


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda s: s.key)
def test_lobby_titles_the_game_and_explains_how_to_move(spec):
    card = lobby.build_lobby_card(spec)
    assert card["markdown"].startswith(f"# {spec.title}")
    assert spec.how_to in card["markdown"]


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda s: s.key)
def test_lobby_marks_the_current_difficulty(spec):
    card = lobby.build_lobby_card(spec, lobby.LEVEL_HARD)
    marked = [b["label"] for r in card["rows"] for b in r if b["label"].startswith("✅")]
    assert marked == ["✅ 困难"]


def test_unknown_difficulty_falls_back_to_normal():
    card = lobby.build_lobby_card(ttt.SPEC, "nightmare")
    assert "✅ 普通" in [b["label"] for r in card["rows"] for b in r]


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda s: s.key)
def test_lobby_is_open_to_everyone_and_reusable(spec):
    """Anyone may start a game, and difficulty is meant to be tapped twice."""
    card = lobby.build_lobby_card(spec)
    assert card["one_shot"] is False
    for row in card["rows"]:
        for button in row:
            assert button.get("owner_mode", "everyone") == "everyone"
            assert button.get("one_shot", False) is False


# --- the waiting card -------------------------------------------------------

@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda s: s.key)
def test_waiting_card_names_the_host_and_offers_join(spec):
    card = lobby.build_waiting_card(spec, "小明", "U1")
    assert "小明" in card["markdown"]
    assert spec.host_mark in card["markdown"]
    assert spec.guest_mark in card["markdown"]
    assert [b["id"] for row in card["rows"] for b in row] == ["join", "cancel"]


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda s: s.key)
def test_only_the_host_may_cancel(spec):
    card = lobby.build_waiting_card(spec, "小明", "U1")
    cancel = card["rows"][0][1]
    assert cancel["owner_mode"] == "specified"
    assert cancel["owner_openid"] == "U1"


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda s: s.key)
def test_join_is_not_one_shot(spec):
    """The Hub burns a one-shot click before the game sees it, so a stray tap
    by the host would lock the seat for everyone."""
    join = lobby.build_waiting_card(spec, "小明", "U1")["rows"][0][0]
    assert join["one_shot"] is False


# --- the games agree with the shared builder --------------------------------

def test_each_game_exposes_its_own_lobby_and_waiting_card():
    assert ttt.build_lobby_card() == lobby.build_lobby_card(ttt.SPEC)
    assert gk.build_lobby_card() == lobby.build_lobby_card(gk.SPEC)

    ttt_state = ttt.new_state(ttt.MODE_PVP, "U1")
    assert ttt.build_waiting_card(ttt_state, "小明") == \
        lobby.build_waiting_card(ttt.SPEC, "小明", "U1")

    gk_state = gk.new_state(gk.MODE_PVP, "U1")
    assert gk.build_waiting_card(gk_state, "小明") == \
        lobby.build_waiting_card(gk.SPEC, "小明", "U1")


def test_both_games_share_one_difficulty_ladder():
    """Each game tunes its own slip rates, but over the *same* three levels."""
    assert set(ttt.LEVEL_SLIP) == set(gk.LEVEL_SLIP) == set(lobby.AI_LEVELS)
    assert (ttt.LEVEL_NORMAL, gk.LEVEL_NORMAL) == (lobby.LEVEL_NORMAL,) * 2


def test_every_lobby_action_is_registered_by_the_plugin():
    """The card is useless if main.py never registers what it points at."""
    source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text("utf-8")
    for spec in ALL_SPECS:
        card = lobby.build_lobby_card(spec)
        waiting = lobby.build_waiting_card(spec, "小明", "U1")
        for row in card["rows"] + waiting["rows"]:
            for button in row:
                assert f'"{button["action_id"]}"' in source, (
                    f"{button['action_id']} 出现在卡片上却没有注册"
                )
        assert f'"{spec.key}.lobby"' in source
