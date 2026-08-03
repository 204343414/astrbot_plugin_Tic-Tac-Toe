"""Tic-tac-toe on QQ Official cards, driven entirely by button clicks.

This plugin is a worked example of the Hub's ephemeral-card contract: it owns
its rules and state, and touches QQ only through the Hub's public API. If it
ever needs a Hub internal, that is a gap in the Hub's API, not a reason to
reach inside.

Design constraints that shaped it (see the Hub's docs/EPHEMERAL_CARDS.md):

* QQ group proactive messages are capped at 4 per month, so **every** board
  update must be a passive reply to the click that caused it;
* a passive reply is valid for 5 minutes and at most 5 messages per event, so
  one click produces exactly one card;
* one-shot buttons and owner locks are enforced by the Hub server-side, which
  is what makes simultaneous taps safe.
"""
from __future__ import annotations

from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .games import gomoku as gk
from .games.session import AvatarCache, MatchRegistry
from .game import (
    AI,
    AI_LEVELS,
    HUMAN,
    LEVEL_LABELS,
    LEVEL_NORMAL,
    MODE_AI,
    MODE_PVP,
    PHASE_PLAYING,
    apply_move,
    autoplay_forced_move,
    build_card,
    build_lobby_card,
    build_waiting_card,
    is_over,
    maybe_ai_move,
    new_state,
)

PLUGIN_NAME = "astrbot_plugin_tictactoe"
HUB_NAME = "astrbot_plugin_qqofficial_hub"
OWNER = PLUGIN_NAME


@register(
    PLUGIN_NAME,
    "204343414",
    "QQ 官方机器人棋类小游戏：井字棋与五子棋，支持群友对战与 AI 对战。",
    "0.2.0",
    "https://github.com/204343414/astrbot_plugin_Tic-Tac-Toe",
)
class TicTacToePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.config = config or {}
        # origin -> game state. In-memory on purpose: an unfinished match is
        # not worth persisting across restarts, and the cards expire anyway.
        self._levels: dict[str, str] = {}
        # Gomoku boards are pictures, so they need their own registry: one
        # match per group, an idle deadline, and avatars cached per match.
        self.idle_timeout = int((self.config or {}).get("idle_timeout_seconds", 90))
        self._matches = MatchRegistry(self.idle_timeout)
        self._avatars: dict[str, AvatarCache] = {}
        self._hub = None

    async def initialize(self) -> None:
        self._register_actions()

    async def terminate(self) -> None:
        self._matches.clear()
        self._avatars.clear()
        hub = self._get_hub(quiet=True)
        if hub is not None:
            try:
                hub.actions.unregister_owner(OWNER)
            except Exception:
                logger.warning("[TicTacToe] Failed to unregister actions")

    # --- Hub plumbing -------------------------------------------------------

    def _get_hub(self, quiet: bool = False):
        if self._hub is not None:
            return self._hub
        star = self.context.get_registered_star(HUB_NAME)
        if star is None:
            if not quiet:
                logger.error("[TicTacToe] 未找到插件 %s，请确认已安装并启用", HUB_NAME)
            return None
        if not getattr(star, "activated", True):
            if not quiet:
                logger.error("[TicTacToe] 插件 %s 已安装但未启用", HUB_NAME)
            return None
        # StarMetadata.star_cls is the plugin *instance* (star_cls_type is the
        # class). There is no `star_cls_obj` -- guessing that name is what made
        # this report "Hub not installed" on a perfectly good install.
        hub = getattr(star, "star_cls", None)
        if hub is None:
            if not quiet:
                logger.error("[TicTacToe] %s 尚未完成初始化", HUB_NAME)
            return None
        missing = [name for name in ("send_ephemeral_card", "actions")
                   if not hasattr(hub, name)]
        if missing:
            if not quiet:
                logger.error(
                    "[TicTacToe] %s 版本过旧，缺少 %s，请升级到 v0.9.0 以上",
                    HUB_NAME, "、".join(missing),
                )
            return None
        self._hub = hub
        return hub

    @staticmethod
    def _hub_module(hub: Any, name: str):
        """Import a Hub submodule without hard-coding its package name.

        AstrBot imports plugins as ``data.plugins.<dir_name>.main`` (see
        star_manager: ``path = "data.plugins." + root_dir_name + "." + module_str``).
        The directory name varies between a git clone and a downloaded zip
        (``...-main``), so the package must be derived from the live Hub
        instance -- but by stripping the trailing module, not by taking the
        first segment, which would yield a useless ``data``.
        """
        import importlib

        module_name = type(hub).__module__          # data.plugins.<dir>.main
        package = module_name.rsplit(".", 1)[0]     # data.plugins.<dir>
        return importlib.import_module(f"{package}.qqofficial_hub.{name}")

    def _register_actions(self) -> None:
        hub = self._get_hub()
        if hub is None:
            return
        ActionSpec = self._hub_module(hub, "action_registry").ActionSpec

        specs = (
            ("tictactoe.lobby", "🎮 井字棋", "打开井字棋大厅卡片，选择对战方式。", self._act_lobby),
            ("gomoku.start_ai", "⚫ 五子棋：人机对战", "开始一局与 AI 的五子棋（图片棋盘）。", self._act_gomoku_ai),
            ("gomoku.start_pvp", "⚫ 五子棋：群友对战", "开始一局群友对战的五子棋（图片棋盘）。", self._act_gomoku_pvp),
            ("tictactoe.start_ai", "井字棋：人机对战", "由大厅卡片触发。", self._act_start_ai),
            ("tictactoe.start_pvp", "井字棋：群友对战", "由大厅卡片触发。", self._act_start_pvp),
            ("tictactoe.set_level", "井字棋：切换难度", "由大厅卡片触发。", self._act_set_level),
            ("tictactoe.join", "井字棋：加入对战", "由等待卡片触发。", self._act_join),
            ("tictactoe.move", "井字棋落子", "由棋盘按钮触发，请勿手动绑定。", self._act_move),
            ("tictactoe.occupied", "井字棋占位提示", "点到已落子的格子，只回提示不发消息。", self._act_occupied),
            ("tictactoe.quit", "井字棋结束对局", "结束当前群的井字棋对局。", self._act_quit),
            ("tictactoe.restart", "井字棋再来一局", "以相同模式重新开局。", self._act_restart),
        )
        for action_id, title, description, callback in specs:
            hub.actions.register(ActionSpec(
                action_id=action_id,
                title=title,
                description=description,
                owner=OWNER,
                default_permission="everyone",
                callback=callback,
            ))
        logger.info("[TicTacToe] Registered %d Hub actions", len(specs))

    async def _send_card(self, context, card: dict[str, Any],
                         session_id: str = "") -> str:
        """Send any card as a passive reply to the click that caused it.

        Raises on failure rather than returning "": a silent empty return made
        a broken send look like a successful one, and the only symptom was a
        bare "操作失败" toast with nothing in the log.
        """
        hub = self._get_hub()
        if hub is None:
            raise RuntimeError("QQ Official Hub 不可用")
        passive_event_id = self._hub_module(hub, "passive_reply").passive_event_id
        return await hub.send_ephemeral_card(
            context.origin,
            card,
            client=context.client,
            session_id=session_id,
            event_id=passive_event_id(context.interaction),
            initiator_openid=context.member_openid,
        )

    async def _refresh_labels(self, context, state: dict[str, Any]) -> None:
        """Resolve seat OpenIDs to nicknames via the Hub's identity book.

        Done here rather than in game.py so the rules module keeps working
        without the Hub. Refreshed on every send, so a rename shows up as soon
        as that player next speaks.
        """
        labels = {}
        for mark, openid in (state.get("players") or {}).items():
            if openid:
                labels[mark] = await self._label_of(context, openid)
        state["labels"] = labels

    async def _send_board(self, context, state: dict[str, Any]) -> None:
        """Send the board as a passive reply to the click that triggered it."""
        hub = self._get_hub()
        if hub is None:
            return
        passive_event_id = self._hub_module(hub, "passive_reply").passive_event_id
        await self._refresh_labels(context, state)

        session_id = await hub.send_ephemeral_card(
            context.origin,
            build_card(state),
            client=context.client,
            session_id=state.get("session_id", ""),
            event_id=passive_event_id(context.interaction),
            initiator_openid=context.member_openid,
        )
        state["session_id"] = session_id

    # --- actions ------------------------------------------------------------

    async def _act_lobby(self, context, params) -> int:
        try:
            await self._retire(context.origin)
            await self._send_card(
                context, build_lobby_card(self._level(context.origin))
            )
        except Exception:
            logger.exception("[TicTacToe] Failed to open lobby")
            return 1
        return 0

    async def _act_set_level(self, context, params) -> int:
        level = str(params.get("level") or "")
        if level not in AI_LEVELS:
            return 1
        self._levels[context.origin] = level
        try:
            await self._send_card(context, build_lobby_card(level))
        except Exception:
            logger.exception("[TicTacToe] Failed to switch level")
            return 1
        return 0

    async def _act_start_ai(self, context, params) -> int:
        return await self._start(context, MODE_AI)

    async def _act_start_pvp(self, context, params) -> int:
        return await self._start(context, MODE_PVP)

    async def _act_join(self, context, params) -> int:
        """Second player takes the ❌ seat.

        The seat is guarded here rather than by a one-shot button: the Hub
        consumes a one-shot click before the game runs, so a stray tap by the
        host would lock everyone else out permanently.
        """
        state = self._matches.get(context.origin)
        if state is None:
            return 3
        if state.get("phase") == PHASE_PLAYING:
            return 3  # already started; the ❌ seat is taken
        host = state["players"].get(HUMAN, "")
        if context.member_openid == host:
            # Toast only, and the card stays usable for a real opponent.
            return 4
        state["players"][AI] = context.member_openid
        state["phase"] = PHASE_PLAYING
        await self._send_board(context, state)
        return 0

    def _level(self, origin: str) -> str:
        return self._levels.get(origin, LEVEL_NORMAL)

    async def _start(self, context, mode: str) -> int:
        # One match per group across *all* games: boards are noisy and a second
        # match would make "which board am I replying to?" ambiguous.
        busy = self._matches.busy_reason(context.origin)
        if busy:
            logger.info("[Games] %s", busy)
            return 2
        state = new_state(mode, context.member_openid)
        state["level"] = self._level(context.origin)
        state["display_name"] = "井字棋"
        self._matches.start(context.origin, state)
        if mode == MODE_PVP:
            label = await self._label_of(context, context.member_openid)
            await self._send_card(context, build_waiting_card(state, label))
            return 0
        await self._send_board(context, state)
        return 0

    async def _label_of(self, context, openid: str) -> str:
        hub = self._get_hub(quiet=True)
        book = getattr(hub, "identities", None) if hub else None
        if book is None:
            return ""
        try:
            return await book.label_for(context.origin, openid)
        except Exception:
            return ""

    async def _act_move(self, context, params) -> int:
        state = self._matches.get(context.origin)
        if state is None:
            logger.info("[TicTacToe] Move without an active game in %s", context.origin)
            return 3  # duplicate/expired
        try:
            cell = int(params.get("cell", -1))
        except (TypeError, ValueError):
            return 1

        refusal = apply_move(state, cell, context.member_openid)
        if refusal:
            logger.info("[TicTacToe] Move refused: %s", refusal)
            return 4 if "回合" in refusal or "对局" in refusal else 3

        if not is_over(state["board"]):
            maybe_ai_move(state)
        # A single remaining square has no decision in it; play it rather than
        # spending another card asking for the inevitable tap.
        autoplay_forced_move(state)

        await self._send_board(context, state)
        if is_over(state["board"]):
            await self._retire(context.origin)
        return 0

    async def _act_occupied(self, context, params) -> int:
        """A click on an already-played square.

        Returning an ACK code makes QQ show a toast to that one user and sends
        **no group message**, so a misclick costs nothing -- neither the passive
        reply budget nor everyone else's attention.
        """
        return 3  # duplicate/已使用

    async def _act_quit(self, context, params) -> int:
        """End the current match. Idempotent: a second tap is a no-op toast."""
        if self._matches.get(context.origin) is None:
            return 3
        await self._retire(context.origin)
        return 0

    async def _act_restart(self, context, params) -> int:
        """Start a fresh match in the same mode.

        Retire first so the previous session's cleanup cannot take the new
        card down with it.
        """
        previous = self._matches.get(context.origin)
        mode = previous["mode"] if previous else MODE_AI
        await self._retire(context.origin)
        return await self._start(context, mode)

    # --- gomoku (picture board, quote-to-move) ------------------------------

    async def _act_gomoku_ai(self, context, params) -> int:
        return await self._start_gomoku(context, gk.MODE_AI)

    async def _act_gomoku_pvp(self, context, params) -> int:
        return await self._start_gomoku(context, gk.MODE_PVP)

    async def _start_gomoku(self, context, mode: str) -> int:
        busy = self._matches.busy_reason(context.origin)
        if busy:
            logger.info("[Gomoku] %s", busy)
            return 2  # 操作频繁：本群已有对局
        state = gk.new_state(mode, context.member_openid, self._level(context.origin))
        state["display_name"] = "五子棋"
        self._matches.start(context.origin, state)
        self._avatars[context.origin] = AvatarCache()
        try:
            await self._send_gomoku_board(context.origin, state,
                                          client=context.client,
                                          interaction=context.interaction)
        except Exception:
            logger.exception("[Gomoku] Failed to open board")
            self._matches.pop(context.origin)
            return 1
        return 0

    async def _fetch_avatars(self, origin: str, state: dict[str, Any]) -> dict:
        from .games import gomoku_render as gr

        appid = self._appid_of(origin)
        if not appid:
            return {}
        cache = self._avatars.setdefault(origin, AvatarCache())

        async def fetch(openid: str):
            import aiohttp

            url = gr.avatar_url(appid, openid)
            if not url:
                return None
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as s:
                async with s.get(url) as response:
                    if response.status != 200:
                        return None
                    return await response.read()

        seats = {
            mark: openid for mark, openid in (state.get("players") or {}).items()
            if openid
        }
        return await cache.get_many(seats, fetch)

    def _appid_of(self, origin: str) -> str:
        try:
            platform = self.context.get_platform_inst(origin.split(":", 1)[0])
            config = getattr(platform, "config", None) or {}
            return str(config.get("appid") or getattr(platform, "appid", "") or "")
        except Exception:
            return ""

    async def _send_gomoku_board(self, origin: str, state: dict[str, Any],
                                 client=None, interaction=None,
                                 msg_id: str | None = None) -> None:
        """Render and send the board, remembering the id players must quote."""
        from .games import gomoku_render as gr

        hub = self._get_hub()
        if hub is None:
            raise RuntimeError("QQ Official Hub 不可用")
        book = getattr(hub, "identities", None)
        labels = {}
        for mark, openid in (state.get("players") or {}).items():
            if openid and book is not None:
                try:
                    labels[mark] = await book.label_for(origin, openid)
                except Exception:
                    pass
        state["labels"] = labels

        avatars = await self._fetch_avatars(origin, state)
        image = gr.render_board(state, avatars)
        event_id = ""
        if interaction is not None:
            event_id = self._hub_module(hub, "passive_reply").passive_event_id(
                interaction
            )
        sent_id = await hub.send_image_message(
            origin, image, text=gk.move_hint(state),
            client=client, event_id=event_id or None, msg_id=msg_id,
        )
        # Players must quote this exact message to move; without an id we fall
        # back to accepting bare coordinates rather than blocking the game.
        state["board_msg_id"] = sent_id
        self._matches.touch(state)

    async def _retire(self, origin: str) -> None:
        state = self._matches.pop(origin)
        hub = self._get_hub(quiet=True)
        if state and hub is not None and state.get("session_id"):
            try:
                await hub.end_ephemeral_session(state["session_id"])
            except Exception:
                logger.warning("[TicTacToe] Failed to retire session")

    # --- chat commands ------------------------------------------------------

    @filter.platform_adapter_type(
        filter.PlatformAdapterType.QQOFFICIAL
        | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
    )
    # Higher than the Hub's catch-all panel hint (priority=100): a quoted
    # coordinate is a move, and the Hub must not swallow it first.
    @filter.event_message_type(filter.EventMessageType.ALL, priority=200)
    async def gomoku_move_by_quote(self, event: AstrMessageEvent):
        """Play by quoting the board image and replying with a coordinate.

        A 15x15 board cannot be buttons (25 max), so moves arrive as text.
        Requiring a quote of the board is what separates a real move from
        someone simply mentioning "H8" in conversation.
        """
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        if "GroupMessage" not in origin:
            return
        for dead_origin, _ in self._matches.sweep():
            self._avatars.pop(dead_origin, None)
            logger.info("[Gomoku] Match in %s expired", dead_origin.split(":")[-1][-8:])
        state = self._matches.get(origin)
        if state is None or state.get("game") != "gomoku":
            return
        text = str(event.get_message_str() or "").strip()
        index = gk.parse_coordinate(text)
        if index < 0:
            return

        board_id = str(state.get("board_msg_id") or "")
        if board_id and not self._quotes(event, board_id):
            return          # a coordinate that is not a reply to the board
        event.stop_event()

        actor = str(event.get_sender_id() or "")
        refusal = gk.apply_move(state, index, actor)
        if refusal:
            yield event.plain_result(f"❌ {refusal}")
            return
        if not gk.is_over(state["board"]):
            gk.maybe_ai_move(state)
        self._matches.touch(state)
        try:
            await self._send_gomoku_board(
                origin, state,
                msg_id=str(event.message_obj.message_id or "") or None,
            )
        except Exception as exc:
            logger.exception("[Gomoku] Failed to refresh board")
            yield event.plain_result(f"刷新棋盘失败：{type(exc).__name__}: {exc}")
            return
        if gk.is_over(state["board"]):
            self._matches.pop(origin)
            self._avatars.pop(origin, None)

    @staticmethod
    def _quotes(event: AstrMessageEvent, message_id: str) -> bool:
        """True when this message quotes ``message_id``."""
        for component in (getattr(event.message_obj, "message", None) or []):
            if str(getattr(component, "type", "")).endswith("Reply"):
                if str(getattr(component, "id", "")) == message_id:
                    return True
        return False

    @filter.platform_adapter_type(
        filter.PlatformAdapterType.QQOFFICIAL
        | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
    )
    @filter.command("五子棋", alias={"gomoku"})
    async def gomoku_from_command(self, event: AstrMessageEvent, mode: str = ""):
        """/五子棋 [对战] —— 开一局五子棋（图片棋盘，引用图片回坐标落子）。"""
        event.stop_event()
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        if "GroupMessage" not in origin:
            yield event.plain_result("五子棋只能在 QQ 官方群里玩。")
            return
        busy = self._matches.busy_reason(origin)
        if busy:
            yield event.plain_result(busy)
            return
        if self._get_hub() is None:
            yield event.plain_result("需要先安装并启用 QQ Official Hub 插件。")
            return
        chosen = gk.MODE_PVP if mode.strip() in ("对战", "pvp", "群友") else gk.MODE_AI
        state = gk.new_state(chosen, str(event.get_sender_id() or ""),
                             self._level(origin))
        state["display_name"] = "五子棋"
        self._matches.start(origin, state)
        self._avatars[origin] = AvatarCache()
        try:
            await self._send_gomoku_board(
                origin, state,
                msg_id=str(event.message_obj.message_id or "") or None,
            )
        except Exception as exc:
            self._matches.pop(origin)
            logger.exception("[Gomoku] Failed to start")
            yield event.plain_result(f"开局失败：{type(exc).__name__}: {exc}")

    @filter.platform_adapter_type(
        filter.PlatformAdapterType.QQOFFICIAL
        | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
    )
    @filter.command("下棋", alias={"落子"})
    async def move_from_command(self, event: AstrMessageEvent, position: str = ""):
        """/下棋 1-9 —— 不依赖按钮的落子入口。

        A button is only a shortcut: the rules live on the server, so typing the
        move must be exactly as safe as tapping it. This also keeps the game
        playable on clients where the keyboard fails to render.
        """
        event.stop_event()
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        state = self._matches.get(origin)
        if state is None:
            yield event.plain_result("本群当前没有对局，先发送 /井字棋 开始。")
            return

        raw = str(position or "").strip()
        if not raw.isdigit() or not 1 <= int(raw) <= 9:
            yield event.plain_result("位置必须是 1~9，例如 /下棋 5")
            return

        cell = int(raw) - 1
        refusal = apply_move(state, cell, str(event.get_sender_id() or ""))
        if refusal:
            yield event.plain_result(f"❌ {refusal}")
            return

        if not is_over(state["board"]):
            maybe_ai_move(state)
        autoplay_forced_move(state)

        hub = self._get_hub()
        if hub is None:
            yield event.plain_result("QQ Official Hub 未启用，无法刷新棋盘。")
            return
        # Same label refresh as the button path, so /下棋 renders names too.
        book = getattr(hub, "identities", None)
        labels = {}
        for mark, openid in (state.get("players") or {}).items():
            if openid and book is not None:
                try:
                    labels[mark] = await book.label_for(origin, openid)
                except Exception:
                    pass
        state["labels"] = labels
        try:
            state["session_id"] = await hub.send_ephemeral_card(
                origin,
                build_card(state),
                session_id=state.get("session_id", ""),
                msg_id=str(event.message_obj.message_id or "") or None,
                initiator_openid=str(event.get_sender_id() or ""),
            )
        except Exception as exc:
            logger.exception("[TicTacToe] Failed to refresh board")
            yield event.plain_result(f"刷新棋盘失败：{type(exc).__name__}: {exc}")
            return
        if is_over(state["board"]):
            await self._retire(origin)

    @filter.platform_adapter_type(
        filter.PlatformAdapterType.QQOFFICIAL
        | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
    )
    @filter.command("井字棋", alias={"tictactoe"})
    async def start_from_command(self, event: AstrMessageEvent, mode: str = ""):
        """/井字棋 —— 打开大厅卡片，由按钮选择对战方式。"""
        event.stop_event()
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        if "GroupMessage" not in origin:
            yield event.plain_result("井字棋只能在 QQ 官方群里玩。")
            return
        hub = self._get_hub()
        if hub is None:
            yield event.plain_result("需要先安装并启用 QQ Official Hub 插件。")
            return

        # A previous match may still hold a session. Retire it first: otherwise
        # its later cleanup calls end_ephemeral_session and takes the freshly
        # sent lobby card down with it.
        await self._retire(origin)
        try:
            await hub.send_ephemeral_card(
                origin,
                build_lobby_card(self._level(origin)),
                msg_id=str(event.message_obj.message_id or "") or None,
                initiator_openid=str(event.get_sender_id() or ""),
            )
        except Exception as exc:
            logger.exception("[TicTacToe] Failed to send lobby")
            yield event.plain_result(f"发牌失败：{type(exc).__name__}: {exc}")
