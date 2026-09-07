"""AI Web V3 — thin IPC client (Hermes slash process). Never imports Playwright."""

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

# Keep this local. Importing .service pulls browser_engine → Playwright into the
# TUI/slash process, which is the failure mode this client exists to avoid.
PROTOCOL_VERSION = 1

SOCK_NAME = "daemon.sock"
PID_NAME = "daemon.pid"
CONNECT_TIMEOUT = 2.0
REQUEST_TIMEOUT = float(os.environ.get("HERMES_AIWEB_CLIENT_TIMEOUT", "600"))
SPAWN_WAIT_SEC = 25.0
MAX_SPAWN_LOG_BYTES = 1_000_000  # 1 MB cap at spawn time
SPAWN_LOG_KEEP = MAX_SPAWN_LOG_BYTES // 2


def _sock_path() -> Path:
    return mem.data_dir() / SOCK_NAME


def _pid_path() -> Path:
    return mem.data_dir() / PID_NAME


def _spawn_log_path() -> Path:
    return mem.data_dir() / "daemon_spawn.err"


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


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()


def _hermes_plugins_dir() -> Path:
    return _hermes_home() / "plugins"


def _daemon_python() -> str:
    env = (os.environ.get("HERMES_AIWEB_PYTHON") or "").strip()
    if env and Path(env).is_file():
        return env
    candidate = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
    if candidate.is_file():
        return str(candidate)
    return sys.executable


def _verbose_errors() -> bool:
    return os.environ.get("HERMES_AIWEB_VERBOSE_ERRORS", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _tail_bytes(path: Path, max_bytes: int) -> bytes:
    """Read at most the last max_bytes of path without loading the whole file."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            return f.read()
    except OSError:
        return b""


def _tail_text(path: Path, max_bytes: int) -> str:
    data = _tail_bytes(path, max_bytes)
    if not data:
        return ""
    text = data.decode("utf-8", errors="replace")
    if len(data) >= max_bytes:
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1 :]
    return text


def _rotate_spawn_log(path: Path) -> None:
    """Trim daemon_spawn.err at spawn time (old writer must already be dead).

    Does not cap a keep-alive daemon's open fd — that requires daemon-side
    rotation. Uses seek, never read_text() of the whole file.
    """
    try:
        if not path.exists():
            return
        size = path.stat().st_size
        if size <= MAX_SPAWN_LOG_BYTES:
            return
        text = _tail_text(path, SPAWN_LOG_KEEP)
        path.write_text(text, encoding="utf-8")
    except OSError:
        pass


def _spawn_daemon() -> None:
    mem.data_dir().mkdir(parents=True, exist_ok=True)
    plugins_dir = _hermes_plugins_dir()
    env = os.environ.copy()
    env["HERMES_HOME"] = str(_hermes_home())
    py = _daemon_python()
    cmd = [py, "-m", "aiweb.daemon"]

    err_path = _spawn_log_path()
    _rotate_spawn_log(err_path)
    err_f = open(err_path, "a", encoding="utf-8")
    err_f.write(
        f"\n--- spawn {time.strftime('%Y-%m-%d %H:%M:%S')} "
        f"cmd={cmd!r} cwd={str(plugins_dir)!r}\n"
    )
    err_f.flush()

    subprocess.Popen(
        cmd,
        cwd=str(plugins_dir),
        env=env,
        stdout=err_f,
        stderr=err_f,
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
            line, _, _ = buf.partition(b"\n")
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
            "args": {"protocol_version": PROTOCOL_VERSION, "client_version": "3.0.0"},
        },
    )
    resp = _recv_json(sock)
    if not resp.get("ok"):
        raise RuntimeError(f"daemon hello failed: {resp.get('error')}")
    if int(resp.get("protocol_version") or 0) != PROTOCOL_VERSION:
        raise RuntimeError("protocol mismatch")
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
    if daemon_is_live():
        return
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
    last_err = "timeout waiting for daemon"
    while time.time() < deadline:
        time.sleep(0.25)
        try:
            if daemon_is_live():
                return
        except Exception as e:
            last_err = str(e)
    err_file = _spawn_log_path()
    extra = ""
    if err_file.exists():
        try:
            tail = _tail_text(err_file, 2000)
            if tail:
                extra = "\n" + tail
        except OSError:
            pass
    raise RuntimeError(f"spawn_failed: {last_err}. Check {err_file}.{extra}")


def request(op: str, **args: Any) -> dict[str, Any]:
    request_id = str(args.pop("request_id", None) or uuid.uuid4())
    payload = {"id": request_id, "op": op, "request_id": request_id, "args": args}

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
    except (ConnectionError, OSError, socket.error):
        for p in (_pid_path(), _sock_path()):
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        try:
            return _once()
        except Exception as e2:
            return {
                "ok": False,
                "message": f"spawn_failed / connection error: {e2}",
                "request_id": request_id,
                "error": str(e2),
                "error_code": "spawn_failed",
                "op": op,
            }
    except Exception as e:
        return {
            "ok": False,
            "message": f"client error: {e}",
            "request_id": request_id,
            "error": str(e),
            "error_code": "spawn_failed",
            "op": op,
        }


def format_user_message(result: dict[str, Any]) -> str:
    if not result:
        return "❌ AI Web: empty result"
    if result.get("ok"):
        return result.get("message") or "✅ OK"
    code = result.get("error_code") or ""
    if code == "internal" and not _verbose_errors():
        log = _spawn_log_path()
        msg = (
            "internal error — see daemon log "
            f"(`{log}` / stderr) for details. "
            "Set HERMES_AIWEB_VERBOSE_ERRORS=1 to show the exception."
        )
    else:
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