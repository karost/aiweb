"""AI Web V3 — session state (daemon-side). Tracks conversation URL for continuity."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from . import memory_manager as mem


class SessionState(str, Enum):
    BROWSER_DOWN = "BrowserDown"
    READY = "Ready"
    BUSY = "Busy"
    NEEDS_LOGIN = "NeedsLogin"


HEAVY_OPS = frozenset({"aiweb", "chat", "write", "login", "run", "load", "new"})
CONTROL_OPS = frozenset({"status", "more", "clear_model", "keep_model", "hello", "reset_memory", "summary", "stop"})


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
    daemon_pid: Optional[int] = None
    updated_at: float = field(default_factory=time.time)

    def session_alive(self) -> bool:
        return bool(self.browser_up and self.state in (SessionState.READY, SessionState.BUSY, SessionState.NEEDS_LOGIN))

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "browser_up": self.browser_up,
            "state": self.state.value,
            "page_url": self.page_url,
            "conversation_url": self.conversation_url,
            "login_required": self.login_required or self.state == SessionState.NEEDS_LOGIN,
            "session_alive": self.session_alive(),
            "busy": self.busy or self.state == SessionState.BUSY,
            "inject_pending": mem.inject_pending(),
            "last_gen_id": self.last_gen_id,
            "daemon_pid": self.daemon_pid or os.getpid(),
        }


class SessionManager:
    def __init__(self) -> None:
        self.status = SessionStatus()
        self.engine: Any = None
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
        self.status.conversation_url = None
        self.status.busy = False
        self._heavy_lock = False
        self._touch()

    def mark_ready(self, *, page_url: Optional[str] = None, conversation_url: Optional[str] = None) -> None:
        self.status.browser_up = True
        self.status.state = SessionState.READY
        self.status.busy = False
        self._heavy_lock = False
        if page_url is not None:
            self.status.page_url = page_url
        if conversation_url is not None:
            self.status.conversation_url = conversation_url
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
            self.status.state = SessionState.NEEDS_LOGIN if self.status.login_required else SessionState.READY
        else:
            self.status.state = SessionState.BROWSER_DOWN
        self._touch()

    def can_run(self, op: str) -> tuple[bool, Optional[str]]:
        if op in CONTROL_OPS:
            return True, None
        if op in HEAVY_OPS:
            if self._heavy_lock or self.status.state == SessionState.BUSY:
                return False, "busy"
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
        try:
            path = mem.data_dir() / "state.json"
            path.write_text(json.dumps(self.status.to_public_dict(), indent=2), encoding="utf-8")
        except OSError:
            pass


_SESSION: Optional[SessionManager] = None


def get_session() -> SessionManager:
    global _SESSION
    if _SESSION is None:
        _SESSION = SessionManager()
    return _SESSION


__all__ = ["SessionState", "SessionStatus", "SessionManager", "get_session", "HEAVY_OPS", "CONTROL_OPS"]