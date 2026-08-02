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
    "QQ 官方机器人井字棋：群友对战或与 AI 对战，全部由卡片按钮驱动。",
    "0.1.0",
    "https://github.com/204343414/astrbot_plugin_Tic-Tac-Toe",
)
class TicTacToePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.config = config or {}
        # origin -> game state. In-memory on purpose: an unfinished match is
        # not worth persisting across restarts, and the cards expire anyway.
        self._games: dict[str, dict[str, Any]] = {}
        self._levels: dict[str, str] = {}
        self._hub = None

    async def initialize(self) -> None:
        self._register_actions()

    async def terminate(self) -> None:
        hub = self._get_hub(quiet=True)
        if hub is not None:
            try:
                hub.actions.unregister_owner(OWNER)
            except Exception:
                logger.warning("[TicTacToe] Failed to unregister actions")
        self._games.clear()

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
        """Send any card as a passive reply to the click that caused it."""
        hub = self._get_hub()
        if hub is None:
            return ""
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
        await self._send_card(context, build_lobby_card(self._level(context.origin)))
        return 0

    async def _act_set_level(self, context, params) -> int:
        level = str(params.get("level") or "")
        if level not in AI_LEVELS:
            return 1
        self._levels[context.origin] = level
        await self._send_card(context, build_lobby_card(level))
        return 0

    async def _act_start_ai(self, context, params) -> int:
        return await self._start(context, MODE_AI)

    async def _act_start_pvp(self, context, params) -> int:
        return await self._start(context, MODE_PVP)

    async def _act_join(self, context, params) -> int:
        """Second player takes the ❌ seat."""
        state = self._games.get(context.origin)
        if state is None or state.get("phase") == PHASE_PLAYING:
            return 3
        host = state["players"].get(HUMAN, "")
        if context.member_openid == host:
            return 4  # the host cannot play both seats
        state["players"][AI] = context.member_openid
        state["phase"] = PHASE_PLAYING
        await self._send_board(context, state)
        return 0

    def _level(self, origin: str) -> str:
        return self._levels.get(origin, LEVEL_NORMAL)

    async def _start(self, context, mode: str) -> int:
        state = new_state(mode, context.member_openid)
        state["level"] = self._level(context.origin)
        self._games[context.origin] = state
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
        state = self._games.get(context.origin)
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
        state = self._games.get(context.origin)
        if state is None:
            return 3
        await self._retire(context.origin)
        return 0

    async def _act_restart(self, context, params) -> int:
        previous = self._games.get(context.origin)
        mode = previous["mode"] if previous else MODE_AI
        return await self._start(context, mode)

    async def _retire(self, origin: str) -> None:
        state = self._games.pop(origin, None)
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
    @filter.command("下棋", alias={"落子"})
    async def move_from_command(self, event: AstrMessageEvent, position: str = ""):
        """/下棋 1-9 —— 不依赖按钮的落子入口。

        A button is only a shortcut: the rules live on the server, so typing the
        move must be exactly as safe as tapping it. This also keeps the game
        playable on clients where the keyboard fails to render.
        """
        event.stop_event()
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        state = self._games.get(origin)
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
