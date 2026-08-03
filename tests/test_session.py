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


def _quote_handler(source: str) -> str:
    """The body of gomoku_move_by_quote, up to the next decorated member."""
    body = source[source.index("async def gomoku_move_by_quote"):]
    end = body.index("    @filter.", 1)
    return body[:end]


def test_gomoku_requires_quoting_something():
    """A bare "H8" in conversation must not be treated as a move."""
    source = _main_source()
    handler = _quote_handler(source)
    assert "quoted = quoted_message_ids(event)" in handler
    assert "if not quoted:" in handler, "没有引用就不是落子"


def test_a_quoted_move_is_never_discarded_over_an_id_mismatch():
    """Requiring an exact id match silently ate real moves.

    The player quoted the board and typed H8, and nothing happened: the id QQ
    echoed when the picture was sent did not equal the id it reported back on
    the quote. Losing someone's turn to that is far worse than acting on a
    quote of the wrong message, so a mismatch is logged, not dropped.
    """
    source = _main_source()
    handler = _quote_handler(source)
    mismatch = handler[handler.index("if board_id and board_id not in quoted:"):]
    mismatch = mismatch[: mismatch.index("event.stop_event()")]
    assert "return" not in mismatch, "id 对不上时不能吞掉这一手"
    assert "logger" in mismatch, "id 对不上应留下诊断"


def test_quoted_ids_reads_every_reply_component():
    """Unit-level: the helper reports ids rather than a yes/no verdict."""
    from types import SimpleNamespace

    from games.session import quoted_message_ids

    def event_with(*components):
        return SimpleNamespace(message_obj=SimpleNamespace(message=list(components)))

    reply = SimpleNamespace(type="Reply", id="BOARD_1")
    plain = SimpleNamespace(type="Plain", text="H8")
    assert quoted_message_ids(event_with(plain)) == []
    assert quoted_message_ids(event_with(reply, plain)) == ["BOARD_1"]
    # A Reply that carries no usable id still counts as "they quoted something".
    assert quoted_message_ids(event_with(SimpleNamespace(type="Reply", id=""))) == ["?"]
    assert quoted_message_ids(SimpleNamespace(message_obj=None)) == []


def test_only_one_gomoku_match_per_group():
    source = _main_source()
    starter = source[source.index("async def _start_gomoku"):]
    starter = starter[: starter.index("async def _fetch_avatars")]
    assert "busy_reason" in starter, "开局前应检查本群是否已有对局"


def test_board_send_touches_the_idle_deadline():
    source = _main_source()
    sender = source[source.index("async def _send_picture_board"):]
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


# --- a failed upload must not lose the move ---------------------------------
#
# QQ answered "系统繁忙，请稍后重试" (its own HTTP 500) mid-game. The stone was
# already on the board, so the match silently ran ahead of the last picture
# players could see.

def test_a_failed_send_reports_the_move_was_kept():
    handler = _quote_handler(_main_source())
    failure = handler[handler.index("except Exception as exc:"):]
    assert "落子已记录" in failure, "发图失败时要说明这一手仍然算数"
    assert "/棋盘" in failure, "要告诉玩家怎么把棋盘找回来"


def test_a_redraw_command_exists_for_recovering_from_a_failed_send():
    source = _main_source()
    assert '@filter.command("棋盘"' in source
    assert "async def redraw_board" in source


def test_qq_server_errors_are_not_reported_as_a_plugin_crash():
    """"ServerError: 系统繁忙" is Tencent's, and should not read like our bug."""
    source = _main_source()
    describe = source[source.index("def _describe("):]
    describe = describe[: describe.index("@filter.")]
    assert "ServerError" in describe
    assert "QQ 服务端繁忙" in describe
