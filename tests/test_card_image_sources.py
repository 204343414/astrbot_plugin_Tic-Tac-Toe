"""棋盘卡片走哪条图片通道、失败怎么退 —— 用一个记账用的假 Hub 钉住。

这一层最容易悄悄坏：图片在平台内部转存失败时，发消息接口是**成功的**，
所以 "卡片发出去了" 从来不能证明图能看见。可测的部分是**我们的决策**：
先试素材、再试图床、被拒之后换下一个来源、全部失败就退成无图卡片，
以及素材额度会不会被回收。
"""
import asyncio
import importlib
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

    _api = types.ModuleType("astrbot.api")
    _api.logger = _Logger()
    _api.AstrBotConfig = dict

    class _Anything:
        """万能桩：既是属性、又可调用、还能 ``|``。

        只为了让 ``main.py`` 的装饰器语法能跑完 —— 这里要测的是发卡片那段
        逻辑，不是 AstrBot 的过滤器。
        """

        def __getattr__(self, name):
            return _Anything()

        def __call__(self, *args, **kwargs):
            if len(args) == 1 and callable(args[0]) and not kwargs:
                return args[0]
            return _Anything()

        def __or__(self, other):
            return self

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

# 插件用相对导入（``from .games import …``），所以要先给它一个包外壳，
# 就像 AstrBot 用 ``data.plugins.<目录>.main`` 加载它那样。
_pkg = types.ModuleType("ttt_plugin")
_pkg.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules["ttt_plugin"] = _pkg
plugin_main = importlib.import_module("ttt_plugin.main")

ORIGIN = "qq_official:GroupMessage:G1"


class _Hub:
    """Records what it was asked to do; can be told to reject card sends."""

    def __init__(self, material=True, reject_cards=0, material_error=False):
        self.has_material = material
        self.reject_cards = reject_cards        # 前 N 次带图发送直接抛错
        self.material_error = material_error
        self.cards: list[dict] = []
        self.images: list[bytes] = []
        self.deleted: list[str] = []
        self.host_calls = 0
        if material:
            self.publish_image_material = self._publish_material

    async def _publish_material(self, data, client=None, origin="", file_name=""):
        if self.material_error:
            raise RuntimeError("素材库 500")
        return {"image_id": f"img{len(self.cards)}", "url": "qqbot-media://image/v1/tok",
                "ttl": 7776000}

    async def publish_image_checked(self, data, slot=""):
        self.host_calls += 1
        return "https://img.example/i/tok.png"

    async def send_ephemeral_card(self, origin, card, client=None, session_id="",
                                  event_id=None, msg_id=None, initiator_openid="",
                                  clicker_header="", verify_image=False):
        # 只有“带图”的卡片会因转存失败被拒 —— 没图的卡片没有转存这一步。
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


class _Matches:
    def touch(self, state):
        pass


def make_plugin(hub):
    plug = plugin_main.TicTacToePlugin.__new__(plugin_main.TicTacToePlugin)
    plug._hub = hub
    plug._matches = _Matches()
    plug.context = None
    return plug


def state():
    from games import animalchess as ac

    st = ac.new_state(ac.MODE_PVP, "U1")
    st["phase"] = ac.PHASE_PLAYING
    st["players"] = {ac.RED: "U1", ac.BLUE: "U2"}
    st["turn"] = ac.RED
    return st


def draw(hub):
    plug = make_plugin(hub)
    asyncio.run(plug._send_animalchess_card(ORIGIN, state()))
    return plug


def test_material_is_preferred_over_the_public_image_host():
    """不依赖公网的那条路先试：它出问题的方式最少。"""
    hub = _Hub()
    draw(hub)
    assert len(hub.cards) == 1
    assert "qqbot-media://" in hub.cards[0]["markdown"]


def test_card_send_asks_qq_to_verify_the_image():
    hub = _Hub()
    draw(hub)
    assert hub.cards[0]["_verify"] is True


def test_public_url_is_the_fallback_when_material_upload_fails():
    hub = _Hub(material_error=True)
    draw(hub)
    assert len(hub.cards) == 1
    assert "https://img.example/i/tok.png" in hub.cards[0]["markdown"]


def test_a_rejected_material_card_falls_through_to_the_hosted_url():
    """转存失败会以异常出现（因为开了校验）。换下一个来源，而不是放弃。"""
    hub = _Hub(reject_cards=1)
    draw(hub)
    assert len(hub.cards) == 1
    assert "https://img.example/i/tok.png" in hub.cards[0]["markdown"]


def test_rejected_material_is_released_instead_of_eaten_by_the_quota():
    hub = _Hub(reject_cards=1)
    draw(hub)
    assert hub.deleted == ["img0"]


def test_every_source_failing_degrades_to_a_text_card_plus_one_image():
    """卡片这条路彻底不通时，棋盘换成独立图片消息，按钮照旧能用。"""
    hub = _Hub(reject_cards=99)
    draw(hub)
    assert len(hub.cards) == 1
    assert "![棋盘" not in hub.cards[0]["markdown"]
    assert len(hub.images) == 1


def test_superseded_materials_are_recycled():
    """每走一步一张图，QQ 只有 100 张额度 —— 不回收就是中途全线失败。"""
    hub = _Hub()
    plug = make_plugin(hub)
    for _ in range(4):
        asyncio.run(plug._send_animalchess_card(ORIGIN, state()))
    bucket = plug._materials[ORIGIN]
    assert len(bucket) == plug_main_keep()
    assert len(hub.deleted) == len(hub.cards) - plug_main_keep()


def plug_main_keep():
    return plugin_main.TicTacToePlugin.MATERIAL_KEEP


def test_material_capable_hub_does_not_need_a_reachable_image_host():
    """素材路线不需要公网地址，所以图床探活失败也不该拦住开局。"""
    hub = _Hub()

    async def unreachable():
        return False

    hub.image_host_reachable = unreachable
    plug = make_plugin(hub)
    assert asyncio.run(plug._image_host_or_reason()) == ""


def test_old_hub_without_material_still_needs_the_image_host():
    hub = _Hub(material=False)

    async def unreachable():
        return False

    hub.image_host_reachable = unreachable
    plug = make_plugin(hub)
    assert "图床" in asyncio.run(plug._image_host_or_reason())
