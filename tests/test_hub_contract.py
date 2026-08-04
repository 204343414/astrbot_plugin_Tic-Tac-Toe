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

from games.tictactoe import (  # noqa: E402
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


def test_hub_module_path_is_derived_not_hard_coded():
    """The Hub's top-level package name is its *directory* name, which differs
    between a git clone and a downloaded zip (``...-main``). Hard-coding it
    raises ModuleNotFoundError on perfectly good installs."""
    import re
    source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text("utf-8")
    hard_coded = re.findall(
        r"^\s*from\s+astrbot_plugin_qqofficial_hub[\w.]*\s+import", source, re.M
    )
    assert not hard_coded, f"不得硬编码 Hub 包名: {hard_coded}"
    assert "_hub_module" in source, "应通过 _hub_module 从实例推导包名"


def test_hub_module_helper_resolves_from_instance():
    """Extract the helper without importing main.py, which needs AstrBot."""
    import ast
    import sys
    import types

    source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text("utf-8")
    tree = ast.parse(source)
    func = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_hub_module"
    )
    func.decorator_list = []
    namespace: dict = {"Any": object}
    exec(compile(ast.Module([func], []), "<helper>", "exec"), namespace)

    # AstrBot imports plugins as data.plugins.<dir_name>.main, so the package
    # is three segments deep. Taking split(".")[0] yields a useless "data" and
    # raises "No module named 'data.qqofficial_hub'".
    base = "data.plugins.astrbot_plugin_qqofficial_hub-main"
    leaf = types.ModuleType(f"{base}.qqofficial_hub.action_registry")
    leaf.ActionSpec = object
    sys.modules.update({
        base: types.ModuleType(base),
        f"{base}.qqofficial_hub": types.ModuleType(f"{base}.qqofficial_hub"),
        f"{base}.qqofficial_hub.action_registry": leaf,
    })

    class FakeHub:
        pass

    FakeHub.__module__ = f"{base}.main"
    assert namespace["_hub_module"](FakeHub(), "action_registry").ActionSpec is object


def test_hub_module_helper_rejects_the_first_segment_shortcut():
    """Guard against regressing to ``__module__.split(".")[0]``."""
    source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text("utf-8")
    assert 'split(".")[0]' not in source, "取第一段会得到 data，导致 ModuleNotFoundError"
    assert 'rsplit(".", 1)[0]' in source, "应剥离末尾模块名保留完整包路径"


def test_lobby_does_not_inherit_a_previous_match_session():
    """Retiring an old match must not take a freshly sent lobby card with it.

    end_ephemeral_session() deletes *every* card of a session. If the lobby
    reuses the finished match's session id, cleanup kills it and the very next
    click reports "卡片不存在或已过期".
    """
    from games import tictactoe as game
    board = ep.bind_initiator(
        ep.validate_card(game.build_card(game.new_state(game.MODE_AI, "U1"))), "U1"
    )
    lobby = ep.bind_initiator(ep.validate_card(game.build_lobby_card()), "U1")
    # Distinct sessions are what makes cleanup safe.
    record_a = ep.build_record(ORIGIN, board, "session-old")
    record_b = ep.build_record(ORIGIN, lobby, "session-new")
    assert record_a["session_id"] != record_b["session_id"]


def test_command_entry_retires_stale_match_before_sending():
    source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text("utf-8")
    entry = source[source.index("async def _lobby_from_command"):]
    assert "_retire(origin)" in entry.split("send_ephemeral_card")[0], (
        "开大厅前应先退掉残留对局"
    )


def test_lobby_action_also_retires_first():
    source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text("utf-8")
    action = source[source.index("async def _open_lobby"):]
    action = action[: action.index("return 0")]
    assert "_retire" in action


# --- one self-replacing card, not a pile ------------------------------------
#
# The Hub recalls the card a new one supersedes, but only within the same
# session. Every _send_card used to open a fresh session, so the lobby, each
# difficulty re-render and the waiting card all stayed on screen.

def _main_text() -> str:
    return Path(__file__).resolve().parents[1].joinpath("main.py").read_text("utf-8")


def test_every_card_send_rides_a_session():
    """A send with no session id can never be recalled."""
    source = _main_text()
    for call in ("lobby.build_lobby_card(spec, level)",
                 "ttt.build_waiting_card(state, label)",
                 "gk.build_waiting_card(state, label)",
                 "ach.build_waiting_card(state, label)"):
        window = source[source.index(call): source.index(call) + 260]
        assert "_ui_session(" in window, f"{call} 未挂到会话上，无法被撤回"


def test_the_ui_session_is_per_group_and_per_game():
    """Opening 五子棋 must not recall a 井字棋 card someone is still reading."""
    source = _main_text()
    body = source[source.index("def _ui_session"):]
    body = body[: body.index("async def _send_card")]
    assert "spec.key" in body and "origin" in body


def test_the_board_joins_the_lobby_session():
    """So the first board replaces the lobby card instead of stacking on it."""
    source = _main_text()
    sender = source[source.index("async def _send_board"):]
    sender = sender[: sender.index("# --- actions")]
    assert "_ui_session(context.origin, ttt.SPEC)" in sender


def test_game_over_keeps_the_final_card_clickable():
    """Regression: 「🔄 再来一局」 was a dead button.

    end_ephemeral_session() invalidates every card of the session, so retiring
    on game over made the final board answer 「卡片不存在或已过期」 -- the one
    card players are most likely to click.
    """
    source = _main_text()
    retire = source[source.index("async def _retire"):]
    retire = retire[: retire.index("# --- chat commands")]
    assert "keep_cards" in retire
    assert "if keep_cards:\n            return" in retire

    move = source[source.index("async def _act_move"):]
    move = move[: move.index("async def _act_occupied")]
    assert "keep_cards=True" in move, "终局应保留卡片，否则再来一局点不动"


def test_an_explicit_quit_still_retires_the_cards():
    """Ending a match on purpose must leave no clickable board behind."""
    source = _main_text()
    quit_handler = source[source.index("async def _act_quit"):]
    quit_handler = quit_handler[: quit_handler.index("async def _act_restart")]
    assert "_retire(context.origin)" in quit_handler
    assert "keep_cards" not in quit_handler


# --- the animal chess board card --------------------------------------------
#
# This card is the first one that carries a picture *and* buttons, which is
# the combination QQ refuses for rich media. It only works because the image
# arrives as a hosted Markdown link, so the Hub's validator is the right place
# to prove the shape is legal rather than merely plausible.

from games import animalchess as ac  # noqa: E402

BOARD_URL = "https://favor-prisoner.trycloudflare.com/i/tok3n.png"


def test_every_animalchess_board_passes_hub_validation():
    """Play a real game, validating the card at every single turn.

    A card that fails validation mid-game is far worse than one that fails at
    the start: the match is already live, and players lose a position they
    cannot get back.
    """
    rng = random.Random(11)
    state = ac.new_state(ac.MODE_AI, "A")
    for _ in range(60):
        ep.validate_card(ac.build_board_card(state, BOARD_URL))
        if ac.is_over(state):
            break
        moves = ac.legal_moves(state, state["turn"])
        if not moves:
            break
        animal = rng.choice(sorted(moves))
        direction = rng.choice(moves[animal])[0]
        ac.apply_move(state, animal, direction, "A")
        if not ac.is_over(state):
            ac.maybe_ai_move(state)
    ep.validate_card(ac.build_board_card(state, BOARD_URL))


def test_the_hub_keeps_the_reply_flag_on_every_board_button():
    """``reply`` is what ties a move to the position it was played against.

    It is set on the card, but the Hub is what renders the QQ payload -- so a
    Hub that dropped the flag would leave the game silently unplayable, since
    the move handler requires a quote.
    """
    card = ep.validate_card(ac.build_board_card(
        ac.new_state(ac.MODE_AI, "A"), BOARD_URL))
    for row in card["rows"]:
        for button in row:
            assert button["reply"] is True


def test_board_buttons_render_as_qq_type_2_actions():
    """type=2 appends to the input box without sending. If these came out as
    type=1 the game would become a stream of rate-limited callbacks, which is
    the thing the design deliberately avoids."""
    card = ep.validate_card(ac.build_board_card(
        ac.new_state(ac.MODE_AI, "A"), BOARD_URL))
    rows = ep.to_keyboard_rows(card, "nonce123")
    actions = [b["action"] for row in rows for b in row["buttons"]]
    assert actions, "棋盘卡必须有按钮"
    for action in actions:
        assert action["type"] == 2
        assert action["enter"] is False, "点了不能直接发送，要留给玩家确认"
        assert action["reply"] is True


def test_a_board_with_only_one_piece_left_still_makes_a_valid_card():
    """The endgame is where the animal row is shortest, and an empty row is
    the shape most likely to slip through untested."""
    state = ac.new_state(ac.MODE_AI, "A")
    keep = ac.find_piece(state, ac.RED, ac.LION)
    state["pieces"] = {
        square: piece for square, piece in state["pieces"].items()
        if square == keep or piece[0] == ac.BLUE
    }
    card = ep.validate_card(ac.build_board_card(state, BOARD_URL))
    animal_rows = card["rows"][:-1]
    assert sum(len(r) for r in animal_rows) == 1


def test_no_card_in_this_plugin_ships_an_id_the_hub_would_reject():
    """CARD_ID_RE allows only [A-Za-z0-9_.:-].

    A Chinese button id looks perfectly reasonable next to a Chinese label,
    passes every test written against the game module alone, and then throws
    at send time -- which is how ``dir_上`` reached a card. Sweep every card
    this plugin builds rather than trusting each one to remember.
    """
    from games import gomoku as gk
    from games import lobby
    from games import tictactoe as ttt

    cards = [
        ttt.build_card(ttt.new_state(MODE_AI, "A")),
        ac.build_board_card(ac.new_state(ac.MODE_AI, "A"), BOARD_URL),
        ac.build_lobby_card(),
        ac.build_waiting_card(ac.new_state(ac.MODE_PVP, "A"), "阿甲"),
        gk.build_lobby_card(),
        gk.build_waiting_card(gk.new_state(MODE_PVP, "A"), "阿甲"),
        lobby.build_lobby_card(ttt.SPEC),
        lobby.build_waiting_card(ttt.SPEC, "阿甲", "A"),
    ]
    for card in cards:
        assert ep.CARD_ID_RE.fullmatch(card["id"]), card["id"]
        for row in card["rows"]:
            for button in row:
                given = button.get("id", "")
                assert ep.CARD_ID_RE.fullmatch(given), (
                    f"{card['id']} 的按钮 ID {given!r} 会被 Hub 拒绝")
