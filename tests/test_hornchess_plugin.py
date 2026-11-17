"""牛角棋接进插件这一层：选边两步入口、AI 执任一方、群友分边、按钮走棋。

规则本身（棋盘、穷举、平衡性）在 test_hornchess.py 里钉死了；这里管的是
"点得动、分得对"：

* ``/牛角棋`` 先问执牛还是执人，选完才出人机／群友／难度；
* 玩家选「🐮 选牛」时 AI 要会走那两个赶牛的人（否则卡片发出来就卡住）；
* 群友对战里，加入的人坐**对面**那一方。
"""
import asyncio
import importlib
import random
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if "astrbot" not in sys.modules:
    class _Logger:
        def __getattr__(self, name):
            return lambda *a, **k: None

    class _Anything:
        def __getattr__(self, name):
            return _Anything()

        def __call__(self, *args, **kwargs):
            if len(args) == 1 and callable(args[0]) and not kwargs:
                return args[0]
            return _Anything()

        def __or__(self, other):
            return self

    _api = types.ModuleType("astrbot.api")
    _api.logger = _Logger()
    _api.AstrBotConfig = dict
    _event = types.ModuleType("astrbot.api.event")
    _event.AstrMessageEvent = object
    _event.filter = _Anything()
    _star = types.ModuleType("astrbot.api.star")
    _star.Context = object
    _star.Star = object
    _star.register = lambda *a, **k: (lambda cls: cls)
    _api.event = _event
    _api.star = _star
    _root = types.ModuleType("astrbot")
    _root.api = _api
    sys.modules["astrbot"] = _root
    sys.modules["astrbot.api"] = _api
    sys.modules["astrbot.api.event"] = _event
    sys.modules["astrbot.api.star"] = _star

_pkg = types.ModuleType("ttt_plugin")
_pkg.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules["ttt_plugin"] = _pkg
plugin_main = importlib.import_module("ttt_plugin.main")

from games import hornchess as hc  # noqa: E402
from games import lobby  # noqa: E402

ORIGIN = "qq_official:GroupMessage:G1"
HOST = "HOSTUSER"
GUEST = "GUESTUSER"


class _Hub:
    def __init__(self):
        self.cards: list[dict] = []
        self.images: list = []
        self.has_material = True

    async def publish_image_checked(self, data, slot=""):
        return "https://img.example/i/tok.png"

    async def send_ephemeral_card(self, origin, card, client=None, session_id="",
                                  event_id=None, msg_id=None, initiator_openid="",
                                  clicker_header="", verify_image=False):
        self.cards.append(dict(card))
        return "card-1"

    async def send_image_message(self, origin, image, client=None, event_id=None,
                                 msg_id=None):
        self.images.append(image)
        return "msg-1"

    async def delete_image_material(self, image_id, client=None, origin=""):
        return True

    def actions(self):
        return SimpleNamespace(register=lambda *a, **k: None,
                               unregister_owner=lambda *a, **k: None)


class _Matches:
    """最小的对局表：只记一个群一个局面。"""

    def __init__(self):
        self.one = None

    def busy_reason(self, origin):
        return "" if self.one is None else "还有一局没打完"

    def start(self, origin, state):
        self.one = state

    def get(self, origin):
        return self.one

    def pop(self, origin):
        state, self.one = self.one, None
        return state

    def touch(self, state):
        return None


def make_plugin():
    plug = plugin_main.TicTacToePlugin.__new__(plugin_main.TicTacToePlugin)
    plug._hub = _Hub()
    # 卡片走的是被动回复：真 Hub 会把点击的交互 id 转成 event_id。这里给个桩，
    # 免得测试去 import 真正的 Hub 包（那东西只存在于服务器上）。
    plug._hub_module = staticmethod(
        lambda hub, name: SimpleNamespace(passive_event_id=lambda interaction: "evt-1",
                                          ActionSpec=lambda **kw: SimpleNamespace(**kw)))
    plug._matches = _Matches()
    plug._levels = {}
    plug._materials = {}
    plug._avatars = {}
    plug._sessions = {}
    plug._state_lock = asyncio.Lock() if hasattr(asyncio, "Lock") else None
    return plug


def context(openid=HOST):
    return SimpleNamespace(
        origin=ORIGIN, member_openid=openid, client=None, interaction=None,
        _label=None,
    )


# --- 两步入口 ---------------------------------------------------------------

def test_the_entry_card_asks_for_a_side_first():
    card = lobby.build_lobby_card(hc.SPEC, "normal")
    labels = [b["label"] for row in card["rows"] for b in row]
    assert len(labels) == 2
    assert any("牛" in label for label in labels)
    assert any("人" in label for label in labels)
    # 第一步不许出现人机／群友
    assert not any("人机" in label or "群友" in label for label in labels)
    for row in card["rows"]:
        for button in row:
            assert button["action_id"] == lobby.action_id(hc.SPEC, "pick_side")
            assert button["params"]["side"] in (hc.HUMANS, hc.BULL)


@pytest.mark.parametrize("side", [hc.BULL, hc.HUMANS])
def test_after_picking_a_side_the_usual_buttons_appear(side):
    card = lobby.build_lobby_card(hc.SPEC, "normal", side)
    rows = card["rows"]
    labels = [b["label"] for row in rows for b in row]
    assert any("人机" in label for label in labels)
    assert any("群友" in label for label in labels)
    assert any("重新选边" in label for label in labels)
    # 选边要跟着每个按钮走，否则点难度就被打回第一步
    for row in rows:
        for button in row:
            if button["id"] == "back":
                continue
            assert button["params"].get("side") == side
    assert hc.SIDE_LABELS[side] in card["markdown"]


def test_every_game_without_sides_keeps_the_single_step_lobby():
    """别的棋种没有正反两方，入口不能被这次改动带歪。"""
    from games import tictactoe as ttt

    card = lobby.build_lobby_card(ttt.SPEC, "normal")
    labels = [b["label"] for row in card["rows"] for b in row]
    assert any("人机" in label for label in labels)
    assert not any("重新选边" in label for label in labels)


def test_the_pick_side_action_turns_the_page():
    plug = make_plugin()
    code = asyncio.run(plug._act_pick_side_hornchess(context(), {"side": hc.BULL}))
    assert code == 0
    card = plug._hub.cards[-1]
    labels = [b["label"] for row in card["rows"] for b in row]
    assert any("人机" in label for label in labels)


def test_the_pick_side_action_rejects_a_bogus_side():
    plug = make_plugin()
    assert asyncio.run(plug._act_pick_side_hornchess(context(), {"side": "猫"})) == 3


def test_switching_difficulty_does_not_lose_the_chosen_side():
    plug = make_plugin()
    code = asyncio.run(plug._act_set_level_hornchess(
        context(), {"level": "hard", "side": hc.BULL}))
    assert code == 0
    card = plug._hub.cards[-1]
    buttons = [b for row in card["rows"] for b in row if b["id"].startswith("start")]
    assert buttons and all(b["params"]["side"] == hc.BULL for b in buttons)
    assert plug._levels[(ORIGIN, hc.SPEC.key)] == "hard"


# --- 人机：AI 执任一方 ------------------------------------------------------

def test_choosing_the_bull_makes_the_ai_open_as_the_farmers():
    """人先走是规矩；玩家选牛，就轮到 AI 那两个赶牛的人先动一步。"""
    plug = make_plugin()
    assert asyncio.run(plug._start_hornchess(context(), hc.MODE_AI, hc.BULL)) == 0
    state = plug._matches.get(ORIGIN)
    assert state["human_side"] == hc.BULL
    assert state["turn"] == hc.BULL                 # 已经推进回玩家
    assert state["players"][hc.BULL] == HOST
    assert state["players"][hc.HUMANS] == ""        # 对面是 AI
    card = plug._hub.cards[-1]
    labels = [b["label"] for row in card["rows"] for b in row]
    # 执牛时走法按钮就是落点本身（B1／B2），不认识「→」这种长写法
    assert {"B1", "B2"} <= set(labels)
    assert all("→" not in label for label in labels)


def test_the_ai_can_play_the_farmers():
    state = hc.new_state(hc.MODE_AI, HOST, "hard", hc.BULL)
    assert state["turn"] == hc.HUMANS
    before = list(state["humans"])
    move = hc.ai_move(state, random.Random(4))
    assert move is not None
    assert state["humans"] != before
    assert state["turn"] == hc.BULL


def test_clicking_the_ai_side_is_refused():
    state = hc.new_state(hc.MODE_AI, HOST, "hard", hc.BULL)
    assert state["turn"] == hc.HUMANS               # AI 的两个人在走
    move = hc.legal_moves(state)[0]
    assert hc.apply_move(state, move[1], move[2], HOST) != ""


def test_a_human_move_lets_the_ai_reply_whichever_side_the_player_took():
    plug = make_plugin()
    asyncio.run(plug._start_hornchess(context(), hc.MODE_AI, hc.BULL))
    state = plug._matches.get(ORIGIN)
    move = hc.legal_moves(state)[0]
    assert asyncio.run(plug._act_hornchess_move(
        context(), {"frm": move[1], "to": move[2]})) == 0
    assert state["turn"] == hc.BULL                 # AI 应了一手，又轮回玩家
    assert state["plies"] >= 2


# --- 群友对战：分边 ---------------------------------------------------------

def test_the_guest_sits_on_the_other_side():
    plug = make_plugin()
    assert asyncio.run(plug._start_hornchess(context(), hc.MODE_PVP, hc.BULL)) == 0
    state = plug._matches.get(ORIGIN)
    assert state["phase"] == hc.PHASE_WAITING
    assert state["players"] == {hc.BULL: HOST, hc.HUMANS: ""}

    assert asyncio.run(plug._act_hornchess_join(context(GUEST), {})) == 0
    assert state["players"] == {hc.BULL: HOST, hc.HUMANS: GUEST}
    assert state["phase"] == hc.PHASE_PLAYING

    # 发起人不能自己坐对面的位子
    plug2 = make_plugin()
    asyncio.run(plug2._start_hornchess(context(), hc.MODE_PVP, hc.BULL))
    assert asyncio.run(plug2._act_hornchess_join(context(HOST), {})) == 4


def test_the_waiting_card_names_the_seats_the_right_way_round():
    state = hc.new_state(hc.MODE_PVP, HOST, "normal", hc.BULL)
    card = hc.build_waiting_card(state, "小明")
    assert hc.MARKS[hc.BULL] in card["markdown"]
    assert hc.MARKS[hc.HUMANS] in card["markdown"]


def test_both_seats_can_move_in_pvp_and_outsiders_cannot():
    state = hc.new_state(hc.MODE_PVP, HOST, "normal", hc.BULL)
    state["phase"] = hc.PHASE_PLAYING
    state["players"] = {hc.BULL: HOST, hc.HUMANS: GUEST}
    state["turn"] = hc.HUMANS
    move = hc.legal_moves(state)[0]
    assert hc.apply_move(state, move[1], move[2], HOST) != ""     # 人那边不是他
    assert hc.apply_move(state, move[1], move[2], GUEST) == ""
    assert state["turn"] == hc.BULL
