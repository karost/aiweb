"""AI Web V3 — session daemon (owns Playwright + service.handle).

Long-lived process. Browser is started once and kept warm.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional

_PLUGIN_ROOT = Path(__file__).resolve().parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT.parent))

from aiweb import memory_manager as mem
from aiweb.artifacts import debug_logs_root, failures_root
from aiweb.service import PROTOCOL_VERSION, handle
from aiweb.session import get_session

DAEMON_VERSION = "3.0.0"
SOCK_NAME = "daemon.sock"
PID_NAME = "daemon.pid"

_stop_flag = threading.Event()
_exit_after_stop = False


def _data() -> Path:
    return mem.data_dir()


def _sock_path() -> Path:
    return _data() / SOCK_NAME


def _pid_path() -> Path:
    return _data() / PID_NAME


def _write_pid() -> None:
    p = _pid_path()
    p.write_text(str(os.getpid()), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _remove_pid() -> None:
    for name in (PID_NAME, SOCK_NAME):
        path = _data() / name
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_singleton() -> None:
    _data().mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(_data(), 0o700)
    except OSError:
        pass
    pid_file = _pid_path()
    if pid_file.exists():
        try:
            old = int(pid_file.read_text(encoding="utf-8").strip())
        except ValueError:
            old = -1
        if _pid_alive(old) and old != os.getpid():
            raise SystemExit(f"AI Web daemon already running (pid={old}).")
        try:
            pid_file.unlink()
        except OSError:
            pass
        sock = _sock_path()
        if sock.exists():
            try:
                sock.unlink()
            except OSError:
                pass
    _write_pid()


def _send(conn: socket.socket, obj: dict[str, Any]) -> None:
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    conn.sendall(data)


def _recv_line(conn: socket.socket, buf: bytearray) -> Optional[dict[str, Any]]:
    while True:
        if b"\n" in buf:
            line, _, rest = buf.partition(b"\n")
            buf[:] = rest
            line = line.strip()
            if not line:
                continue
            return json.loads(line.decode("utf-8"))
        chunk = conn.recv(65536)
        if not chunk:
            return None
        buf.extend(chunk)


def _handle_connection(conn: socket.socket) -> None:
    global _exit_after_stop
    buf = bytearray()
    try:
        conn.settimeout(600)
        while not _stop_flag.is_set():
            try:
                req = _recv_line(conn, buf)
            except socket.timeout:
                continue
            if req is None:
                break
            if not isinstance(req, dict):
                _send(conn, {"ok": False, "error": "invalid request", "error_code": "invalid_args"})
                continue

            req_id = str(req.get("id") or "")
            op = str(req.get("op") or "").strip().lower()
            args = req.get("args") if isinstance(req.get("args"), dict) else {}
            request_id = str(req.get("request_id") or args.get("request_id") or req_id or "")

            if op == "hello":
                client_proto = int(args.get("protocol_version") or req.get("protocol_version") or 1)
                if client_proto != PROTOCOL_VERSION:
                    _send(conn, {
                        "id": req_id, "ok": False, "error": "protocol mismatch",
                        "error_code": "protocol_mismatch", "protocol_version": PROTOCOL_VERSION,
                    })
                    continue
                _send(conn, {
                    "id": req_id, "ok": True, "protocol_version": PROTOCOL_VERSION,
                    "daemon_version": DAEMON_VERSION, "message": "hello",
                })
                continue

            try:
                result = handle(op, request_id=request_id, **args)
            except Exception as e:
                result = {
                    "ok": False, "message": f"internal: {e}", "request_id": request_id,
                    "error": str(e), "error_code": "internal", "op": op,
                }
                traceback.print_exc()

            result = dict(result)
            result["id"] = req_id
            _send(conn, result)

            if op == "stop" and (args.get("daemon") or args.get("stop_daemon")):
                _exit_after_stop = True
                _stop_flag.set()
                break
    except Exception:
        traceback.print_exc()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main() -> None:
    acquire_singleton()
    sock_path = _sock_path()
    if sock_path.exists():
        try:
            sock_path.unlink()
        except OSError:
            pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(8)
    server.settimeout(1.0)

    def _sig(_signum, _frame):
        _stop_flag.set()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    print(f"[aiweb-daemon] v{DAEMON_VERSION} listening on {sock_path}", flush=True)

    try:
        while not _stop_flag.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            t = threading.Thread(target=_handle_connection, args=(conn,), daemon=True)
            t.start()
    finally:
        try:
            server.close()
        except Exception:
            pass
        _remove_pid()
        if _exit_after_stop:
            try:
                from aiweb.session import get_session
                from aiweb.browser_engine import run_async
                sess = get_session()
                if sess.engine is not None:
                    run_async(sess.engine.stop())
            except Exception:
                pass
        print("[aiweb-daemon] exited", flush=True)


if __name__ == "__main__":
    main()