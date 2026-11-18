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

import asyncio
import hashlib
import inspect
import os
import random
import time
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter

try:                              # 老版本 AstrBot 可能没有导出 MessageChain
    from astrbot.api.event import MessageChain
except ImportError:               # pragma: no cover
    MessageChain = None           # type: ignore[assignment]
from astrbot.api.star import Context, Star, register

from .games import animalchess as ach
from .games import chess as ch
from .games import hornchess as hn
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


#: 轮到某一方却迟迟不走棋，多久算挂机（秒）。所有双人博弈统一用这一个值。
DEFAULT_MOVE_TIMEOUT = 180

#: 所有棋种的规格表。规则按钮、超时文案、以及以后的新玩法都从这里取，
#: 避免"新增一个游戏忘了在某处补一行"。
GAME_SPECS = (ttt.SPEC, gk.SPEC, ach.SPEC, ch.SPEC, hn.SPEC)


@register(
    PLUGIN_NAME,
    "204343414",
    "QQ 官方机器人棋类小游戏：井字棋、五子棋、斗兽棋、国际象棋、牛角棋，支持群友对战与 AI 对战。",
    "0.11.0",
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
        #: 轮到某一方却迟迟不走棋，超过这么多秒就自动判他认输。
        #: 这个和 idle_timeout 是两件事：idle 是"整局没人管了就把座位清空"，
        #: 这个是"一方挂机了，直接判负，让对手不用干等"。
        #: 0 = 关掉自动认输（只保留清座位）。
        self.move_timeout = int(
            (self.config or {}).get("move_timeout_seconds", DEFAULT_MOVE_TIMEOUT)
        )
        # 座位清理必须晚于判负，否则一局挂机到 90 秒就被静悄悄清掉，
        # 玩家永远等不到那句"判认输"。留 60 秒余量让播报先发出去。
        if self.move_timeout:
            # 只有本来就没配 idle（旧配置）时才自动放宽，避免覆盖用户手写的值。
            if "idle_timeout_seconds" not in (self.config or {}):
                self.idle_timeout = max(self.idle_timeout, self.move_timeout + 60)
        self._matches = MatchRegistry(self.idle_timeout)
        #: 后台巡检任务：没有它，超时只能等到下一次有人说话才被发现，
        #: 而挂机场景恰恰就是"没有人说话"。
        self._timeout_task: asyncio.Task | None = None
        self._avatars: dict[str, AvatarCache] = {}
        self._hub = None

    async def initialize(self) -> None:
        self._register_actions()
        self._start_timeout_watchdog()

    async def terminate(self) -> None:
        await self._stop_timeout_watchdog()
        self._matches.clear()
        self._avatars.clear()
        hub = self._get_hub(quiet=True)
        if hub is not None:
            try:
                hub.actions.unregister_owner(OWNER)
            except Exception:
                logger.warning("[TicTacToe] Failed to unregister actions")

    # --- 走棋超时（挂机判负）------------------------------------------------
    #
    # 为什么要有后台任务：以前一轮"清座位"是懒扫描，只有在**下一条消息进来**
    # 时才检查。可挂机的场景正好是没人说话，于是那局会一直躺在那里，对手只能
    # 自己发一句 /认输。改成定时巡检之后，180 秒到点就自己判负并推一条消息。
    #
    # 为什么判"超时方认输"而不是"判和"：用户明确要求认输。判和会让挂机变成
    # 一种便宜的求和手段；认输才让"轮到你就得走"成为硬规则。

    TIMEOUT_SWEEP_SECONDS = 15

    def _start_timeout_watchdog(self) -> None:
        if not self.move_timeout or self._timeout_task is not None:
            return
        try:
            self._timeout_task = asyncio.create_task(self._timeout_watchdog())
        except RuntimeError:          # 没有事件循环（单测里常见），静默跳过
            self._timeout_task = None

    async def _stop_timeout_watchdog(self) -> None:
        task = self._timeout_task
        self._timeout_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _timeout_watchdog(self) -> None:
        """每 15 秒看一遍所有对局，把挂机的那一方判负。"""
        while True:
            try:
                await asyncio.sleep(self.TIMEOUT_SWEEP_SECONDS)
                await self._sweep_move_timeouts()
            except asyncio.CancelledError:
                raise
            except Exception:         # 巡检自己不能把插件带崩
                logger.exception("[Games] 超时巡检出错")

    async def _sweep_move_timeouts(self, now: float | None = None) -> list[str]:
        """对所有对局做一次超时检查，返回被判负的 origin 列表。

        独立成一个方法（而不是写进循环里）是为了能在测试里直接调、并且
        传入伪造的 ``now``，不用真的等 180 秒。
        """
        if not self.move_timeout:
            return []
        now = time.time() if now is None else now
        fired: list[str] = []
        for origin in list(self._matches._matches.keys()):
            # 直接读注册表而不是 get()：get() 会顺手把"idle 过期"的对局删掉，
            # 那样判负就永远轮不到它，玩家只会看到对局凭空消失。
            state = self._matches._matches.get(origin)
            if state is None:
                continue
            if not self._match_is_live(state):
                continue
            moved = float(state.get("touched_at") or 0)
            if now - moved <= self.move_timeout:
                continue
            if await self._forfeit_on_timeout(origin, state):
                fired.append(origin)
        return fired

    @staticmethod
    def _match_is_live(state: dict[str, Any]) -> bool:
        """只有"真在下"的对局才计时。等待对手、已分胜负的都不算。"""
        if state.get("winner"):
            return False
        if str(state.get("phase") or "") == "waiting":
            return False
        return True

    async def _forfeit_on_timeout(self, origin: str, state: dict[str, Any]) -> bool:
        """把轮到的那一方判负，并推一条消息到群里。"""
        spec = next((sp for sp in GAME_SPECS if sp.key == state.get("game")), None)
        if spec is None:
            await self._retire(origin, keep_cards=True)
            return False
        loser_side = str(state.get("turn") or "")
        loser_mark = {"R": spec.host_mark, "B": spec.guest_mark}.get(loser_side, "")
        if state.get("game") in (ach.SPEC.key,):
            loser_name = {"R": "红方", "B": "蓝方"}.get(loser_side, "轮到的一方")
        else:
            loser_name = {"R": "先手", "B": "后手"}.get(loser_side, "轮到的一方")
        labels = state.get("labels") or {}
        who = labels.get(loser_side) or ""
        who_text = f"{loser_name} {who}".strip()
        minutes = max(1, round(self.move_timeout / 60))
        text = (f"⏱️ {spec.title}：{who_text}{('（' + loser_mark + '）') if loser_mark else ''}"
                f" 超过 {minutes} 分钟没有走棋，判**认输**。"
                f"这一局到此为止，可以随时开新的一局。")

        # 先把对局收掉，再发消息：消息发失败也不能让对局赖在注册表里。
        await self._retire(origin, keep_cards=True)
        await self._announce(origin, text)
        logger.info("[Games] %s 超时判负：origin=%s side=%s", spec.title, origin, loser_side)
        return True

    async def _announce(self, origin: str, text: str) -> bool:
        """在本群发一条普通文本消息（主动消息）。

        失败只记日志：QQ 官方对主动消息有频控和"用户可关闭"的设置，这是平台
        限制而不是插件故障，绝不能因此回滚已经做出的判决。
        """
        if MessageChain is None:
            logger.warning("[Games] 当前 AstrBot 没有 MessageChain，超时提示只能写日志：%s", text)
            return False
        try:
            await self.context.send_message(origin, MessageChain().message(text))
            return True
        except Exception as exc:      # noqa: BLE001
            logger.warning("[Games] 超时提示发送失败：origin=%s error=%s", origin, exc)
            return False

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

    #: 每个群保留几张棋盘素材。QQ 素材库每个机器人只有 100 张，而斗兽棋每走
    #: 一步都换一张图 —— 不回收的话四五盘棋就把额度吃光。留 2 张是因为翻回去
    #: 看上一手仍然常见；更早的卡片本来也会被 Hub 撤回。
    MATERIAL_KEEP = 2

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
            ("chess.lobby", "♔ 国际象棋", "打开国际象棋卡片：人机 / 群友 / 三档难度。",
             self._act_lobby_chess),
            ("hornchess.lobby", "🐮 牛角棋", "打开牛角棋卡片：先选执牛还是执人，再选对战方式。",
             self._act_lobby_hornchess),

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

            ("chess.start_ai", "国际象棋：人机对战", "由国际象棋卡片触发。",
             self._act_chess_ai),
            ("chess.start_pvp", "国际象棋：群友对战", "由国际象棋卡片触发。",
             self._act_chess_pvp),
            ("chess.set_level", "国际象棋：切换难度", "由国际象棋卡片触发。",
             self._act_set_level_chess),
            ("chess.join", "国际象棋：加入对战", "由等待卡片触发，入座后才发第一张棋盘。",
             self._act_chess_join),
            ("chess.quit", "国际象棋结束对局", "结束当前群的国际象棋对局。",
             self._act_quit),

            ("hornchess.pick_side", "牛角棋：选边", "由牛角棋入口卡片触发：先选执牛还是执人。",
             self._act_pick_side_hornchess),
            ("hornchess.start_ai", "牛角棋：人机对战", "由牛角棋卡片触发。",
             self._act_hornchess_ai),
            ("hornchess.start_pvp", "牛角棋：群友对战", "由牛角棋卡片触发。",
             self._act_hornchess_pvp),
            ("hornchess.set_level", "牛角棋：切换难度", "由牛角棋卡片触发。",
             self._act_set_level_hornchess),
            ("hornchess.join", "牛角棋：加入对战", "由等待卡片触发。",
             self._act_hornchess_join),
            ("hornchess.move", "牛角棋走棋", "由棋盘按钮触发，请勿手动绑定。",
             self._act_hornchess_move),
            ("hornchess.quit", "牛角棋结束对局", "结束当前群的牛角棋对局。",
             self._act_quit),
        ]
        # 规则按钮：只要 GameSpec 声明了 rules 就有，新增游戏免改这里。
        for spec in GAME_SPECS:
            if not spec.rules:
                continue
            specs.append((f"{spec.key}.rules", f"{spec.title}：规则速查",
                          f"把{spec.title}的规则推到群里。", self._act_show_rules))
            specs.append((f"{spec.key}.rules_ack", f"{spec.title}：规则卡关闭",
                          "规则卡片上的「知道了」，只收卡片。", self._act_rules_ack))
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

    async def _act_lobby_chess(self, context, params) -> int:
        return await self._open_lobby(context, ch.SPEC)

    async def _act_lobby_hornchess(self, context, params) -> int:
        return await self._open_lobby(context, hn.SPEC)

    async def _act_pick_side_hornchess(self, context, params) -> int:
        """入口卡片的第一步：玩家选了执牛还是执人，再给第二种卡片。"""
        side = str(params.get("side") or "")
        if side not in (hn.HUMANS, hn.BULL):
            return 3
        try:
            await self._send_card(
                context,
                lobby.build_lobby_card(hn.SPEC,
                                       self._level(context.origin, hn.SPEC), side),
                session_id=self._ui_session(context.origin, hn.SPEC),
            )
        except Exception:
            logger.exception("[HornChess] Failed to open the start card")
            return 1
        return 0

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

    async def _act_set_level_chess(self, context, params) -> int:
        return await self._set_level(context, ch.SPEC, params)

    async def _act_set_level_hornchess(self, context, params) -> int:
        return await self._set_level(context, hn.SPEC, params)

    async def _set_level(self, context, spec, params) -> int:
        """Difficulty is remembered per group *and per game*:井字棋困难 and
        五子棋轻松 are a perfectly reasonable pair."""
        level = str(params.get("level") or "")
        if level not in AI_LEVELS:
            return 1
        side = str(params.get("side") or "")     # 有选边的棋种要记住玩家选的那一方
        self._levels[(context.origin, spec.key)] = level
        try:
            await self._send_card(
                context, lobby.build_lobby_card(spec, level, side),
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

    async def _act_show_rules(self, context, params) -> int:
        """规则按钮：发一张独立的规则卡，不动任何对局状态。

        不塞进大厅卡的原因：大厅卡的按钮位是给"开始游戏"用的，规则是看完
        就走的资料；而且独立卡片能自己过期，不会把大厅卡挤到下一屏。
        """
        key = str((params or {}).get("spec") or "")
        spec = next((sp for sp in GAME_SPECS if sp.key == key), None)
        if spec is None or not spec.rules:
            return 1
        await self._send_card(context, lobby.build_rules_card(spec),
                              session_id=self._ui_session(context.origin, spec))
        return 0

    async def _act_rules_ack(self, context, params) -> int:
        """规则卡上的「知道了」：什么都不改，只让 Hub 收掉这张卡。"""
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

    # --- horn chess (picture board, one-tap buttons) ------------------------

    async def _act_hornchess_ai(self, context, params) -> int:
        return await self._start_hornchess(
            context, hn.MODE_AI, str(params.get("side") or ""))

    async def _act_hornchess_pvp(self, context, params) -> int:
        return await self._start_hornchess(
            context, hn.MODE_PVP, str(params.get("side") or ""))

    async def _start_hornchess(self, context, mode: str, side: str = "") -> int:
        busy = self._matches.busy_reason(context.origin)
        if busy:
            logger.info("[HornChess] %s", busy)
            return 2
        reason = await self._image_host_or_reason()
        if reason:
            logger.warning("[HornChess] Refusing to start: %s", reason)
            try:
                await self._send_card(context, {
                    "id": "hornchess_unavailable",
                    "markdown": "\n".join([
                        "# 牛角棋暂时开不了",
                        f"**{reason}**",
                        "",
                        "棋盘是一张带按钮的卡片，图片要有地方安放才能开局。",
                    ]),
                    "rows": [],
                    "ttl_seconds": 600,
                }, session_id=self._ui_session(context.origin, hn.SPEC))
            except Exception:
                logger.exception("[HornChess] Failed to explain the refusal")
            return 1
        side = side if side in (hn.HUMANS, hn.BULL) else hn.HUMANS
        state = hn.new_state(mode, context.member_openid,
                             self._level(context.origin, hn.SPEC), side)
        state["display_name"] = hn.SPEC.title
        # 人先走是这棋的规矩；玩家要是选执牛，开局就轮到 AI 的那两个人走一步，
        # 否则卡片一发出来就是"轮到对手"，按钮一个都不给，看着像卡住了。
        if mode == hn.MODE_AI and state["turn"] != hn.player_side(state):
            hn.ai_move(state)
        self._matches.start(context.origin, state)
        self._avatars[context.origin] = AvatarCache()
        try:
            if mode == hn.MODE_PVP:
                label = await self._label_of(context, context.member_openid)
                await self._send_card(
                    context, hn.build_waiting_card(state, label),
                    session_id=self._ui_session(context.origin, hn.SPEC),
                )
                return 0
            await self._send_hornchess_card(context.origin, state,
                                           client=context.client,
                                           interaction=context.interaction)
        except Exception:
            logger.exception("[HornChess] Failed to open board")
            self._matches.pop(context.origin)
            return 1
        return 0

    async def _act_hornchess_join(self, context, params) -> int:
        state = self._matches.get(context.origin)
        if state is None or state.get("game") != "hornchess":
            return 3
        if state.get("phase") != hn.PHASE_WAITING:
            return 3
        if context.member_openid == state["players"].get(hn.player_side(state), ""):
            return 4
        other = hn.OTHER[hn.player_side(state)]
        state["players"][other] = context.member_openid
        state["phase"] = hn.PHASE_PLAYING
        try:
            await self._send_hornchess_card(context.origin, state,
                                           client=context.client,
                                           interaction=context.interaction)
        except Exception:
            logger.exception("[HornChess] Failed to open board after join")
            return 1
        return 0

    async def _act_hornchess_move(self, context, params) -> int:
        """棋盘按钮：一次点击就是一步棋。

        这是牛角棋值得单独做一个的原因——11 个点、每方最多 4 种走法，
        全部塞进键盘绰绰有余（一行最多 5 个按钮）。斗兽棋要点两次，国际象棋只能打字，这里
        点一下就完事。
        """
        state = self._matches.get(context.origin)
        if state is None or state.get("game") != "hornchess":
            return 3
        try:
            frm = int(params.get("frm", -1))
            to = int(params.get("to", -1))
        except (TypeError, ValueError):
            return 3
        refusal = hn.apply_move(state, frm, to, context.member_openid)
        if refusal:
            # 非法走法只回一个 toast，不浪费一条被动回复
            logger.info("[HornChess] Rejected %s→%s: %s",
                        hn.name_of(frm), hn.name_of(to), refusal)
            return 4
        if (not hn.is_over(state) and state["mode"] == hn.MODE_AI
                and state["turn"] != hn.player_side(state)):
            # AI 那一方（可能是牛，也可能是那两个赶牛的人）。查表求解是 O(1) 的
            # （局面只有 990 个，开局算一次就缓存），不用像国际象棋那样丢线程。
            hn.ai_move(state)
        self._matches.touch(state)
        try:
            await self._send_hornchess_card(context.origin, state,
                                           client=context.client,
                                           interaction=context.interaction)
        except Exception:
            logger.exception("[HornChess] Failed to refresh board")
            return 1
        if hn.is_over(state):
            self._matches.pop(context.origin)
            self._avatars.pop(context.origin, None)
        return 0

    async def _send_hornchess_card(self, origin: str, state: dict[str, Any],
                                   client=None, interaction=None,
                                   msg_id: str | None = None) -> None:
        from .games import hornchess_render as hr

        await self._refresh_labels_from_origin(origin, state)
        await self._send_image_card(
            origin, state, hr.render_board(state, banner=False),
            hn.SPEC, hn.build_board_card, tag="HornChess",
            client=client, interaction=interaction, msg_id=msg_id,
        )

    # --- chess (picture board, quote-to-move) -------------------------------

    async def _act_chess_ai(self, context, params) -> int:
        return await self._start_chess(context, ch.MODE_AI)

    async def _act_chess_pvp(self, context, params) -> int:
        return await self._start_chess(context, ch.MODE_PVP)

    async def _start_chess(self, context, mode: str) -> int:
        busy = self._matches.busy_reason(context.origin)
        if busy:
            logger.info("[Chess] %s", busy)
            return 2
        reason = await self._image_host_or_reason()
        if reason:
            logger.warning("[Chess] Refusing to start: %s", reason)
            try:
                await self._send_card(context, {
                    "id": "chess_unavailable",
                    "markdown": "\n".join([
                        "# 国际象棋暂时开不了",
                        f"**{reason}**",
                        "",
                        "棋盘是一张带图的卡片，图片要有地方安放才能开局。"
                        "把 Hub 升级到 0.24 就可以直接存进 QQ 素材库，"
                        "不再需要公网图床。",
                    ]),
                    "rows": [],
                    "ttl_seconds": 600,
                }, session_id=self._ui_session(context.origin, ch.SPEC))
            except Exception:
                logger.exception("[Chess] Failed to explain the refusal")
            return 1
        state = ch.new_state(mode, context.member_openid,
                             self._level(context.origin, ch.SPEC))
        state["display_name"] = ch.SPEC.title
        self._matches.start(context.origin, state)
        self._avatars[context.origin] = AvatarCache()
        try:
            if mode == ch.MODE_PVP:
                label = await self._label_of(context, context.member_openid)
                await self._send_card(
                    context, ch.build_waiting_card(state, label),
                    session_id=self._ui_session(context.origin, ch.SPEC),
                )
                return 0
            await self._send_chess_card(context.origin, state,
                                       client=context.client,
                                       interaction=context.interaction)
        except Exception:
            logger.exception("[Chess] Failed to open board")
            self._matches.pop(context.origin)
            return 1
        return 0

    async def _act_chess_join(self, context, params) -> int:
        state = self._matches.get(context.origin)
        if state is None or state.get("game") != "chess":
            return 3
        if state.get("phase") != ch.PHASE_WAITING:
            return 3
        if context.member_openid == state["players"].get(ch.WHITE, ""):
            return 4
        state["players"][ch.BLACK] = context.member_openid
        state["phase"] = ch.PHASE_PLAYING
        try:
            await self._send_chess_card(context.origin, state,
                                       client=context.client,
                                       interaction=context.interaction)
        except Exception:
            logger.exception("[Chess] Failed to open board after join")
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
                        "斗兽棋的棋盘是一张带按钮的卡片，图片要有地方安放才能"
                        "开局。把 Hub 升级到 0.24 就可以直接存进 QQ 素材库，"
                        "不再需要公网图床。",
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

    @staticmethod
    def _bytes_hashes(data: bytes) -> tuple[str, str, str]:
        md5 = hashlib.md5(data).hexdigest()
        sha1 = hashlib.sha1(data).hexdigest()
        md5_10m = hashlib.md5(data[:10_002_432]).hexdigest()
        return md5, sha1, md5_10m

    async def _send_qq_chunked_image(
        self,
        origin: str,
        image_bytes: bytes,
        file_name: str = "board.png",
        client: Any = None,
        event_id: str | None = None,
        msg_id: str | None = None,
    ) -> str:
        """使用 QQ 官方原生分片上传协议发送棋盘大图，返回发送的消息 ID。"""
        import aiohttp
        from botpy.http import Route
        from botpy.types.message import Media

        group_openid = origin.split(":")[-1]
        platform_name = origin.split(":", 1)[0]
        platform = self.context.get_platform_inst(platform_name)
        bot = getattr(platform, "bot", None) or client
        api = getattr(bot, "api", None) or getattr(client, "api", None)
        http_client = getattr(api, "_http", None)
        if not http_client:
            raise RuntimeError("无法获取 QQ Bot HTTP 客户端")

        file_size = len(image_bytes)
        md5, sha1, md5_10m = self._bytes_hashes(image_bytes)

        prepare_path = "/v2/groups/{group_id}/upload_prepare"
        finish_path = "/v2/groups/{group_id}/upload_part_finish"
        files_path = "/v2/groups/{group_openid}/files"
        prepare_kwargs = {"group_id": group_openid}
        finish_kwargs = {"group_id": group_openid}
        files_kwargs = {"group_openid": group_openid}

        prepare_payload = {
            "file_type": 1,
            "file_size": str(file_size),
            "file_name": file_name,
            "md5": md5,
            "sha1": sha1,
            "md5_10m": md5_10m,
        }
        prepare = await http_client.request(
            Route("POST", prepare_path, **prepare_kwargs), json=prepare_payload
        )
        if not isinstance(prepare, dict) or not prepare.get("upload_id"):
            raise RuntimeError(f"预上传响应异常: {prepare}")

        upload_id = str(prepare["upload_id"])
        block_size = int(prepare.get("block_size") or 5 * 1024 * 1024)
        parts = prepare.get("parts") or []
        upload_config = prepare.get("upload_config") or {}
        retry_timeout = int(upload_config.get("retry_timeout") or 300)

        timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=retry_timeout)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            uploaded_bytes = 0
            for order, part in enumerate(
                sorted(parts, key=lambda item: int(item.get("index", 0)))
            ):
                index = int(part.get("index", order))
                presigned_url = str(part.get("presigned_url") or "")
                if not presigned_url:
                    raise RuntimeError(f"分片 {index} 缺少 presigned_url")
                part_size = int(part.get("block_size") or block_size)
                remaining = file_size - uploaded_bytes
                if remaining <= 0:
                    break
                data = image_bytes[uploaded_bytes : uploaded_bytes + min(part_size, remaining)]
                if not data:
                    raise RuntimeError(f"分片 {index} 数据为空")

                async with session.put(
                    presigned_url,
                    data=data,
                    headers={"Content-Type": "application/octet-stream"},
                ) as resp:
                    if resp.status not in (200, 201, 204):
                        raise RuntimeError(f"分片 PUT 失败: HTTP {resp.status}")

                part_md5 = hashlib.md5(data).hexdigest()
                await http_client.request(
                    Route("POST", finish_path, **finish_kwargs),
                    json={
                        "upload_id": upload_id,
                        "part_index": index,
                        "block_size": str(len(data)),
                        "md5": part_md5,
                    },
                )
                uploaded_bytes += len(data)

        complete_payload = {
            "file_type": 1,
            "srv_send_msg": False,
            "file_name": file_name,
            "upload_id": upload_id,
        }
        complete = await http_client.request(
            Route("POST", files_path, **files_kwargs), json=complete_payload
        )
        if not isinstance(complete, dict) or not complete.get("file_info"):
            raise RuntimeError(f"分片合并响应缺少 file_info: {complete}")

        media = Media(
            file_uuid=complete.get("file_uuid", ""),
            file_info=complete["file_info"],
            ttl=complete.get("ttl", 0),
        )

        msg_payload = {
            "msg_type": 7,
            "msg_seq": random.randint(1, 10000),
            "media": media,
        }
        if msg_id:
            msg_payload["msg_id"] = msg_id
        if event_id:
            msg_payload["event_id"] = event_id

        # 记住会话场景
        remember_scene = getattr(platform, "remember_session_scene", None)
        if callable(remember_scene):
            remember_scene(str(group_openid), "group")

        result = await http_client.request(
            Route("POST", "/v2/groups/{group_openid}/messages", group_openid=group_openid),
            json=msg_payload,
        )
        if isinstance(result, dict):
            return str(result.get("id") or "")
        return str(getattr(result, "id", "") or "")

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

        # 优先使用官方原生分片直传，避免 Base64 上传限制或图床故障
        sent_id = ""
        try:
            sent_id = await self._send_qq_chunked_image(
                origin, image,
                file_name=f"{state.get('game', 'board')}_{int(time.time())}.png",
                client=client, event_id=event_id or None, msg_id=msg_id,
            )
        except Exception as exc:
            logger.warning(f"[TicTacToe] 官方分片直传棋盘失败，回退至 hub.send_image_message: {exc}")
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
        """Why a picture card cannot be drawn right now, or "" when it can.

        The material route (Hub >= 0.24) needs no public address at all, so its
        presence alone settles the question: nothing to tunnel, nothing to
        reach, nothing to go stale. Only when it is missing do we fall back to
        asking the image host whether it is reachable.
        """
        hub = self._get_hub(quiet=True)
        if hub is None:
            return "QQ Official Hub 未安装或未启用"
        if hasattr(hub, "publish_image_material"):
            return ""
        checker = getattr(hub, "image_host_reachable", None)
        if checker is not None:
            try:
                if not await checker():
                    return "图床未就绪；请升级 Hub 到 0.24（用素材库发包，不需要公网地址）"
            except Exception as exc:
                return f"图床检查失败：{exc}"
        return ""

    async def _send_animalchess_card(self, origin: str, state: dict[str, Any],
                                     client=None, interaction=None,
                                     msg_id: str | None = None) -> None:
        """斗兽棋：一张带按钮的卡片棋盘。"""
        from .games import animalchess_render as ar

        await self._refresh_labels_from_origin(origin, state)
        await self._send_image_card(
            origin, state, ar.render_board(state, banner=False),
            ach.SPEC, ach.build_board_card, tag="AnimalChess",
            client=client, interaction=interaction, msg_id=msg_id,
        )

    async def _send_chess_card(self, origin: str, state: dict[str, Any],
                               client=None, interaction=None,
                               msg_id: str | None = None) -> None:
        """国际象棋：图片棋盘，走法靠引用回复（64 格放不下按钮）。"""
        from .games import chess_render as cr

        await self._refresh_labels_from_origin(origin, state)
        await self._send_image_card(
            origin, state, cr.render_board(state, banner=False),
            ch.SPEC, ch.build_board_card, tag="Chess",
            client=client, interaction=interaction, msg_id=msg_id,
        )

    async def _send_image_card(self, origin: str, state: dict[str, Any],
                               image: bytes, spec, build_card, *,
                               tag: str, client=None, interaction=None,
                               msg_id: str | None = None) -> None:
        """把一张图片安全的塞进卡片发出去。三个游戏共用这一条路径。

        为什么要抽出来：卡片里的图片经过的坑（腾讯抓不到、转存失败、素材额度）
        和"是哪个游戏"完全无关。每个游戏各写一份，等于每个游戏各踩一次。

        顺序按"对外部世界有多少依赖"排：

          1. QQ 素材库 —— 字节直接进腾讯的存储，卡片里写 qqbot-media 引用。
             不需要公网地址、不需要隧道、不需要 DNS。
          2. Hub 图床的公网 URL —— 老路子，老版本 Hub 的兜底。
          3. 无图卡片 + 独立图片消息 —— 前两条都被 QQ 拒了才走到这里。
        """
        hub = self._get_hub()
        if hub is None:
            raise RuntimeError("QQ Official Hub 不可用")
        event_id = (
            self._hub_module(hub, "passive_reply").passive_event_id(interaction)
            if interaction is not None else None
        )

        candidates: list[tuple[str, str]] = []       # (图片地址, 素材 id)
        upload = getattr(hub, "publish_image_material", None)
        if upload is not None:
            try:
                material = await upload(image, client=client, origin=origin)
                if material.get("url"):
                    candidates.append((str(material["url"]),
                                       str(material.get("image_id") or "")))
            except Exception as exc:
                logger.warning("[%s] 素材上传失败，回退图床：%s", tag, exc)
        try:
            host_url = await hub.publish_image_checked(image, slot=f"{spec.key}:{origin}")
            if host_url:
                candidates.append((str(host_url), ""))
        except Exception as exc:
            logger.warning("[%s] 图床发布失败：%s", tag, exc)
        if not candidates:
            raise RuntimeError("棋盘图片无处安放：素材库和图床都不可用")

        # 带图发送并开 verify_image：转存失败会变成明确错误，我们才能换下一个
        # 来源，而不是把一张裂图交给玩家。
        session_id = self._ui_session(origin, spec)
        delivered = False
        material_id = ""
        last_error: Exception | None = None
        for url, candidate_material in candidates:
            try:
                await self._send_card_image(
                    origin, build_card(state, url), hub,
                    client=client, session_id=session_id,
                    event_id=event_id, msg_id=msg_id,
                )
            except Exception as exc:
                last_error = exc
                logger.warning("[%s] 带图卡片被拒（%s）：%s",
                               tag, url.split("?")[0], exc)
                if candidate_material:
                    await self._release_material(hub, candidate_material,
                                                 client=client, origin=origin)
                continue
            delivered = True
            material_id = candidate_material
            break

        if not delivered:
            logger.warning("[%s] 带图卡片全部失败，回退无图卡片：%s", tag, last_error)
            await hub.send_ephemeral_card(
                origin, build_card(state, ""),
                client=client, session_id=session_id,
                event_id=event_id, msg_id=msg_id,
                initiator_openid="", clicker_header="",
            )
            await hub.send_image_message(
                origin, image, client=client,
                event_id=event_id, msg_id=msg_id,
            )

        if material_id:
            await self._remember_material(hub, material_id,
                                          client=client, origin=origin)
        self._matches.touch(state)

    async def _send_card_image(self, origin: str, card: dict[str, Any], hub,
                               *, client=None, session_id: str = "",
                               event_id: str | None = None,
                               msg_id: str | None = None) -> None:
        """Send a card whose picture must survive, or raise.

        ``verify_image`` is opt-in on the Hub side because it can turn a
        delivered-but-broken message into a refused one. For a board that is
        exactly the trade we want -- but an older Hub has no such parameter, so
        it is detected by signature rather than by catching TypeError (which
        would also swallow a real failure from inside the call).
        """
        kwargs: dict[str, Any] = {
            "client": client, "session_id": session_id,
            "event_id": event_id, "msg_id": msg_id,
            "initiator_openid": "", "clicker_header": "",
        }
        if "verify_image" in inspect.signature(hub.send_ephemeral_card).parameters:
            kwargs["verify_image"] = True
        await hub.send_ephemeral_card(origin, card, **kwargs)

    async def _remember_material(self, hub, material_id: str, *,
                                 client=None, origin: str = "") -> None:
        """Track this group's material images and release the ones that fell out.

        QQ caps materials at 100 per bot. A board redraws every move, so without
        this the bot hits the cap mid-game and *then* every card fails -- a
        failure that looks nothing like its cause.
        """
        book = getattr(self, "_materials", None)
        if book is None:
            book = self._materials = {}
        bucket = book.setdefault(origin, [])
        bucket.append((time.time(), material_id))
        while len(bucket) > self.MATERIAL_KEEP:
            _, stale = bucket.pop(0)
            await self._release_material(hub, stale, client=client, origin=origin)

    async def _release_material(self, hub, material_id: str, *,
                                client=None, origin: str = "") -> None:
        cleaner = getattr(hub, "delete_image_material", None)
        if cleaner is None or not material_id:
            return
        try:
            await cleaner(material_id, client=client, origin=origin)
        except Exception:
            logger.debug("[AnimalChess] 回收素材失败 %s", material_id)

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

    @filter.platform_adapter_type(
        filter.PlatformAdapterType.QQOFFICIAL
        | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
    )
    # 和五子棋/斗兽棋一样的优先级：要压过 Hub 的兜底面板提示，否则引用的
    # 走法会先被别的处理器吞掉。
    @filter.event_message_type(filter.EventMessageType.ALL, priority=200)
    async def chess_move_by_quote(self, event: AstrMessageEvent):
        """引用棋盘图回复走法，例如 e2e4、Nf3 式写法不认、但 e2-e4 认。

        为什么不做成按钮：8x8=64 格装不下（QQ 上限 5x5），而且中局里真正合法
        的走法往往只有二三十种，"先点起点再点终点"要点两次还要重排按钮。打字
        一行 e2e4 对会下棋的人反而更快——五子棋也是同一个交互。
        """
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        if "GroupMessage" not in origin:
            return
        for dead_origin, _ in self._matches.sweep():
            self._avatars.pop(dead_origin, None)
            logger.info("[Chess] Match in %s expired",
                        dead_origin.split(":")[-1][-8:])
        state = self._matches.get(origin)
        if state is None or state.get("game") != "chess":
            return
        if state.get("phase") == ch.PHASE_WAITING:
            return          # 对手还没入座，没有棋盘可以引用
        move = ch.resolve_text(state, event.get_message_str())
        if move is None:
            return          # 不是走法（或者不合法），交给别人处理

        if not quoted_message_ids(event):
            return          # 群里随口打一句 e2e4 不算下棋
        event.stop_event()

        actor = str(event.get_sender_id() or "")
        refusal = ch.apply_move(state, move, actor)
        if refusal:
            yield event.plain_result(f"❌ {refusal}")
            return
        if not ch.is_over(state):
            # 国际象棋的 AI 是纯 CPU 搜索（困难档约 1.6 秒）。直接在事件循环里
            # 同步跑会把整个 AstrBot 卡住——期间别的群发消息也要排队。丢到
            # 线程里跑，代价是这一次 await 多几毫秒的调度开销。
            await asyncio.to_thread(ch.maybe_ai_move, state)
        self._matches.touch(state)
        try:
            await self._send_chess_card(
                origin, state,
                msg_id=str(event.message_obj.message_id or "") or None,
            )
        except Exception as exc:
            logger.exception("[Chess] Failed to refresh board")
            yield event.plain_result(
                f"走法已记录（{ch.square_name(move.frm)}→"
                f"{ch.square_name(move.to)}），但棋盘卡没发出来："
                f"{self._describe(exc)}\n发送 /棋盘 可以重新出卡。"
            )
            return
        if ch.is_over(state):
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
        if state is None or state.get("game") not in (
                "gomoku", "animalchess", "chess", "hornchess"):
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
            elif state.get("game") == "chess":
                await self._send_chess_card(origin, state, msg_id=msg_id)
            elif state.get("game") == "hornchess":
                await self._send_hornchess_card(origin, state, msg_id=msg_id)
            else:
                await self._send_picture_board(origin, state, msg_id=msg_id)
        except Exception as exc:
            logger.exception("[Games] Failed to redraw board")
            yield event.plain_result(f"出图失败：{self._describe(exc)}")

    @filter.platform_adapter_type(
        filter.PlatformAdapterType.QQOFFICIAL
        | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
    )
    @filter.platform_adapter_type(
        filter.PlatformAdapterType.QQOFFICIAL
        | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
    )
    @filter.platform_adapter_type(
        filter.PlatformAdapterType.QQOFFICIAL
        | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
    )
    @filter.platform_adapter_type(
        filter.PlatformAdapterType.QQOFFICIAL
        | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
    )
    @filter.command("认输", alias={"resign", "投子"})
    async def resign(self, event: AstrMessageEvent):
        """/认输 —— 结束本群的对局。

        国际象棋那种"棋盘上没有按钮"的棋，如果没有这条命令，玩家想停就只能等
        它超时；而这棋的每张卡片都带「结束对局」按钮，唯独它没有。
        """
        event.stop_event()
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        state = self._matches.get(origin)
        if state is None:
            yield event.plain_result("本群当前没有进行中的对局。")
            return
        name = str(state.get("display_name") or "对局")
        self._matches.pop(origin)
        self._avatars.pop(origin, None)
        yield event.plain_result(f"已结束本群的{name}。")

    @filter.platform_adapter_type(
        filter.PlatformAdapterType.QQOFFICIAL
        | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
    )
    @filter.command("牛角棋", alias={"hornchess", "niujiao", "钻牛角", "赶牛"})
    async def hornchess_from_command(self, event: AstrMessageEvent):
        """/牛角棋 —— 打开牛角棋卡片。按钮走棋，不用打字。"""
        async for result in self._lobby_from_command(event, hn.SPEC):
            yield result

    @filter.platform_adapter_type(
        filter.PlatformAdapterType.QQOFFICIAL
        | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
    )
    @filter.command("国际象棋", alias={"chess", "gjxq"})
    async def chess_from_command(self, event: AstrMessageEvent):
        """/国际象棋 —— 打开国际象棋卡片，由按钮选择对战方式与难度。"""
        async for result in self._lobby_from_command(event, ch.SPEC):
            yield result

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
