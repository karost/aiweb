"""AI Web V3 — session state (daemon-side).

Tracks conversation URL for same-chat continuity and serializes heavy
browser ops so CLI+TUI callers queue instead of failing with "busy".

The heavy slot is a real threading.Lock:
  - begin_heavy_blocking() waits (timeout) instead of instant-reject
  - end_heavy() releases only if *this thread* holds the slot
  - mark_ready / mark_needs_login / mark_browser_down never steal the lock
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator, Optional

from . import memory_manager as mem


class SessionState(str, Enum):
    BROWSER_DOWN = "BrowserDown"
    READY = "Ready"
    BUSY = "Busy"
    NEEDS_LOGIN = "NeedsLogin"


HEAVY_OPS = frozenset({"aiweb", "chat", "write", "login", "run", "load", "new"})
CONTROL_OPS = frozenset(
    {
        "status",
        "more",
        "clear_model",
        "keep_model",
        "hello",
        "reset_memory",
        "summary",
        "stop",
    }
)


def _queue_timeout_sec() -> float:
    try:
        return max(1.0, float(os.environ.get("HERMES_AIWEB_QUEUE_TIMEOUT", "300")))
    except ValueError:
        return 300.0


@dataclass
class SessionStatus:
    state: SessionState = SessionState.BROWSER_DOWN
    browser_up: bool = False
    page_url: Optional[str] = None
    conversation_url: Optional[str] = None  # key for same-chat continuity
    login_required: bool = False
    last_op_ok: Optional[bool] = None
    last_error: Optional[str] = None
    last_gen_id: Optional[str] = None
    busy: bool = False
    queue_depth: int = 0  # waiters currently blocked in begin_heavy_blocking
    daemon_pid: Optional[int] = None
    updated_at: float = field(default_factory=time.time)

    def session_alive(self) -> bool:
        return bool(
            self.browser_up
            and self.state
            in (SessionState.READY, SessionState.BUSY, SessionState.NEEDS_LOGIN)
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "browser_up": self.browser_up,
            "state": self.state.value,
            "page_url": self.page_url,
            "conversation_url": self.conversation_url,
            "login_required": self.login_required
            or self.state == SessionState.NEEDS_LOGIN,
            "session_alive": self.session_alive(),
            "busy": self.busy or self.state == SessionState.BUSY,
            "queue_depth": self.queue_depth,
            "inject_pending": mem.inject_pending(),
            "last_gen_id": self.last_gen_id,
            "daemon_pid": self.daemon_pid or os.getpid(),
        }


class SessionManager:
    def __init__(self) -> None:
        self.status = SessionStatus()
        self.engine: Any = None
        # Serializes heavy browser ops. NOT a fair FIFO — OS wakeup order.
        self._heavy_gate = threading.Lock()
        self._queue_lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._queue_depth = 0
        # threading.get_ident() of the slot holder, or None.
        # end_heavy() is a no-op unless this matches the caller.
        self._holder_ident: Optional[int] = None
        self._restore_persisted_conversation()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _restore_persisted_conversation(self) -> None:
        """Reload last conversation_url from state.json (daemon restart)."""
        path = mem.data_dir() / "state.json"
        try:
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        url = data.get("conversation_url")
        if isinstance(url, str) and url.strip() and "grok" in url.lower():
            self.status.conversation_url = url.strip()
        page = data.get("page_url")
        if isinstance(page, str) and page.strip():
            self.status.page_url = page.strip()

    def bind_engine(self, engine: Any) -> None:
        self.engine = engine
        self.status.browser_up = engine is not None and getattr(
            engine, "is_up", lambda: False
        )()
        if self.status.browser_up and self.status.state == SessionState.BROWSER_DOWN:
            self.status.state = SessionState.READY
        self._touch()

    def mark_browser_down(self) -> None:
        """Engine is gone. Does *not* release the heavy slot — holder must end_heavy."""
        self.engine = None
        self.status.browser_up = False
        self.status.page_url = None
        self.status.conversation_url = None
        if self._holder_ident is None:
            self.status.busy = False
            self.status.state = SessionState.BROWSER_DOWN
        else:
            # Slot still held by a handler that is about to fail / retry.
            self.status.state = SessionState.BROWSER_DOWN
        self._touch()

    def mark_ready(
        self,
        *,
        page_url: Optional[str] = None,
        conversation_url: Optional[str] = None,
    ) -> None:
        """Record a healthy page. Does *not* drop the heavy slot or lie about busy."""
        self.status.browser_up = True
        self.status.login_required = False
        if page_url is not None:
            self.status.page_url = page_url
        if conversation_url is not None:
            self.status.conversation_url = conversation_url
        if self._holder_ident is None:
            self.status.busy = False
            self.status.state = SessionState.READY
        self._touch()

    def mark_needs_login(self, *, page_url: Optional[str] = None) -> None:
        self.status.browser_up = True
        self.status.login_required = True
        if page_url is not None:
            self.status.page_url = page_url
        if self._holder_ident is None:
            self.status.busy = False
            self.status.state = SessionState.NEEDS_LOGIN
        self._touch()

    # ------------------------------------------------------------------
    # Heavy-op slot (queue, don't instant-reject)
    # ------------------------------------------------------------------

    def begin_heavy_blocking(
        self, *, timeout: Optional[float] = None
    ) -> tuple[bool, Optional[str]]:
        """Block until the heavy-op slot is free, or time out.

        Waiters are serialized by threading.Lock (not a guaranteed FIFO).
        Returns (True, None) if this thread now holds the slot; caller
        MUST call end_heavy() (or use heavy_op()) to release it.
        """
        if timeout is None:
            timeout = _queue_timeout_sec()
        ident = threading.get_ident()
        if self._holder_ident == ident:
            return False, "reentrant heavy-op on the same thread is not allowed"

        with self._queue_lock:
            self._queue_depth += 1
            depth_at_entry = self._queue_depth
            self.status.queue_depth = self._queue_depth
        self._touch()

        acquired = False
        try:
            acquired = self._heavy_gate.acquire(timeout=timeout)
        finally:
            with self._queue_lock:
                self._queue_depth = max(0, self._queue_depth - 1)
                self.status.queue_depth = self._queue_depth

        if not acquired:
            self._touch()
            behind = max(0, depth_at_entry - 1)
            return (
                False,
                f"timeout after {timeout:.0f}s waiting "
                f"(queue depth at entry {depth_at_entry}, ~{behind} ahead/running)",
            )

        self._holder_ident = ident
        self.status.busy = True
        self.status.state = SessionState.BUSY
        self._touch()
        return True, None

    def try_begin_heavy(self) -> bool:
        """Non-blocking acquire. Kept so leftover call sites still work."""
        ok, _ = self.begin_heavy_blocking(timeout=0.0)
        return ok

    def end_heavy(self, *, ok: bool, error: Optional[str] = None) -> None:
        """Release the slot if *this thread* holds it. Idempotent; never steals."""
        ident = threading.get_ident()
        self.status.last_op_ok = ok
        self.status.last_error = error

        if self._holder_ident != ident:
            # Spurious end (or already released). Do not unlock another holder.
            self._touch()
            return

        self._holder_ident = None
        self.status.busy = False
        if self.status.browser_up:
            self.status.state = (
                SessionState.NEEDS_LOGIN
                if self.status.login_required
                else SessionState.READY
            )
        else:
            self.status.state = SessionState.BROWSER_DOWN
        self._touch()
        try:
            self._heavy_gate.release()
        except RuntimeError:
            # Already unlocked (should not happen if holder_ident was set).
            pass

    @contextmanager
    def heavy_op(
        self, *, timeout: Optional[float] = None
    ) -> Iterator[tuple[bool, Optional[str]]]:
        """Context manager: acquire slot, always release on exit if we hold it."""
        acquired, reason = self.begin_heavy_blocking(timeout=timeout)
        try:
            yield acquired, reason
        finally:
            if acquired:
                # If the handler already called end_heavy(), this is a no-op.
                self.end_heavy(ok=self.status.last_op_ok is not False)

    def can_run(self, op: str) -> tuple[bool, Optional[str]]:
        """Control ops always run. Heavy ops are queued in the handler, not rejected here."""
        if op in CONTROL_OPS:
            return True, None
        if op in HEAVY_OPS:
            return True, None
        return True, None

    def set_last_gen(self, gen_id: Optional[str]) -> None:
        self.status.last_gen_id = gen_id
        self._touch()

    def set_conversation_url(self, url: Optional[str]) -> None:
        self.status.conversation_url = url
        self._touch()

    def _touch(self) -> None:
        self.status.updated_at = time.time()
        self.status.daemon_pid = os.getpid()
        self.status.queue_depth = self._queue_depth
        try:
            path = mem.data_dir() / "state.json"
            payload = json.dumps(self.status.to_public_dict(), indent=2)
            with self._io_lock:
                path.write_text(payload, encoding="utf-8")
        except OSError:
            pass


_SESSION: Optional[SessionManager] = None
_SESSION_LOCK = threading.Lock()


def get_session() -> SessionManager:
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:
                _SESSION = SessionManager()
    return _SESSION


__all__ = [
    "SessionState",
    "SessionStatus",
    "SessionManager",
    "get_session",
    "HEAVY_OPS",
    "CONTROL_OPS",
]