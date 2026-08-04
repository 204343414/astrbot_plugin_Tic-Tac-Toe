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

import time
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .games import animalchess as ach
from .games import gomoku as gk
from .games import lobby
from .games import tictactoe as ttt
from .games.lobby import AI_LEVELS, LEVEL_NORMAL
from .games.session import AvatarCache, MatchRegistry, quoted_message_ids
from .games.tictactoe import (
    AI,
    HUMAN,
    MODE_AI,
    MODE_PVP,
    PHASE_PLAYING,
    apply_move,
    autoplay_forced_move,
    build_card,
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
    "QQ 官方机器人棋类小游戏：井字棋、五子棋、斗兽棋，支持群友对战与 AI 对战。",
    "0.8.0",
    "https://github.com/204343414/astrbot_plugin_Tic-Tac-Toe",
)
class TicTacToePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.config = config or {}
        # origin -> game state. In-memory on purpose: an unfinished match is
        # not worth persisting across restarts, and the cards expire anyway.
        # (origin, game key) -> difficulty. Per game on purpose: the two games
        # are tuned separately and share nothing but the ladder's names.
        self._levels: dict[tuple[str, str], str] = {}
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

        # Every game exposes the *same* five action ids under its own key, so
        # the shared lobby card can be built from a GameSpec alone. Anything
        # game-specific (a board button, an occupied-cell toast) stays out of
        # this table.
        specs = [
            ("tictactoe.lobby", "🎮 井字棋", "打开井字棋卡片：人机 / 群友 / 三档难度。",
             self._act_lobby_tictactoe),
            ("gomoku.lobby", "⚫ 五子棋", "打开五子棋卡片：人机 / 群友 / 三档难度。",
             self._act_lobby_gomoku),
            ("animalchess.lobby", "🐯 斗兽棋", "打开斗兽棋卡片：人机 / 群友 / 三档难度。",
             self._act_lobby_animalchess),

            ("tictactoe.start_ai", "井字棋：人机对战", "由井字棋卡片触发。", self._act_start_ai),
            ("tictactoe.start_pvp", "井字棋：群友对战", "由井字棋卡片触发。", self._act_start_pvp),
            ("tictactoe.set_level", "井字棋：切换难度", "由井字棋卡片触发。", self._act_set_level),
            ("tictactoe.join", "井字棋：加入对战", "由等待卡片触发。", self._act_join),
            ("tictactoe.move", "井字棋落子", "由棋盘按钮触发，请勿手动绑定。", self._act_move),
            ("tictactoe.occupied", "井字棋占位提示", "点到已落子的格子，只回提示不发消息。",
             self._act_occupied),
            ("tictactoe.quit", "井字棋结束对局", "结束当前群的井字棋对局。", self._act_quit),
            ("tictactoe.restart", "井字棋再来一局", "以相同模式重新开局。", self._act_restart),

            ("gomoku.start_ai", "五子棋：人机对战", "由五子棋卡片触发。", self._act_gomoku_ai),
            ("gomoku.start_pvp", "五子棋：群友对战", "由五子棋卡片触发。", self._act_gomoku_pvp),
            ("gomoku.set_level", "五子棋：切换难度", "由五子棋卡片触发。", self._act_set_level_gomoku),
            ("gomoku.join", "五子棋：加入对战", "由等待卡片触发，入座后才发第一张棋盘图。",
             self._act_gomoku_join),
            ("gomoku.quit", "五子棋结束对局", "结束当前群的五子棋对局。", self._act_quit),

            ("animalchess.start_ai", "斗兽棋：人机对战", "由斗兽棋卡片触发。",
             self._act_animalchess_ai),
            ("animalchess.start_pvp", "斗兽棋：群友对战", "由斗兽棋卡片触发。",
             self._act_animalchess_pvp),
            ("animalchess.set_level", "斗兽棋：切换难度", "由斗兽棋卡片触发。",
             self._act_set_level_animalchess),
            ("animalchess.join", "斗兽棋：加入对战", "由等待卡片触发，入座后才发第一张棋盘图。",
             self._act_animalchess_join),
            ("animalchess.quit", "斗兽棋结束对局", "结束当前群的斗兽棋对局。", self._act_quit),
        ]
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

    def _ui_session(self, origin: str, spec) -> str:
        """A stable session id for a game's *card* UI in one group.

        The Hub recalls the card a new one supersedes, but only within the same
        session -- and every send used to open a fresh session, so the lobby,
        the difficulty re-render and the waiting card all piled up on screen.
        Keying the session to (group, game) makes the whole entry flow one
        conversation that replaces itself, which is what it always looked like.

        Deliberately not shared *between* games: opening 五子棋 must not recall
        the 井字棋 card someone else is still reading.
        """
        return f"ui:{spec.key}:{origin}"

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
            session_id=state.get("session_id")
            or self._ui_session(context.origin, ttt.SPEC),
            event_id=passive_event_id(context.interaction),
            initiator_openid=context.member_openid,
        )
        state["session_id"] = session_id

    # --- actions ------------------------------------------------------------

    async def _act_lobby_tictactoe(self, context, params) -> int:
        return await self._open_lobby(context, ttt.SPEC)

    async def _act_lobby_gomoku(self, context, params) -> int:
        return await self._open_lobby(context, gk.SPEC)

    async def _act_lobby_animalchess(self, context, params) -> int:
        return await self._open_lobby(context, ach.SPEC)

    async def _open_lobby(self, context, spec) -> int:
        """Re-send a game's entry card. Each game owns its own; neither is a
        menu of the other."""
        try:
            await self._retire(context.origin)
            await self._send_card(
                context,
                lobby.build_lobby_card(spec, self._level(context.origin, spec)),
                session_id=self._ui_session(context.origin, spec),
            )
        except Exception:
            logger.exception("[Games] Failed to open the %s lobby", spec.key)
            return 1
        return 0

    async def _act_set_level(self, context, params) -> int:
        return await self._set_level(context, ttt.SPEC, params)

    async def _act_set_level_gomoku(self, context, params) -> int:
        return await self._set_level(context, gk.SPEC, params)

    async def _act_set_level_animalchess(self, context, params) -> int:
        return await self._set_level(context, ach.SPEC, params)

    async def _set_level(self, context, spec, params) -> int:
        """Difficulty is remembered per group *and per game*:井字棋困难 and
        五子棋轻松 are a perfectly reasonable pair."""
        level = str(params.get("level") or "")
        if level not in AI_LEVELS:
            return 1
        self._levels[(context.origin, spec.key)] = level
        try:
            await self._send_card(
                context, lobby.build_lobby_card(spec, level),
                session_id=self._ui_session(context.origin, spec),
            )
        except Exception:
            logger.exception("[Games] Failed to switch the %s level", spec.key)
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

    def _level(self, origin: str, spec) -> str:
        return self._levels.get((origin, spec.key), LEVEL_NORMAL)

    async def _start(self, context, mode: str) -> int:
        # One match per group across *all* games: boards are noisy and a second
        # match would make "which board am I replying to?" ambiguous.
        busy = self._matches.busy_reason(context.origin)
        if busy:
            logger.info("[Games] %s", busy)
            return 2
        state = new_state(mode, context.member_openid)
        state["level"] = self._level(context.origin, ttt.SPEC)
        state["display_name"] = ttt.SPEC.title
        self._matches.start(context.origin, state)
        if mode == MODE_PVP:
            label = await self._label_of(context, context.member_openid)
            await self._send_card(
                    context, ttt.build_waiting_card(state, label),
                    session_id=self._ui_session(context.origin, ttt.SPEC),
                )
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
            # Keep the final card: it carries 「🔄 再来一局」.
            await self._retire(context.origin, keep_cards=True)
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
        state = gk.new_state(mode, context.member_openid,
                             self._level(context.origin, gk.SPEC))
        state["display_name"] = gk.SPEC.title
        self._matches.start(context.origin, state)
        self._avatars[context.origin] = AvatarCache()
        try:
            if mode == gk.MODE_PVP:
                # Same two-step as tic-tac-toe: seat the opponent on a card
                # first, so the board picture is only spent on a real match.
                label = await self._label_of(context, context.member_openid)
                await self._send_card(
                    context, gk.build_waiting_card(state, label),
                    session_id=self._ui_session(context.origin, gk.SPEC),
                )
                return 0
            await self._send_picture_board(context.origin, state,
                                          client=context.client,
                                          interaction=context.interaction)
        except Exception:
            logger.exception("[Gomoku] Failed to open board")
            self._matches.pop(context.origin)
            return 1
        return 0

    async def _act_gomoku_join(self, context, params) -> int:
        """Second player takes the ⚪ seat, and only then is a board drawn.

        Guarded here rather than by a one-shot button: the Hub consumes a
        one-shot click *before* the game runs, so the host tapping it once
        would burn the seat for everyone.
        """
        state = self._matches.get(context.origin)
        if state is None or state.get("game") != "gomoku":
            return 3
        if state.get("phase") != gk.PHASE_WAITING:
            return 3  # already started; the ⚪ seat is taken
        if context.member_openid == state["players"].get(gk.BLACK, ""):
            return 4  # toast only; the card stays usable for a real opponent
        state["players"][gk.WHITE] = context.member_openid
        state["phase"] = gk.PHASE_PLAYING
        try:
            await self._send_picture_board(context.origin, state,
                                          client=context.client,
                                          interaction=context.interaction)
        except Exception:
            logger.exception("[Gomoku] Failed to open board after join")
            return 1
        return 0

    # --- animal chess (picture board, quote-to-move) ------------------------

    async def _act_animalchess_ai(self, context, params) -> int:
        return await self._start_animalchess(context, ach.MODE_AI)

    async def _act_animalchess_pvp(self, context, params) -> int:
        return await self._start_animalchess(context, ach.MODE_PVP)

    async def _start_animalchess(self, context, mode: str) -> int:
        busy = self._matches.busy_reason(context.origin)
        if busy:
            logger.info("[AnimalChess] %s", busy)
            return 2  # 操作频繁：本群已有对局
        # Checked before the match exists: the board *is* a card here, so a
        # game that cannot draw one is a game that cannot be played at all.
        reason = await self._image_host_or_reason()
        if reason:
            # A toast says only "操作失败", which sends people to the logs for
            # something they can fix in the config. Spell it out in the group.
            logger.warning("[AnimalChess] Refusing to start: %s", reason)
            try:
                await self._send_card(context, {
                    "id": "animalchess_unavailable",
                    "markdown": "\n".join([
                        "# 斗兽棋暂时开不了",
                        f"**{reason}**",
                        "",
                        "斗兽棋的棋盘是一张带按钮的卡片，需要 Hub 图床把图片"
                        "发布到公网，所以图床不通就没法开局。",
                        "管理员可发送 `/诊断` 查看图床状态。",
                    ]),
                    "rows": [],
                    "ttl_seconds": 600,
                }, session_id=self._ui_session(context.origin, ach.SPEC))
            except Exception:
                logger.exception("[AnimalChess] Failed to explain the refusal")
            return 1
        state = ach.new_state(mode, context.member_openid,
                              self._level(context.origin, ach.SPEC))
        state["display_name"] = ach.SPEC.title
        self._matches.start(context.origin, state)
        try:
            if mode == ach.MODE_PVP:
                label = await self._label_of(context, context.member_openid)
                await self._send_card(
                    context, ach.build_waiting_card(state, label),
                    session_id=self._ui_session(context.origin, ach.SPEC),
                )
                return 0
            await self._send_animalchess_card(context.origin, state,
                                              client=context.client,
                                              interaction=context.interaction)
        except Exception:
            logger.exception("[AnimalChess] Failed to open board")
            self._matches.pop(context.origin)
            return 1
        return 0

    async def _act_animalchess_join(self, context, params) -> int:
        """Second player takes the 🔵 seat, and only then is a board drawn."""
        state = self._matches.get(context.origin)
        if state is None or state.get("game") != "animalchess":
            return 3
        if state.get("phase") != ach.PHASE_WAITING:
            return 3  # already started; the 🔵 seat is taken
        if context.member_openid == state["players"].get(ach.RED, ""):
            return 4  # toast only; the card stays usable for a real opponent
        state["players"][ach.BLUE] = context.member_openid
        state["phase"] = ach.PHASE_PLAYING
        try:
            await self._send_animalchess_card(context.origin, state,
                                              client=context.client,
                                              interaction=context.interaction)
        except Exception:
            logger.exception("[AnimalChess] Failed to open board after join")
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

    async def _send_picture_board(self, origin: str, state: dict[str, Any],
                                  client=None, interaction=None,
                                  msg_id: str | None = None) -> None:
        """Render and send a picture board, remembering the id to quote.

        Shared by every game whose board is an image. Only the render call
        differs, so it is dispatched on ``state["game"]`` rather than copied --
        the send/label/quote plumbing is identical and was worth getting right
        exactly once.
        """
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

        if state.get("game") == "animalchess":
            from .games import animalchess_render as ar

            image = ar.render_board(state)
        else:
            from .games import gomoku_render as gr

            image = gr.render_board(state, await self._fetch_avatars(origin, state))
        event_id = ""
        if interaction is not None:
            event_id = self._hub_module(hub, "passive_reply").passive_event_id(
                interaction
            )
        # No caption. The hint is drawn *inside* the picture (render_board);
        # passing it as text too printed it twice -- once in the image and once
        # in the chat body, which is exactly what it was moved in to avoid.
        previous_id = str(state.get("board_msg_id") or "")
        previous_at = state.get("board_sent_at")
        sent_id = await hub.send_image_message(
            origin, image,
            client=client, event_id=event_id or None, msg_id=msg_id,
        )
        # Players must quote this exact message to move; without an id we fall
        # back to accepting bare coordinates rather than blocking the game.
        state["board_msg_id"] = sent_id
        state["board_sent_at"] = time.time()
        self._matches.touch(state)

        # Retire the previous board so a long game leaves one picture behind
        # instead of one per move. Deliberately *after* the new board is up: if
        # the send failed we raised already, and recalling first would leave the
        # group with no board at all.
        if previous_id and previous_id != sent_id:
            await self._recall_quietly(origin, previous_id, previous_at, client)

    async def _image_host_or_reason(self) -> str:
        """"" when cards can carry pictures, otherwise why they cannot.

        Animal chess has no non-card mode, so this is checked before a match
        is created rather than after -- refusing to start is recoverable,
        while a half-started game with no board is not.
        """
        hub = self._get_hub(quiet=True)
        if hub is None:
            return "QQ Official Hub 未安装或未启用"
        checker = getattr(hub, "image_host_reachable", None)
        if checker is None:
            return "Hub 版本过旧（需要 v0.21.0+ 的图床自愈接口），请更新"
        try:
            if await checker():
                return ""
        except Exception as exc:
            return f"图床检查失败：{type(exc).__name__}: {exc}"
        if not getattr(hub, "image_host_enabled", False):
            return "Hub 图床未开启：配置里打开 image_host_enabled"
        return "图床拿不到公网地址：确认 cloudflared 正在运行"

    async def _send_animalchess_card(self, origin: str, state: dict[str, Any],
                                     client=None, interaction=None,
                                     msg_id: str | None = None) -> None:
        """Send the board as a card: picture and move buttons in one message.

        Requires the Hub's image host, because QQ will not accept rich media
        and a keyboard in the same message -- the picture has to arrive as a
        Markdown image, which needs a public URL. There is no degraded mode
        on purpose: silently falling back to the old quote-a-picture flow
        would mean the buttons vanish with no explanation, and "the feature
        sometimes exists" is harder to report than "it is off".
        """
        from .games import animalchess_render as ar

        hub = self._get_hub()
        if hub is None:
            raise RuntimeError("QQ Official Hub 不可用")
        await self._refresh_labels_from_origin(origin, state)

        # No banner: the card's Markdown already states the turn and the
        # hint, and drawing them into the picture too printed both twice.
        image = ar.render_board(state, banner=False)
        # One slot per group: publishing the next turn's board retires this
        # one, so a long game leaves a single file rather than one per move.
        url = await hub.publish_image_checked(image, slot=f"animalchess:{origin}")

        card = ach.build_board_card(state, url)
        passive_event_id = self._hub_module(hub, "passive_reply").passive_event_id
        await hub.send_ephemeral_card(
            origin, card,
            client=client,
            session_id=self._ui_session(origin, ach.SPEC),
            event_id=passive_event_id(interaction) if interaction is not None else None,
            msg_id=msg_id,
            initiator_openid="",
            clicker_header="",
        )
        self._matches.touch(state)

    async def _refresh_labels_from_origin(self, origin: str,
                                          state: dict[str, Any]) -> None:
        """Resolve seat OpenIDs to nicknames without needing a click context."""
        hub = self._get_hub(quiet=True)
        book = getattr(hub, "identities", None) if hub else None
        if book is None:
            return
        labels = {}
        for mark, openid in (state.get("players") or {}).items():
            if not openid:
                continue
            try:
                labels[mark] = await book.label_for(origin, openid)
            except Exception:
                pass
        state["labels"] = labels

    async def _recall_quietly(self, origin: str, message_id: str,
                              sent_at: float | None, client=None) -> None:
        """Best-effort cleanup of a superseded board.

        Never raises and never reports: the move already succeeded, and QQ
        refuses recalls older than two minutes, so failure here is ordinary
        rather than exceptional.
        """
        hub = self._get_hub(quiet=True)
        if hub is None or not hasattr(hub, "recall_message"):
            return          # older Hub; the extra pictures are only cosmetic
        try:
            await hub.recall_message(origin, message_id,
                                     client=client, sent_at=sent_at)
        except Exception:
            logger.debug("[Games] Failed to recall the previous board")

    async def _retire(self, origin: str, keep_cards: bool = False) -> None:
        """Drop the match. ``keep_cards`` leaves the last card clickable.

        end_ephemeral_session() invalidates *every* card of the session, so
        calling it when a game ends turned the final board's 「🔄 再来一局」
        into a dead button answering 「卡片不存在或已过期」. At game over the
        match is gone but the card must survive; only an explicit quit -- or
        making room for a new game -- retires the cards too.
        """
        state = self._matches.pop(origin)
        self._avatars.pop(origin, None)
        if keep_cards:
            return
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
        if state.get("phase") == gk.PHASE_WAITING:
            return  # nobody has taken the ⚪ seat yet; there is no board to quote
        text = str(event.get_message_str() or "").strip()
        index = gk.parse_coordinate(text)
        if index < 0:
            return

        board_id = str(state.get("board_msg_id") or "")
        quoted = quoted_message_ids(event)
        if not quoted:
            return          # a bare coordinate in conversation is not a move
        if board_id and board_id not in quoted:
            # They quoted *something* and typed a coordinate during a live
            # match, so treat it as a move -- but say so, because a mismatch
            # means the id QQ echoed on send differs from the one it reports
            # on quote, and that is worth knowing.
            logger.info(
                "[Gomoku] Quoted id %s does not match the board id %s; "
                "accepting the move anyway",
                ",".join(quoted), board_id,
            )
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
            await self._send_picture_board(
                origin, state,
                msg_id=str(event.message_obj.message_id or "") or None,
            )
        except Exception as exc:
            # The move is already on the board, so the match is *ahead* of what
            # players can see. Say so and tell them how to redraw, rather than
            # leaving them staring at a stale picture wondering if it counted.
            logger.exception("[Gomoku] Failed to refresh board")
            yield event.plain_result(
                f"落子已记录（{gk.format_coordinate(index)}），但棋盘图没发出来："
                f"{self._describe(exc)}\n发送 /棋盘 可以重新出图。"
            )
            return
        if gk.is_over(state["board"]):
            self._matches.pop(origin)
            self._avatars.pop(origin, None)

    @filter.platform_adapter_type(
        filter.PlatformAdapterType.QQOFFICIAL
        | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
    )
    # Same priority reasoning as the gomoku handler: outrank the Hub's
    # catch-all panel hint so a quoted move is not swallowed first.
    @filter.event_message_type(filter.EventMessageType.ALL, priority=200)
    async def animalchess_move_by_quote(self, event: AstrMessageEvent):
        """Play by quoting the board image and naming a piece and direction.

        63 squares cannot be buttons (25 max) and a coordinate grid would be
        unreadable, so moves are phrases like 鼠下. Each side owns exactly one
        of every animal, so the piece is unambiguous once the mover is known.
        """
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        if "GroupMessage" not in origin:
            return
        for dead_origin, _ in self._matches.sweep():
            self._avatars.pop(dead_origin, None)
            logger.info("[AnimalChess] Match in %s expired",
                        dead_origin.split(":")[-1][-8:])
        state = self._matches.get(origin)
        if state is None or state.get("game") != "animalchess":
            return
        if state.get("phase") == ach.PHASE_WAITING:
            return  # nobody has taken the 🔵 seat yet; there is no board to quote
        parsed = ach.parse_move(event.get_message_str())
        if parsed is None:
            return
        animal, direction = parsed

        # The board buttons carry reply=True, so a move composed by tapping
        # always arrives quoting the board card. A quote is still required --
        # it is what separates a move from someone saying 鼠下 in conversation
        # -- but the id is no longer matched: the card is replaced every turn,
        # and rejecting a move for quoting the turn-old card would punish the
        # player for the redraw rather than for anything they did.
        if not quoted_message_ids(event):
            return
        event.stop_event()

        actor = str(event.get_sender_id() or "")
        refusal = ach.apply_move(state, animal, direction, actor)
        if refusal:
            yield event.plain_result(f"❌ {refusal}")
            return
        if not ach.is_over(state):
            ach.maybe_ai_move(state)
        self._matches.touch(state)
        try:
            await self._send_animalchess_card(
                origin, state,
                msg_id=str(event.message_obj.message_id or "") or None,
            )
        except Exception as exc:
            logger.exception("[AnimalChess] Failed to refresh board")
            yield event.plain_result(
                f"走棋已记录（{ach.NAMES[animal]}{direction}），但棋盘卡没发出来："
                f"{self._describe(exc)}\n发送 /棋盘 可以重新出卡。"
            )
            return
        if ach.is_over(state):
            self._matches.pop(origin)
            self._avatars.pop(origin, None)

    @staticmethod
    def _describe(exc: BaseException) -> str:
        """A one-line cause for chat.

        QQ's own 5xx ("系统繁忙，请稍后重试") is by far the most common failure and
        is nobody's fault, so it gets said plainly instead of as a stack-trace
        class name that reads like a plugin crash.
        """
        text = str(exc).strip()
        if type(exc).__name__ == "ServerError":
            return f"QQ 服务端繁忙（{text or '稍后重试'}），重试 3 次仍失败"
        return f"{type(exc).__name__}: {text}"

    @filter.platform_adapter_type(
        filter.PlatformAdapterType.QQOFFICIAL
        | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
    )
    @filter.command("棋盘", alias={"board"})
    async def redraw_board(self, event: AstrMessageEvent):
        """/棋盘 —— 重新发一张当前棋盘图（上传失败后用）。"""
        event.stop_event()
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        state = self._matches.get(origin)
        if state is None or state.get("game") not in ("gomoku", "animalchess"):
            yield event.plain_result("本群当前没有图片棋盘对局。")
            return
        if state.get("phase") == gk.PHASE_WAITING:
            yield event.plain_result("对局还在等对手加入，尚未开始。")
            return
        msg_id = str(event.message_obj.message_id or "") or None
        try:
            # Animal chess draws a card, gomoku an image message. Dispatching
            # on the game rather than always doing the same thing is what
            # keeps /棋盘 an actual redraw of what players are looking at.
            if state.get("game") == "animalchess":
                await self._send_animalchess_card(origin, state, msg_id=msg_id)
            else:
                await self._send_picture_board(origin, state, msg_id=msg_id)
        except Exception as exc:
            logger.exception("[Games] Failed to redraw board")
            yield event.plain_result(f"出图失败：{self._describe(exc)}")

    @filter.platform_adapter_type(
        filter.PlatformAdapterType.QQOFFICIAL
        | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
    )
    @filter.command("斗兽棋", alias={"animalchess", "jungle"})
    async def animalchess_from_command(self, event: AstrMessageEvent):
        """/斗兽棋 —— 打开斗兽棋卡片，由按钮选择对战方式与难度。"""
        async for result in self._lobby_from_command(event, ach.SPEC):
            yield result

    @filter.platform_adapter_type(
        filter.PlatformAdapterType.QQOFFICIAL
        | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
    )
    @filter.command("五子棋", alias={"gomoku"})
    async def gomoku_from_command(self, event: AstrMessageEvent):
        """/五子棋 —— 打开五子棋卡片，由按钮选择对战方式与难度。"""
        async for result in self._lobby_from_command(event, gk.SPEC):
            yield result

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
            await self._retire(origin, keep_cards=True)

    @filter.platform_adapter_type(
        filter.PlatformAdapterType.QQOFFICIAL
        | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
    )
    @filter.command("井字棋", alias={"tictactoe"})
    async def start_from_command(self, event: AstrMessageEvent):
        """/井字棋 —— 打开井字棋卡片，由按钮选择对战方式与难度。"""
        async for result in self._lobby_from_command(event, ttt.SPEC):
            yield result

    async def _lobby_from_command(self, event: AstrMessageEvent, spec):
        """Every game's slash command does the same thing: send its lobby.

        Identical by construction rather than by copy-paste, so a third game
        cannot drift into a different entry experience.
        """
        event.stop_event()
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        if "GroupMessage" not in origin:
            yield event.plain_result(f"{spec.title}只能在 QQ 官方群里玩。")
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
                lobby.build_lobby_card(spec, self._level(origin, spec)),
                session_id=self._ui_session(origin, spec),
                msg_id=str(event.message_obj.message_id or "") or None,
                initiator_openid=str(event.get_sender_id() or ""),
            )
        except Exception as exc:
            logger.exception("[Games] Failed to send the %s lobby", spec.key)
            yield event.plain_result(f"发牌失败：{type(exc).__name__}: {exc}")
