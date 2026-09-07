"""AI Web — session state machine helpers (daemon-side).

States: DaemonDown | BrowserDown | Ready | Busy | NeedsLogin
BrowserEngine is attached by the daemon; this module tracks flags only.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from . import memory_manager as mem


class SessionState(str, Enum):
    DAEMON_DOWN = "DaemonDown"   # client-side only normally
    BROWSER_DOWN = "BrowserDown"
    READY = "Ready"
    BUSY = "Busy"
    NEEDS_LOGIN = "NeedsLogin"


HEAVY_OPS = frozenset({"aiweb", "chat", "write", "login", "run", "load"})
CONTROL_OPS = frozenset({"status", "more", "clear_model", "keep_model", "hello", "reset_memory", "summary"})
STOP_OPS = frozenset({"stop"})


@dataclass
class SessionStatus:
    state: SessionState = SessionState.BROWSER_DOWN
    browser_up: bool = False
    daemon_up: bool = True
    page_url: Optional[str] = None
    login_required: bool = False
    last_op_ok: Optional[bool] = None
    last_error: Optional[str] = None
    last_gen_id: Optional[str] = None
    busy: bool = False
    daemon_pid: Optional[int] = None
    protocol_version: int = 1
    updated_at: float = field(default_factory=time.time)

    def session_alive(self) -> bool:
        return bool(
            self.daemon_up
            and self.browser_up
            and self.state
            in (SessionState.READY, SessionState.BUSY, SessionState.NEEDS_LOGIN)
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "daemon_up": self.daemon_up,
            "browser_up": self.browser_up,
            "state": self.state.value,
            "page_url": self.page_url,
            "login_required": self.login_required or self.state == SessionState.NEEDS_LOGIN,
            "session_alive": self.session_alive(),
            "busy": self.busy or self.state == SessionState.BUSY,
            "inject_pending": mem.inject_pending(),
            "last_gen_id": self.last_gen_id,
            "last_op_ok": self.last_op_ok,
            "last_error": self.last_error,
            "daemon_pid": self.daemon_pid or os.getpid(),
            "protocol_version": self.protocol_version,
        }


class SessionManager:
    """
    In-daemon singleton-style manager.
    Does not launch Playwright itself — holds engine reference set by daemon/service.
    """

    def __init__(self) -> None:
        self.status = SessionStatus()
        self.engine: Any = None  # BrowserEngine | None
        self._heavy_lock = False

    def bind_engine(self, engine: Any) -> None:
        self.engine = engine
        self.status.browser_up = engine is not None and getattr(engine, "is_up", lambda: False)()
        if self.status.browser_up and self.status.state == SessionState.BROWSER_DOWN:
            self.status.state = SessionState.READY
        self._touch()

    def mark_browser_down(self) -> None:
        self.engine = None
        self.status.browser_up = False
        self.status.state = SessionState.BROWSER_DOWN
        self.status.page_url = None
        self.status.busy = False
        self._heavy_lock = False
        self._touch()

    def mark_ready(self, *, page_url: Optional[str] = None) -> None:
        self.status.browser_up = True
        self.status.state = SessionState.READY
        self.status.busy = False
        self._heavy_lock = False
        if page_url is not None:
            self.status.page_url = page_url
        self.status.login_required = False
        self._touch()

    def mark_needs_login(self, *, page_url: Optional[str] = None) -> None:
        self.status.browser_up = True
        self.status.state = SessionState.NEEDS_LOGIN
        self.status.login_required = True
        self.status.busy = False
        self._heavy_lock = False
        if page_url is not None:
            self.status.page_url = page_url
        self._touch()

    def try_begin_heavy(self) -> bool:
        """Return False if another heavy op holds the session."""
        if self._heavy_lock or self.status.state == SessionState.BUSY:
            return False
        self._heavy_lock = True
        self.status.busy = True
        self.status.state = SessionState.BUSY
        self._touch()
        return True

    def end_heavy(self, *, ok: bool, error: Optional[str] = None) -> None:
        self._heavy_lock = False
        self.status.busy = False
        self.status.last_op_ok = ok
        self.status.last_error = error
        if self.status.browser_up:
            if self.status.login_required:
                self.status.state = SessionState.NEEDS_LOGIN
            else:
                self.status.state = SessionState.READY
        else:
            self.status.state = SessionState.BROWSER_DOWN
        self._touch()

    def can_run(self, op: str) -> tuple[bool, Optional[str]]:
        """Concurrency matrix: heavy blocked when busy; control/stop allowed."""
        if op in CONTROL_OPS or op in STOP_OPS:
            return True, None
        if op in HEAVY_OPS:
            if self._heavy_lock or self.status.state == SessionState.BUSY:
                return False, "busy"
            return True, None
        return True, None

    def set_last_gen(self, gen_id: Optional[str]) -> None:
        self.status.last_gen_id = gen_id
        self._touch()

    def persist_state_file(self) -> None:
        path = mem.data_dir() / "state.json"
        payload = self.status.to_public_dict()
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _touch(self) -> None:
        self.status.updated_at = time.time()
        self.status.daemon_pid = os.getpid()
        try:
            self.persist_state_file()
        except OSError:
            pass


# Module-level instance used by service/daemon in-process
_SESSION: Optional[SessionManager] = None


def get_session() -> SessionManager:
    global _SESSION
    if _SESSION is None:
        _SESSION = SessionManager()
    return _SESSION


def reset_session_for_tests() -> None:
    global _SESSION
    _SESSION = SessionManager()


__all__ = [
    "SessionState",
    "SessionStatus",
    "SessionManager",
    "HEAVY_OPS",
    "CONTROL_OPS",
    "STOP_OPS",
    "get_session",
    "reset_session_for_tests",
]