"""AI Web V3 — thin slash command handlers. All work goes through client.request."""

from __future__ import annotations

import shlex
from typing import Any, Optional

from .client import format_user_message, request


def _parse_write_args(raw: str) -> tuple[Optional[str], Optional[str]]:
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
    msg = (arg or "").strip()
    if not msg:
        return "Usage: /aiweb <prompt>"
    return format_user_message(request("aiweb", message=msg))


def cmd_aiweb_chat(arg: str = "") -> str:
    msg = (arg or "").strip()
    if not msg:
        return "Usage: /aiweb-chat <prompt>"
    return format_user_message(request("chat", message=msg))


def cmd_aiweb_new(arg: str = "") -> str:
    """Only command that starts a fresh Grok conversation."""
    extra = (arg or "").strip()
    return format_user_message(request("new", message=extra))


def cmd_aiweb_write(arg: str = "") -> str:
    path, prompt = _parse_write_args(arg)
    if not path or not prompt:
        return "Usage: /aiweb-write <path> <prompt>"
    return format_user_message(request("write", path=path, prompt=prompt))


def cmd_aiweb_more(arg: str = "") -> str:
    return format_user_message(request("more"))


def cmd_aiweb_login(arg: str = "") -> str:
    return format_user_message(request("login"))


def cmd_aiweb_stop(arg: str = "") -> str:
    raw = (arg or "").strip().lower()
    stop_daemon = raw in ("daemon", "--daemon", "all")
    return format_user_message(request("stop", daemon=stop_daemon, stop_daemon=stop_daemon))


def cmd_aiweb_status(arg: str = "") -> str:
    return format_user_message(request("status"))


def cmd_aiweb_clear_model(arg: str = "") -> str:
    return format_user_message(request("clear_model"))


def cmd_aiweb_keep_model(arg: str = "") -> str:
    return format_user_message(request("keep_model"))


def cmd_aiweb_run(arg: str = "") -> str:
    msg = (arg or "").strip()
    if not msg:
        return "Usage: /aiweb-run <prompt>"
    return format_user_message(request("run", message=msg))


def cmd_aiweb_load(arg: str = "") -> str:
    return format_user_message(request("load", message=(arg or "").strip()))


def cmd_aiweb_reset(arg: str = "") -> str:
    return format_user_message(request("reset_memory"))


def cmd_aiweb_summary(arg: str = "") -> str:
    return format_user_message(request("summary", message=(arg or "").strip()))


COMMAND_HANDLERS = {
    "aiweb": cmd_aiweb,
    "aiweb-chat": cmd_aiweb_chat,
    "aiweb-new": cmd_aiweb_new,
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
    except Exception as e:
        return f"❌ AI Web command error: {e}"


__all__ = ["COMMAND_HANDLERS", "dispatch"]