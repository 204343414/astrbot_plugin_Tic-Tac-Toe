"""One live match per group, with an idle deadline and an avatar cache.

Why one per group
-----------------
Every match owns the group's attention: it sends cards or images that everyone
sees, and a picture board asks players to quote it. Two concurrent matches in
one group would make "which board am I replying to?" ambiguous and double the
message volume against QQ's passive-reply budget.

Why a deadline
--------------
An abandoned match would hold that single slot forever. Matches are therefore
stamped on every move and swept lazily -- no background task, so nothing keeps
running when the plugin is unloaded.

Avatars are cached per match rather than globally: a board is redrawn every
turn, and re-downloading two pictures each time would dominate the response
latency. They live and die with the match, so a user who changes their avatar
sees it update on their next game.
"""
from __future__ import annotations

import time
from typing import Any

DEFAULT_IDLE_TIMEOUT = 90


class MatchRegistry:
    """Tracks the single active match of each group origin."""

    def __init__(self, idle_timeout: int = DEFAULT_IDLE_TIMEOUT) -> None:
        self.idle_timeout = max(int(idle_timeout), 0)
        self._matches: dict[str, dict[str, Any]] = {}

    # --- lifecycle ----------------------------------------------------------

    def get(self, origin: str, now: float | None = None) -> dict[str, Any] | None:
        """Return the live match, sweeping it first if it has gone stale."""
        if self.expired(origin, now):
            self._matches.pop(origin, None)
        return self._matches.get(origin)

    def start(self, origin: str, state: dict[str, Any],
              now: float | None = None) -> dict[str, Any]:
        state["touched_at"] = time.time() if now is None else now
        self._matches[origin] = state
        return state

    def pop(self, origin: str) -> dict[str, Any] | None:
        return self._matches.pop(origin, None)

    def touch(self, state: dict[str, Any], now: float | None = None) -> None:
        state["touched_at"] = time.time() if now is None else now

    def clear(self) -> None:
        self._matches.clear()

    # --- expiry -------------------------------------------------------------

    def expired(self, origin: str, now: float | None = None) -> bool:
        state = self._matches.get(origin)
        if state is None or not self.idle_timeout:
            return False
        now = time.time() if now is None else now
        return (now - float(state.get("touched_at", 0))) > self.idle_timeout

    def seconds_left(self, origin: str, now: float | None = None) -> int:
        state = self._matches.get(origin)
        if state is None or not self.idle_timeout:
            return 0
        now = time.time() if now is None else now
        return max(0, int(self.idle_timeout - (now - float(state.get("touched_at", 0)))))

    def sweep(self, now: float | None = None) -> list[tuple[str, dict[str, Any]]]:
        """Drop every stale match, returning them so callers can clean up."""
        if not self.idle_timeout:
            return []
        now = time.time() if now is None else now
        dead = [
            (origin, state) for origin, state in self._matches.items()
            if (now - float(state.get("touched_at", 0))) > self.idle_timeout
        ]
        for origin, _ in dead:
            self._matches.pop(origin, None)
        return dead

    # --- busy reporting -----------------------------------------------------

    def busy_reason(self, origin: str, now: float | None = None) -> str:
        """Explain why a new match cannot start, or "" when the slot is free."""
        state = self.get(origin, now)
        if state is None:
            return ""
        name = str(state.get("display_name") or "对局")
        left = self.seconds_left(origin, now)
        tail = f"，{left} 秒无人落子后自动解散" if left else ""
        return f"本群已有一局{name}进行中{tail}。可点「结束对局」或等待其自动解散。"


class AvatarCache:
    """Per-match avatar bytes, fetched once and reused for every redraw."""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    async def get_many(
        self,
        openids: dict[str, str],
        fetch,
    ) -> dict[str, bytes]:
        """Map mark -> avatar bytes, downloading only what is missing.

        ``fetch`` is an async callable taking an OpenID and returning bytes or
        None. Failures are cached as empty so a broken avatar does not trigger
        a download on every single turn.
        """
        result: dict[str, bytes] = {}
        for mark, openid in openids.items():
            if not openid:
                continue
            if openid not in self._data:
                try:
                    self._data[openid] = await fetch(openid) or b""
                except Exception:
                    self._data[openid] = b""
            data = self._data[openid]
            if data:
                result[mark] = data
        return result


def quoted_message_ids(event: object) -> list[str]:
    """Every message id the event quotes; empty when it quotes nothing.

    Lives here rather than in ``main.py`` so it can be tested without AstrBot
    installed -- this is the check that decides whether a coordinate is a move,
    and it silently ate a real one once already.

    It returns ids instead of a yes/no verdict on purpose. Demanding an exact
    match against the board's id made quoted moves vanish: the id QQ echoes
    when the picture is sent is not always the id it reports back on the quote.
    A Reply component with no usable id still yields ``["?"]``, because "they
    quoted something" is the fact the caller needs.
    """
    ids: list[str] = []
    for component in (getattr(getattr(event, "message_obj", None), "message", None) or []):
        if not str(getattr(component, "type", "")).endswith("Reply"):
            continue
        ids.append(str(getattr(component, "id", "") or ""))
    return [value for value in ids if value] or (["?"] if ids else [])
