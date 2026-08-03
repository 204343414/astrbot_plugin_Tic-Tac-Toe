"""One match per group, idle expiry, and per-match avatar caching."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from games.session import AvatarCache, MatchRegistry  # noqa: E402


def test_one_match_per_group():
    reg = MatchRegistry(90)
    reg.start("G1", {"display_name": "五子棋"}, now=1000)
    assert reg.busy_reason("G1", now=1010), "同群第二局应被拒绝"
    assert reg.busy_reason("G2", now=1010) == "", "其它群不受影响"


def test_busy_reason_tells_how_long_until_release():
    reg = MatchRegistry(90)
    reg.start("G1", {"display_name": "五子棋"}, now=1000)
    text = reg.busy_reason("G1", now=1030)
    assert "五子棋" in text and "60 秒" in text


def test_match_expires_after_the_idle_timeout():
    reg = MatchRegistry(90)
    reg.start("G1", {}, now=1000)
    assert reg.get("G1", now=1089) is not None
    assert reg.get("G1", now=1091) is None, "超时后应自动释放"


def test_touch_postpones_expiry():
    reg = MatchRegistry(90)
    state = reg.start("G1", {}, now=1000)
    reg.touch(state, now=1080)
    assert reg.get("G1", now=1150) is not None, "落子应刷新计时"
    assert reg.get("G1", now=1200) is None


def test_zero_timeout_disables_expiry():
    reg = MatchRegistry(0)
    reg.start("G1", {}, now=0)
    assert reg.get("G1", now=10 ** 9) is not None
    assert reg.sweep(now=10 ** 9) == []


def test_sweep_returns_dead_matches_for_cleanup():
    reg = MatchRegistry(90)
    reg.start("G1", {}, now=1000)
    reg.start("G2", {}, now=1190)
    dead = reg.sweep(now=1200)
    assert [origin for origin, _ in dead] == ["G1"]
    assert reg.get("G2", now=1200) is not None


def test_expired_slot_can_be_reused():
    reg = MatchRegistry(90)
    reg.start("G1", {"display_name": "旧局"}, now=1000)
    assert reg.busy_reason("G1", now=1200) == "", "过期后应释放对局位"
    reg.start("G1", {"display_name": "新局"}, now=1200)
    assert reg.get("G1", now=1200)["display_name"] == "新局"


def test_avatars_are_downloaded_once_per_match():
    async def scenario():
        cache = AvatarCache()
        calls = []

        async def fetch(openid):
            calls.append(openid)
            return b"PNG" if openid == "A" else None

        first = await cache.get_many({"B": "A", "W": "X"}, fetch)
        second = await cache.get_many({"B": "A", "W": "X"}, fetch)
        assert calls == ["A", "X"], "重绘不应重复下载"
        assert first == second == {"B": b"PNG"}, "失败的头像应被跳过而非重试"
    asyncio.run(scenario())


def test_avatar_fetch_errors_do_not_propagate():
    async def scenario():
        cache = AvatarCache()

        async def boom(openid):
            raise RuntimeError("network down")

        assert await cache.get_many({"B": "A"}, boom) == {}
    asyncio.run(scenario())


# --- wiring guarantees ------------------------------------------------------

def _main_source() -> str:
    return Path(__file__).resolve().parents[1].joinpath("main.py").read_text("utf-8")


def test_gomoku_requires_quoting_the_board():
    """A bare "H8" in conversation must not be treated as a move."""
    source = _main_source()
    handler = source[source.index("async def gomoku_move_by_quote"):]
    handler = handler[: handler.index("@staticmethod")]
    assert "self._quotes(event, board_id)" in handler
    assert "board_msg_id" in handler


def test_move_falls_back_when_qq_reports_no_message_id():
    """Blocking play is worse than accepting an un-quoted coordinate."""
    source = _main_source()
    handler = source[source.index("async def gomoku_move_by_quote"):]
    assert "if board_id and not self._quotes" in handler, (
        "拿不到消息 id 时应降级放行，而不是让人无法落子"
    )


def test_only_one_gomoku_match_per_group():
    source = _main_source()
    starter = source[source.index("async def _start_gomoku"):]
    starter = starter[: starter.index("async def _fetch_avatars")]
    assert "busy_reason" in starter, "开局前应检查本群是否已有对局"


def test_board_send_touches_the_idle_deadline():
    source = _main_source()
    sender = source[source.index("async def _send_gomoku_board"):]
    sender = sender[: sender.index("async def _retire")]
    assert "_matches.touch(state)" in sender, "每次刷新棋盘都应续期"


def test_config_exposes_only_the_shared_timeout():
    import json
    schema = json.loads(
        Path(__file__).resolve().parents[1].joinpath("_conf_schema.json")
        .read_text("utf-8")
    )
    assert list(schema) == ["idle_timeout_seconds"], "配置应只保留通用项"
    assert schema["idle_timeout_seconds"]["default"] == 90
