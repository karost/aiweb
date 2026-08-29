"""AI Web — memory and inject-buffer files (cross-process safe).

Daemon writes; Hermes agent process reads/pops via pre_llm_call.
Uses file locks where available (fcntl on Unix).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional

try:
    import fcntl
except ImportError:  # Windows — best-effort without fcntl
    fcntl = None  # type: ignore


def _hermes_home() -> Path:
    raw = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return Path(raw).expanduser().resolve()


def data_dir() -> Path:
    d = _hermes_home() / "data" / "aiweb"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(*parts: str) -> Path:
    return data_dir().joinpath(*parts)


# --- file names ---
LAST_RESPONSE = "last_response.md"
MODEL_CONTEXT = "model_context.md"
INJECT_PENDING = "inject_pending.flag"
MODEL_STICKY = "model_sticky.flag"
HOT_CONTEXT = "hot_context.md"
ARCHIVE = "archive.md"
MORE_STATE = "more_state.json"
STATE_JSON = "state.json"


class _FileLock:
    """Simple exclusive lock file companion."""

    def __init__(self, target: Path, timeout: float = 5.0):
        self.target = target
        self.lock_path = target.with_suffix(target.suffix + ".lock")
        self.timeout = timeout
        self._fh = None

    def __enter__(self) -> "_FileLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.lock_path, "a+", encoding="utf-8")
        if fcntl is None:
            return self
        deadline = time.time() + self.timeout
        while True:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.time() >= deadline:
                    raise TimeoutError(f"lock timeout: {self.lock_path}")
                time.sleep(0.05)

    def __exit__(self, *args: Any) -> None:
        if self._fh is not None:
            if fcntl is not None:
                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            self._fh.close()
            self._fh = None


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _FileLock(path):
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)


def read_text(path: Path, default: str = "") -> str:
    if not path.exists():
        return default
    with _FileLock(path):
        return path.read_text(encoding="utf-8")


def write_last_response(text: str) -> Path:
    p = _path(LAST_RESPONSE)
    write_text(p, text)
    return p


def read_last_response() -> str:
    return read_text(_path(LAST_RESPONSE))


def write_model_context(payload: str) -> Path:
    """Write inject buffer and set pending flag."""
    p = _path(MODEL_CONTEXT)
    write_text(p, payload)
    flag = _path(INJECT_PENDING)
    flag.write_text("1", encoding="utf-8")
    return p


def clear_model_context() -> None:
    for name in (MODEL_CONTEXT, INJECT_PENDING):
        p = _path(name)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass


def inject_pending() -> bool:
    return _path(INJECT_PENDING).exists()


def is_sticky() -> bool:
    return _path(MODEL_STICKY).exists()


def set_sticky(enabled: bool) -> None:
    p = _path(MODEL_STICKY)
    if enabled:
        p.write_text("1", encoding="utf-8")
    elif p.exists():
        p.unlink()


def pop_model_context() -> Optional[str]:
    """
    Read inject payload for pre_llm_call.

    One-shot default: clear after read unless sticky is set.
    Returns None if nothing pending / empty.
    """
    if not inject_pending() and not _path(MODEL_CONTEXT).exists():
        return None

    payload = read_text(_path(MODEL_CONTEXT), default="").strip()
    if not payload:
        clear_model_context()
        return None

    if not is_sticky():
        clear_model_context()
    # sticky: leave file + pending for re-inject; caller may still clear later
    return payload


def write_path_only_inject(file_path: str) -> Path:
    """Optional write-inject mode: path pointer only, no file body."""
    line = f"[File written: {file_path}]\n(Content not injected; open the file if needed.)\n"
    return write_model_context(line)


def append_hot_summary(line: str, max_chars: int = 4000) -> None:
    """Keep a short rolling summary; never store full large Grok bodies."""
    p = _path(HOT_CONTEXT)
    line = (line or "").strip()
    if not line:
        return
    prev = read_text(p, default="")
    merged = (prev + "\n" + line).strip()
    if len(merged) > max_chars:
        merged = merged[-max_chars:]
    write_text(p, merged)


def archive_line(line: str) -> None:
    p = _path(ARCHIVE)
    prev = read_text(p, default="")
    write_text(p, (prev + "\n" + line).strip() + "\n")


def more_state_path() -> Path:
    return _path(MORE_STATE)


def model_context_path() -> Path:
    return _path(MODEL_CONTEXT)


def last_response_path() -> Path:
    return _path(LAST_RESPONSE)


def inject_info_snapshot(
    *,
    written: bool,
    pending: bool,
    chars: int,
    mode: str,
    distill_method: Optional[str] = None,
    capped: bool = False,
    cap: Optional[int] = None,
) -> dict:
    return {
        "written": written,
        "pending": pending,
        "chars": chars,
        "mode": mode,
        "distill_method": distill_method,
        "capped": capped,
        "cap": cap,
    }


__all__ = [
    "data_dir",
    "write_last_response",
    "read_last_response",
    "write_model_context",
    "clear_model_context",
    "inject_pending",
    "is_sticky",
    "set_sticky",
    "pop_model_context",
    "write_path_only_inject",
    "append_hot_summary",
    "archive_line",
    "more_state_path",
    "model_context_path",
    "last_response_path",
    "inject_info_snapshot",
]