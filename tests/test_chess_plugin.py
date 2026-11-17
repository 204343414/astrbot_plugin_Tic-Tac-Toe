"""国际象棋接进插件这一层：开局、入座、引用走棋、卡片出图。

规则在 test_chess.py 里已经用 perft 钉死了；这里管的是"接得上、接得对"——
按钮 action 的 id 能不能对上 Hub，走法会不会被别的处理器吞掉，棋盘出不来
的时候有没有兜底。
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
        """装饰器语法桩：既是属性、又可调用、还能 ``|``。"""

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
from games import chess as ch  # noqa: E402

ORIGIN = "qq_official:GroupMessage:G1"
WHITE_OPENID = "WHITEUSER"
BLACK_OPENID = "BLACKUSER"


class _Hub:
    def __init__(self, material=True, reject_cards=0):
        self.has_material = material
        self.reject_cards = reject_cards
        self.cards: list[dict] = []
        self.images: list = []
        self.deleted: list[str] = []
        if material:
            self.publish_image_material = self._publish_material

    async def _publish_material(self, data, client=None, origin="", file_name=""):
        return {"image_id": f"img{len(self.cards)}",
                "url": "qqbot-media://image/v1/tok", "ttl": 7776000}

    async def publish_image_checked(self, data, slot=""):
        return "https://img.example/i/tok.png"

    async def send_ephemeral_card(self, origin, card, client=None, session_id="",
                                  event_id=None, msg_id=None, initiator_openid="",
                                  clicker_header="", verify_image=False):
        if self.reject_cards > 0 and "![" in str(card.get("markdown", "")):
            self.reject_cards -= 1
            raise RuntimeError("304010 图片转存错误")
        self.cards.append(dict(card, _verify=verify_image))
        return "card-1"

    async def send_image_message(self, origin, image, client=None, event_id=None,
                                 msg_id=None):
        self.images.append(image)
        return "msg-1"

    async def delete_image_material(self, image_id, client=None, origin=""):
        self.deleted.append(image_id)
        return True

    def actions(self):
        return SimpleNamespace(register=lambda *a, **k: None,
                               unregister_owner=lambda *a, **k: None)


class _Matches:
    def touch(self, state):
        return None


def make_plugin(hub=None):
    plug = plugin_main.TicTacToePlugin.__new__(plugin_main.TicTacToePlugin)
    plug._hub = hub or _Hub()
    plug._matches = _Matches()
    plug._levels = {}
    plug._materials = {}
    return plug


def playing_pvp(level="normal"):
    state = ch.new_state(ch.MODE_PVP, WHITE_OPENID, level)
    state["phase"] = ch.PHASE_PLAYING
    state["players"] = {ch.WHITE: WHITE_OPENID, ch.BLACK: BLACK_OPENID}
    return state


# --- 契约：action id 必须和 GameSpec 推出来的一致 ---------------------------

def test_every_action_the_lobby_can_emit_is_registered():
    """大厅卡片的按钮是从 GameSpec 推出来的 id，注册表少一个就点不动。

    直接跑一遍 ``_register_actions`` 并把注册的东西记下来——比用正则去源码里
    抠字符串可靠，因为那只能证明代码里有这个字符串，证明不了它真的被注册。
    """
    recorded: list[str] = []

    class _Registry:
        def register(self, spec):
            recorded.append(spec.action_id)

    class _HubWithActions(_Hub):
        def __init__(self):
            super().__init__()
            self.actions = _Registry()

    plug = make_plugin(_HubWithActions())
    # _register_actions 会按 Hub 所在包去 import 子模块；这里的假 Hub 不在任何
    # 包里，所以直接把那个模块换成桩，只留下 ActionSpec 这一件需要的东西。
    plug._hub_module = staticmethod(
        lambda hub, name: SimpleNamespace(ActionSpec=lambda **kw: SimpleNamespace(**kw)))
    plug._register_actions()

    needed = {f"{ch.SPEC.key}.{name}"
              for name in ("lobby", "start_ai", "start_pvp", "set_level",
                           "join", "quit")}
    assert needed <= set(recorded), needed - set(recorded)


# --- 走法解析接到插件上 -----------------------------------------------------

def test_card_declares_the_chess_size():
    card = ch.build_board_card(ch.new_state(ch.MODE_AI, "U1"), "")
    assert card["id"] == "chess_board"


# --- 出图：素材优先、图床兜底、全挂了降级 -----------------------------------

def draw(hub, state=None):
    plug = make_plugin(hub)
    asyncio.run(plug._send_chess_card(ORIGIN, state or playing_pvp()))
    return plug


def test_chess_board_goes_through_the_material_store_first():
    hub = _Hub()
    draw(hub)
    assert len(hub.cards) == 1
    assert "qqbot-media://" in hub.cards[0]["markdown"]
    assert hub.cards[0]["_verify"] is True


def test_chess_board_falls_back_to_the_public_url():
    hub = _Hub(material=False)
    draw(hub)
    assert "https://img.example/i/tok.png" in hub.cards[0]["markdown"]


def test_chess_board_degrades_to_text_plus_image_when_every_source_fails():
    hub = _Hub(reject_cards=9)
    draw(hub)
    assert len(hub.cards) == 1
    assert "![棋盘" not in hub.cards[0]["markdown"]
    assert len(hub.images) == 1


def test_chess_recycles_material_the_same_way_animalchess_does():
    hub = _Hub()
    plug = make_plugin(hub)
    for _ in range(4):
        asyncio.run(plug._send_chess_card(ORIGIN, playing_pvp()))
    assert len(plug._materials[ORIGIN]) == plugin_main.TicTacToePlugin.MATERIAL_KEEP
    assert hub.deleted


# --- AI 走完要把局面推进到玩家回合 -----------------------------------------

def test_ai_move_advances_the_game_and_is_legal():
    state = ch.new_state(ch.MODE_AI, WHITE_OPENID, "normal")
    move = ch.resolve_text(state, "e2e4")
    assert ch.apply_move(state, move, WHITE_OPENID) == ""
    reply = ch.maybe_ai_move(state, random.Random(11))
    assert reply is not None
    assert state["turn"] == ch.WHITE            # 回到玩家
    assert state["last_move"]["piece"].startswith(ch.BLACK)
    assert len([p for p in state["board"] if p]) == 32
