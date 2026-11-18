"""超时认输 + 规则按钮：这两条是"以后所有双人博弈都复用"的规矩。

测试分三层：
  1. 纯规则层（不需要 AstrBot）：规则文案、规则卡片结构；
  2. 插件层（用桩）：超时判负的判定条件、播报、以及对局被收掉；
  3. 契约层：新游戏只要声明 GameSpec.rules 就自动获得规则按钮。
"""
import asyncio
import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from games import animalchess as ach          # noqa: E402
from games import chess as ch                # noqa: E402
from games import gomoku as gk               # noqa: E402
from games import hornchess as hn            # noqa: E402
from games import lobby                      # noqa: E402
from games import tictactoe as ttt           # noqa: E402

ALL_SPECS = (ttt.SPEC, gk.SPEC, ach.SPEC, ch.SPEC, hn.SPEC)


# ---------------------------------------------------------------- 桩
def _install_astrbot_stub():
    if "astrbot" in sys.modules and hasattr(sys.modules["astrbot"], "api"):
        return

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

    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    api.AstrBotConfig = dict
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.filter = _Anything()
    event.MessageChain = lambda *a, **k: SimpleNamespace(chain=[])
    star = types.ModuleType("astrbot.api.star")
    star.Context = object

    class _Star:
        def __init__(self, context=None):
            self.context = context

    star.Star = _Star
    star.register = lambda *a, **k: (lambda cls: cls)
    api.event = event
    api.star = star
    root = types.ModuleType("astrbot")
    root.api = api
    sys.modules["astrbot"] = root
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.star"] = star


_install_astrbot_stub()
if "ttt_plugin" not in sys.modules:
    pkg = types.ModuleType("ttt_plugin")
    pkg.__path__ = [str(Path(__file__).resolve().parents[1])]
    sys.modules["ttt_plugin"] = pkg
main = importlib.import_module("ttt_plugin.main")


# ---------------------------------------------------------------- 规则文案
@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda s: s.key)
def test_every_game_publishes_rules(spec):
    assert spec.rules.strip(), f"{spec.title} 没有规则文案，就会被规则按钮跳过"


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda s: s.key)
def test_rules_mention_the_timeout_rule(spec):
    """用户在群里看规则时，必须先知道 180 秒不走会判负。"""
    assert "超时" in spec.rules
    assert "认输" in spec.rules


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda s: s.key)
def test_rules_card_fits_the_card_limits(spec):
    card = lobby.build_rules_card(spec)
    assert card["id"] == f"{spec.key}_rules"
    assert card["markdown"].startswith(f"# {spec.title}")
    assert len(card["markdown"]) <= 4000, "Hub 的 markdown 上限是 4000 字"
    assert len(card["rows"]) <= 5
    assert all(len(r) <= 5 for r in card["rows"])
    assert card["one_shot"] is True, "规则卡是一次性资料，点完就该收掉"


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda s: s.key)
def test_lobby_offers_the_rules_button(spec):
    # 不对称棋（牛角棋）的第一张卡是「选边」，规则按钮在选完边之后那张上；
    # 但第一步也应该能看规则，否则玩家得先随便选一边才知道规则。
    card = lobby.build_lobby_card(spec)
    if spec.sides:
        card = lobby.build_lobby_card(spec, side=spec.sides[0][0])
    if spec.sides:
        side_card = lobby.build_lobby_card(spec)
        side_labels = [b["label"] for row in side_card["rows"] for b in row]
        assert "📖 规则" in side_labels, "选边卡上也要有规则按钮"
    flat = [b for row in card["rows"] for b in row]
    rules = [b for b in flat if b["action_id"] == f"{spec.key}.rules"]
    assert len(rules) == 1
    assert rules[0]["label"] == "📖 规则"
    assert rules[0]["params"]["spec"] == spec.key


def test_animalchess_rules_spell_out_the_trap_rule():
    """斗兽棋被问过"自家陷阱有没有效果"，规则卡必须把这点写清楚。"""
    rules = ach.SPEC.rules
    assert "对方的" in rules and "陷阱" in rules
    assert "自己的陷阱没有影响" in rules


# ---------------------------------------------------------------- 超时判负
def _plugin(move_timeout=180):
    """绕开 __init__ 构造：其它测试文件会往 sys.modules 里塞自己的 astrbot 桩，
    全量跑时这里的 Star 基类可能已经被换成 object，__init__ 一调就炸。
    这里只装出被测方法真正用到的字段。"""
    plugin = main.TicTacToePlugin.__new__(main.TicTacToePlugin)
    plugin.context = SimpleNamespace()
    plugin.config = {"move_timeout_seconds": move_timeout}
    plugin.move_timeout = int(move_timeout)
    plugin.idle_timeout = max(90, int(move_timeout or 0) + 60)
    plugin._matches = main.MatchRegistry(plugin.idle_timeout)
    plugin._avatars = {}
    plugin._hub = None
    plugin._timeout_task = None
    plugin._levels = {}
    return plugin


def _live_state(spec, turn="R", age=0.0, now=None):
    state = {
        "game": spec.key,
        "turn": turn,
        "phase": "playing",
        "winner": "",
        "labels": {"R": "小明", "B": "小红"},
        "touched_at": (now or __import__("time").time()) - age,
        "session_id": "",
    }
    return state


def test_timeout_defaults_to_180_seconds():
    assert main.DEFAULT_MOVE_TIMEOUT == 180
    source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text("utf-8")
    assert '"move_timeout_seconds", DEFAULT_MOVE_TIMEOUT' in source
    # 而且判负必须早于座位清理，否则挂机局会被静悄悄删掉
    assert "self.move_timeout + 60" in source


def test_no_forfeit_before_the_deadline():
    import time
    plugin = _plugin()
    spec = ach.SPEC
    now = time.time()
    plugin._matches._matches["G1"] = _live_state(spec, age=179, now=now)
    fired = asyncio.run(plugin._sweep_move_timeouts(now=now))
    assert fired == []
    assert plugin._matches.get("G1") is not None


def test_forfeit_after_the_deadline_retires_the_match():
    import time
    plugin = _plugin()
    spec = ach.SPEC
    now = time.time()
    plugin._matches._matches["G1"] = _live_state(spec, turn="B", age=181, now=now)
    announced = []

    async def _fake_announce(origin, text):
        announced.append((origin, text))
        return True

    async def _fake_retire(origin, keep_cards=False):
        plugin._matches.pop(origin)

    plugin._announce = _fake_announce
    plugin._retire = _fake_retire
    fired = asyncio.run(plugin._sweep_move_timeouts(now=now))
    assert fired == ["G1"]
    assert plugin._matches.get("G1") is None, "判负之后必须把对局收掉"
    origin, text = announced[0]
    assert origin == "G1"
    assert spec.title in text and "认输" in text and "180" not in text  # 用了分钟说法
    assert "3 分钟" in text
    assert "小红" in text, "被判负的是 B 方，应该点名"


def test_waiting_matches_never_time_out():
    import time
    plugin = _plugin()
    spec = ach.SPEC
    now = time.time()
    state = _live_state(spec, age=999, now=now)
    state["phase"] = "waiting"
    plugin._matches._matches["G1"] = state
    assert asyncio.run(plugin._sweep_move_timeouts(now=now)) == []


def test_finished_matches_never_time_out():
    import time
    plugin = _plugin()
    spec = ach.SPEC
    now = time.time()
    state = _live_state(spec, age=999, now=now)
    state["winner"] = "R"
    plugin._matches._matches["G1"] = state
    assert asyncio.run(plugin._sweep_move_timeouts(now=now)) == []


def test_zero_disables_auto_forfeit():
    import time
    plugin = _plugin(move_timeout=0)
    now = time.time()
    plugin._matches._matches["G1"] = _live_state(ach.SPEC, age=9999, now=now)
    assert asyncio.run(plugin._sweep_move_timeouts(now=now)) == []


def test_announce_failure_does_not_resurrect_the_match():
    """QQ 主动消息可能被频控或用户关闭；这不能阻止判负。"""
    import time
    plugin = _plugin()
    now = time.time()
    plugin._matches._matches["G1"] = _live_state(ach.SPEC, age=181, now=now)

    async def _boom(origin, text):
        return False

    async def _fake_retire(origin, keep_cards=False):
        plugin._matches.pop(origin)

    plugin._announce = _boom
    plugin._retire = _fake_retire
    assert asyncio.run(plugin._sweep_move_timeouts(now=now)) == ["G1"]
    assert plugin._matches.get("G1") is None


# ---------------------------------------------------------------- 复用约定
def test_new_game_gets_rules_button_for_free():
    """以后所有双人博弈都复用这套：声明 GameSpec.rules 就自动有规则按钮。"""
    spec = lobby.GameSpec(
        key="reversi", title="黑白棋", how_to="点格子落子。",
        host_mark="⚫", guest_mark="⚪", rules="**规则**：夹住就翻面。",
    )
    card = lobby.build_lobby_card(spec)
    labels = [b["label"] for row in card["rows"] for b in row]
    assert "📖 规则" in labels
    rules_card = lobby.build_rules_card(spec)
    assert "夹住就翻面" in rules_card["markdown"]


def test_game_without_rules_has_no_rules_button():
    """没写规则的游戏保持旧行为，不能凭空多出一个点了没反应的按钮。"""
    spec = lobby.GameSpec(
        key="othello", title="某棋", how_to="...", host_mark="⚫", guest_mark="⚪",
    )
    labels = [b["label"] for row in lobby.build_lobby_card(spec)["rows"] for b in row]
    assert "📖 规则" not in labels


def test_every_spec_is_wired_into_the_plugin():
    keys = {sp.key for sp in main.GAME_SPECS}
    assert keys == {sp.key for sp in ALL_SPECS}


def test_rule_actions_are_registered_for_each_game():
    source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text("utf-8")
    assert 'f"{spec.key}.rules"' in source
    assert 'f"{spec.key}.rules_ack"' in source
