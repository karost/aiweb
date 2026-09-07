"""AI Web — thin slash command handlers (CLI + TUI).

All handlers call client.request; no Playwright here.
"""

from __future__ import annotations

import shlex
from typing import Any, Optional

from .client import format_user_message, request


def _parse_write_args(raw: str) -> tuple[Optional[str], Optional[str]]:
    """
    /aiweb-write <path> <prompt...>
    path is first token; rest is prompt.
    """
    raw = (raw or "").strip()
    if not raw:
        return None, None
    try:
        parts = shlex.split(raw)
    except ValueError:
        parts = raw.split(None, 1)
        if len(parts) == 1:
            return parts[0], None
        return parts[0], parts[1]
    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], " ".join(parts[1:])


def cmd_aiweb(arg: str = "") -> str:
    """Research on Grok; final-only inject on next Hermes model turn."""
    msg = (arg or "").strip()
    if not msg:
        return "Usage: /aiweb <prompt>"
    result = request("aiweb", message=msg)
    return format_user_message(result)


def cmd_aiweb_chat(arg: str = "") -> str:
    """Research on Grok; no model inject."""
    msg = (arg or "").strip()
    if not msg:
        return "Usage: /aiweb-chat <prompt>"
    result = request("chat", message=msg)
    return format_user_message(result)


def cmd_aiweb_write(arg: str = "") -> str:
    """Grok → file under out dir."""
    path, prompt = _parse_write_args(arg)
    if not path or not prompt:
        return "Usage: /aiweb-write <path> <prompt>"
    result = request("write", path=path, prompt=prompt)
    return format_user_message(result)


def cmd_aiweb_more(arg: str = "") -> str:
    """Next chunk of last large response."""
    result = request("more")
    return format_user_message(result)


def cmd_aiweb_login(arg: str = "") -> str:
    """Headed login / refresh session."""
    result = request("login")
    return format_user_message(result)


def cmd_aiweb_stop(arg: str = "") -> str:
    """
    Close browser. Optional: /aiweb-stop daemon  → stop daemon process too.
    """
    raw = (arg or "").strip().lower()
    stop_daemon = raw in ("daemon", "--daemon", "all")
    result = request("stop", daemon=stop_daemon, stop_daemon=stop_daemon)
    return format_user_message(result)


def cmd_aiweb_status(arg: str = "") -> str:
    result = request("status")
    return format_user_message(result)


def cmd_aiweb_clear_model(arg: str = "") -> str:
    result = request("clear_model")
    return format_user_message(result)


def cmd_aiweb_keep_model(arg: str = "") -> str:
    result = request("keep_model")
    return format_user_message(result)


def cmd_aiweb_run(arg: str = "") -> str:
    msg = (arg or "").strip()
    if not msg:
        return "Usage: /aiweb-run <prompt>"
    result = request("run", message=msg)
    return format_user_message(result)


def cmd_aiweb_load(arg: str = "") -> str:
    result = request("load", message=(arg or "").strip())
    return format_user_message(result)


def cmd_aiweb_reset(arg: str = "") -> str:
    result = request("reset_memory")
    return format_user_message(result)


def cmd_aiweb_summary(arg: str = "") -> str:
    result = request("summary", message=(arg or "").strip())
    return format_user_message(result)


COMMAND_HANDLERS = {
    "aiweb": cmd_aiweb,
    "aiweb-chat": cmd_aiweb_chat,
    "aiweb-write": cmd_aiweb_write,
    "aiweb-more": cmd_aiweb_more,
    "aiweb-login": cmd_aiweb_login,
    "aiweb-stop": cmd_aiweb_stop,
    "aiweb-status": cmd_aiweb_status,
    "aiweb-clear-model": cmd_aiweb_clear_model,
    "aiweb-keep-model": cmd_aiweb_keep_model,
    "aiweb-run": cmd_aiweb_run,
    "aiweb-load": cmd_aiweb_load,
    "aiweb-reset": cmd_aiweb_reset,
    "aiweb-summary": cmd_aiweb_summary,
}


def dispatch(name: str, arg: str = "") -> str:
    fn = COMMAND_HANDLERS.get(name)
    if not fn:
        return f"Unknown AI Web command: {name}"
    try:
        return fn(arg)
    except Exception as e:  # noqa: BLE001
        return f"❌ AI Web command error: {e}"


__all__ = [
    "COMMAND_HANDLERS",
    "dispatch",
    "cmd_aiweb",
    "cmd_aiweb_chat",
    "cmd_aiweb_write",
    "cmd_aiweb_more",
    "cmd_aiweb_login",
    "cmd_aiweb_stop",
    "cmd_aiweb_status",
    "cmd_aiweb_clear_model",
    "cmd_aiweb_keep_model",
    "cmd_aiweb_run",
    "cmd_aiweb_load",
    "cmd_aiweb_reset",
    "cmd_aiweb_summary",
]