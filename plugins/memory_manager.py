"""AI Web V3 — memory, inject buffer, paths, hot context.

Final-only inject contract:
  /aiweb writes buffer + inject_pending flag
  slash does NOT consume the buffer
  pre_llm_call pops on the *next* agent turn (one-shot unless sticky)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()


def data_dir() -> Path:
    d = _hermes_home() / "data" / "aiweb"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


HOT_FILE = data_dir() / "hot_context.md"
ARCHIVE_FILE = data_dir() / "archive.md"
MODEL_CTX_FILE = data_dir() / "model_context.md"
INJECT_FLAG = data_dir() / "inject_pending.flag"
STICKY_FLAG = data_dir() / "model_sticky.flag"
LAST_RESPONSE_FILE = data_dir() / "last_response.md"
PIPELINE_STATE_FILE = data_dir() / "response_pipeline_state.json"
STATE_FILE = data_dir() / "state.json"


def ensure_files() -> None:
    data_dir()
    for p in (HOT_FILE, ARCHIVE_FILE):
        if not p.exists():
            p.write_text("", encoding="utf-8")


def get_out_dir() -> Path:
    env = (os.environ.get("HERMES_AIWEB_OUT_DIR") or "").strip()
    if env:
        d = Path(env).expanduser().resolve()
    else:
        d = (data_dir() / "out").resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Hot / archive memory
# ---------------------------------------------------------------------------

def load_hot_context(max_chars: int = 12000) -> str:
    ensure_files()
    try:
        text = HOT_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if len(text) > max_chars:
        text = text[-max_chars:]
        text = "…[earlier hot context truncated]…\n\n" + text
    return text or "(empty hot context)"


def append_hot_summary(line: str) -> None:
    ensure_files()
    ts = time.strftime("%Y-%m-%d %H:%M")
    entry = f"- [{ts}] {line.strip()}\n"
    try:
        with HOT_FILE.open("a", encoding="utf-8") as f:
            f.write(entry)
        # Soft rotate if very large
        if HOT_FILE.stat().st_size > 80_000:
            content = HOT_FILE.read_text(encoding="utf-8")
            keep = content[-40_000:]
            HOT_FILE.write_text("…[rotated]\n" + keep, encoding="utf-8")
            with ARCHIVE_FILE.open("a", encoding="utf-8") as af:
                af.write(f"\n--- rotated {ts} ---\n{content[:40_000]}\n")
    except OSError:
        pass


def update_current_state(msg: str) -> None:
    """Lightweight status line kept at top of hot context."""
    ensure_files()
    try:
        body = HOT_FILE.read_text(encoding="utf-8")
    except OSError:
        body = ""
    # Remove previous state marker
    lines = [ln for ln in body.splitlines() if not ln.startswith("<!-- state:")]
    lines.insert(0, f"<!-- state: {msg[:120]} -->")
    HOT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def clear_hot_and_archive() -> None:
    ensure_files()
    for p in (HOT_FILE, ARCHIVE_FILE):
        try:
            p.write_text("", encoding="utf-8")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Model context inject (final-only)
# ---------------------------------------------------------------------------

def model_pack_cap() -> int:
    try:
        return max(500, int(os.environ.get("HERMES_AIWEB_MODEL_PACK", "6000")))
    except ValueError:
        return 6000


def is_model_sticky() -> bool:
    return STICKY_FLAG.exists()


def set_sticky(on: bool) -> None:
    if on:
        STICKY_FLAG.write_text("1", encoding="utf-8")
    else:
        if STICKY_FLAG.exists():
            try:
                STICKY_FLAG.unlink()
            except OSError:
                pass


def inject_pending() -> bool:
    return INJECT_FLAG.exists() and MODEL_CTX_FILE.exists()


def write_model_context(payload: str) -> None:
    """Write final-only pack. Caps to MODEL_PACK_CAP."""
    text = (payload or "").strip()
    cap = model_pack_cap()
    if len(text) > cap:
        head = cap * 2 // 3
        tail = cap - head - 60
        text = (
            text[:head]
            + "\n\n…[middle omitted for model context budget]…\n\n"
            + text[-tail:]
        )
    MODEL_CTX_FILE.write_text(text + "\n", encoding="utf-8")
    INJECT_FLAG.write_text("1", encoding="utf-8")


def write_path_only_inject(path: str) -> None:
    write_model_context(f"[AI Web wrote file: {path}]")


def clear_model_context() -> None:
    for p in (MODEL_CTX_FILE, INJECT_FLAG):
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
    set_sticky(False)


def pop_model_context(max_chars: Optional[int] = None) -> str:
    """
    Called by pre_llm_call.
    One-shot unless sticky flag is set.
    """
    if not inject_pending():
        return ""
    try:
        text = MODEL_CTX_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if not text:
        clear_model_context()
        return ""

    if max_chars is None:
        max_chars = model_pack_cap()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…[truncated]"

    if not is_model_sticky():
        # one-shot: remove flag + content
        clear_model_context()
    else:
        # sticky: keep content, clear only the "pending this turn" flag
        # so next turn still injects until explicit clear
        if INJECT_FLAG.exists():
            try:
                INJECT_FLAG.unlink()
            except OSError:
                pass
        # re-arm for next turn
        INJECT_FLAG.write_text("1", encoding="utf-8")

    return text


def inject_info_snapshot(
    *,
    written: bool,
    pending: bool,
    chars: int,
    mode: str = "final_only",
    distill_method: str = "",
    capped: bool = False,
    cap: Optional[int] = None,
) -> dict[str, Any]:
    return {
        "written": written,
        "pending": pending,
        "chars": chars,
        "mode": mode,
        "distill_method": distill_method,
        "capped": capped,
        "cap": cap if cap is not None else model_pack_cap(),
        "sticky": is_model_sticky(),
    }


def _empty_inject() -> dict[str, Any]:
    return inject_info_snapshot(
        written=False,
        pending=False,
        chars=0,
        mode="none",
        distill_method="",
        capped=False,
    )


# ---------------------------------------------------------------------------
# Helpers used by service / write
# ---------------------------------------------------------------------------

def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return default


def resolve_safe_write_path(filename: str) -> Path:
    """Deprecated alias — prefer write_policy.resolve_out_path."""
    from .write_policy import resolve_out_path, WriteResolveErr
    r = resolve_out_path(filename)
    if isinstance(r, WriteResolveErr):
        raise ValueError(r.error)
    return r.target


__all__ = [
    "data_dir",
    "HOT_FILE",
    "ARCHIVE_FILE",
    "MODEL_CTX_FILE",
    "LAST_RESPONSE_FILE",
    "PIPELINE_STATE_FILE",
    "ensure_files",
    "get_out_dir",
    "load_hot_context",
    "append_hot_summary",
    "update_current_state",
    "clear_hot_and_archive",
    "model_pack_cap",
    "is_model_sticky",
    "set_sticky",
    "inject_pending",
    "write_model_context",
    "write_path_only_inject",
    "clear_model_context",
    "pop_model_context",
    "inject_info_snapshot",
    "read_text",
    "resolve_safe_write_path",
]