"""AI Web — thin IPC client (Hermes slash process).

ensure_daemon() + request(op, **args) → AIWebResult dict.
Never imports BrowserEngine / Playwright.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from . import memory_manager as mem
from .service import PROTOCOL_VERSION

SOCK_NAME = "daemon.sock"
PID_NAME = "daemon.pid"
CONNECT_TIMEOUT = 2.0
REQUEST_TIMEOUT = float(os.environ.get("HERMES_AIWEB_CLIENT_TIMEOUT", "600"))
SPAWN_WAIT_SEC = 25.0


def _sock_path() -> Path:
    return mem.data_dir() / SOCK_NAME


def _pid_path() -> Path:
    return mem.data_dir() / PID_NAME


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_pid() -> Optional[int]:
    p = _pid_path()
    if not p.exists():
        return None
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _plugin_root() -> Path:
    return Path(__file__).resolve().parent


def _spawn_daemon() -> None:
    """Start daemon as detached subprocess using same Python."""
    mem.data_dir().mkdir(parents=True, exist_ok=True)
    plugins_dir = _plugin_root().parent  # .../plugins
    env = os.environ.copy()
    env.setdefault("HERMES_HOME", str(mem.data_dir().parent.parent))

    # Prefer: python -m aiweb.daemon with plugins on PYTHONPATH
    cmd = [sys.executable, "-m", "aiweb.daemon"]
    try:
        subprocess.Popen(
            cmd,
            cwd=str(plugins_dir),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        # Fallback: run daemon.py as file
        daemon_py = _plugin_root() / "daemon.py"
        subprocess.Popen(
            [sys.executable, str(daemon_py)],
            cwd=str(plugins_dir),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def _connect() -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(CONNECT_TIMEOUT)
    s.connect(str(_sock_path()))
    s.settimeout(REQUEST_TIMEOUT)
    return s


def _recv_json(sock: socket.socket) -> dict[str, Any]:
    buf = bytearray()
    while True:
        if b"\n" in buf:
            line, _, rest = buf.partition(b"\n")
            return json.loads(line.decode("utf-8"))
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("daemon closed connection")
        buf.extend(chunk)


def _send_json(sock: socket.socket, obj: dict[str, Any]) -> None:
    sock.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))


def _handshake(sock: socket.socket) -> dict[str, Any]:
    rid = str(uuid.uuid4())
    _send_json(
        sock,
        {
            "id": rid,
            "op": "hello",
            "request_id": rid,
            "args": {
                "protocol_version": PROTOCOL_VERSION,
                "client_version": "2.0.0",
            },
        },
    )
    resp = _recv_json(sock)
    if not resp.get("ok"):
        code = resp.get("error_code") or "protocol_mismatch"
        raise RuntimeError(
            f"daemon hello failed: {resp.get('error') or code} "
            f"(daemon protocol={resp.get('protocol_version')})"
        )
    if int(resp.get("protocol_version") or 0) != PROTOCOL_VERSION:
        raise RuntimeError(
            f"protocol_mismatch: client={PROTOCOL_VERSION} "
            f"daemon={resp.get('protocol_version')}"
        )
    return resp


def daemon_is_live() -> bool:
    pid = _read_pid()
    if pid is None or not _pid_alive(pid):
        return False
    if not _sock_path().exists():
        return False
    try:
        sock = _connect()
        try:
            _handshake(sock)
            return True
        finally:
            sock.close()
    except Exception:
        return False


def ensure_daemon() -> None:
    """Spawn daemon if needed; wait until hello succeeds."""
    if daemon_is_live():
        return

    # Stale pid/sock cleanup
    pid = _read_pid()
    if pid is not None and not _pid_alive(pid):
        for p in (_pid_path(), _sock_path()):
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass

    _spawn_daemon()
    deadline = time.time() + SPAWN_WAIT_SEC
    last_err = "timeout"
    while time.time() < deadline:
        time.sleep(0.25)
        try:
            if daemon_is_live():
                return
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
    raise RuntimeError(
        "spawn_failed: could not start AI Web daemon. "
        f"Last error: {last_err}. "
        "Check: pip install playwright && playwright install chromium; "
        f"data dir={mem.data_dir()}"
    )


def request(op: str, **args: Any) -> dict[str, Any]:
    """
    Ensure daemon, send op, return Result dict.
    On dead socket: respawn once and retry.
    """
    request_id = str(args.pop("request_id", None) or uuid.uuid4())
    payload = {
        "id": request_id,
        "op": op,
        "request_id": request_id,
        "args": args,
    }

    def _once() -> dict[str, Any]:
        ensure_daemon()
        sock = _connect()
        try:
            _handshake(sock)
            _send_json(sock, payload)
            return _recv_json(sock)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    try:
        return _once()
    except (ConnectionError, OSError, socket.error) as e:
        # One retry after forced respawn
        for p in (_pid_path(), _sock_path()):
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        try:
            return _once()
        except Exception as e2:  # noqa: BLE001
            return {
                "ok": False,
                "message": f"spawn_failed / connection error: {e2}",
                "request_id": request_id,
                "session_alive": False,
                "state": "DaemonDown",
                "busy": False,
                "error": str(e2),
                "error_code": "spawn_failed",
                "artifacts": [],
                "path": "none",
                "chars": 0,
                "full_path": None,
                "gen_id": None,
                "more_available": False,
                "inject": {
                    "written": False,
                    "pending": False,
                    "chars": 0,
                    "mode": "none",
                    "distill_method": None,
                    "capped": False,
                    "cap": None,
                },
                "op": op,
            }


def format_user_message(result: dict[str, Any]) -> str:
    """Slash handlers return this string to Hermes."""
    if not result:
        return "❌ AI Web: empty result"
    if result.get("ok"):
        return result.get("message") or "✅ OK"
    code = result.get("error_code") or ""
    msg = result.get("message") or result.get("error") or "error"
    prefix = f"❌ [{code}] " if code else "❌ "
    return prefix + str(msg)


__all__ = [
    "ensure_daemon",
    "request",
    "daemon_is_live",
    "format_user_message",
    "PROTOCOL_VERSION",
]