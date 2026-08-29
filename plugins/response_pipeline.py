"""AI Web — small/large response pipeline, last_response.md, /aiweb-more state.

Rules (architecture v2):
  n <= DEFAULT → small (no chunk required); still write last_response.md
  n >  DEFAULT → large: full md required, chat = chunk 0, more_available
  more_state carries gen_id so /aiweb-more cannot page a newer capture
"""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import memory_manager as mem


def response_default_size() -> int:
    try:
        return max(256, int(os.environ.get("HERMES_AIWEB_RESPONSE_DEFAULT_SIZE", "2000")))
    except ValueError:
        return 2000


def max_capture_chars() -> int:
    try:
        return max(10_000, int(os.environ.get("HERMES_AIWEB_MAX_CAPTURE_CHARS", "500000")))
    except ValueError:
        return 500_000


def new_gen_id() -> str:
    return secrets.token_hex(4)


@dataclass
class PipelineResult:
    path: str  # "small" | "large"
    chars: int
    chat_piece: str
    full_path: str
    gen_id: str
    more_available: bool
    chunk_index: int
    chunk_total: int
    truncated_capture: bool = False
    text: str = ""  # full text used (after max-capture clamp)


def _clamp_capture(text: str) -> tuple[str, bool]:
    limit = max_capture_chars()
    if len(text) <= limit:
        return text, False
    notice = (
        f"\n\n\n<!-- aiweb: truncated to {limit} chars of {len(text)} -->\n"
    )
    return text[:limit] + notice, True


def process_response(text: str, *, default_size: Optional[int] = None) -> PipelineResult:
    """
    Persist full text, decide small/large, prepare first chat piece + more_state.
    """
    raw = text or ""
    raw, truncated = _clamp_capture(raw)
    d = default_size if default_size is not None else response_default_size()
    gen_id = new_gen_id()

    full_path = mem.write_last_response(raw)
    n = len(raw)

    if n <= d:
        _clear_more_state()
        return PipelineResult(
            path="small",
            chars=n,
            chat_piece=raw,
            full_path=str(full_path),
            gen_id=gen_id,
            more_available=False,
            chunk_index=0,
            chunk_total=1,
            truncated_capture=truncated,
            text=raw,
        )

    chunks = chunk_for_chat(raw, size=d)
    total = len(chunks)
    _write_more_state(
        {
            "gen_id": gen_id,
            "index": 0,
            "chunk_size": d,
            "total_chars": n,
            "chunk_total": total,
        }
    )
    return PipelineResult(
        path="large",
        chars=n,
        chat_piece=chunks[0] if chunks else "",
        full_path=str(full_path),
        gen_id=gen_id,
        more_available=total > 1,
        chunk_index=0,
        chunk_total=total,
        truncated_capture=truncated,
        text=raw,
    )


def chunk_for_chat(text: str, *, size: int) -> list[str]:
    """Fence-aware-ish chunking: prefer split on blank lines / fence boundaries."""
    if size < 64:
        size = 64
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= size:
            chunks.append(rest)
            break
        window = rest[:size]
        # Prefer breaking at a closed fence or double newline near the end
        split_at = _best_split(window)
        if split_at < size // 4:
            split_at = size
        piece = rest[:split_at]
        chunks.append(piece)
        rest = rest[split_at:]
        # Avoid infinite loop on pathological input
        if not piece:
            chunks.append(rest[:size])
            rest = rest[size:]
    return chunks or [text]


def _best_split(window: str) -> int:
    """Return index in window to split after; prefer end of fence or paragraph."""
    # Last closing fence
    idx_fence = window.rfind("```")
    if idx_fence > len(window) // 3:
        # if odd number of ``` before idx, include closing
        count = window.count("```")
        if count % 2 == 0:
            return idx_fence + 3

    idx_para = window.rfind("\n\n")
    if idx_para > len(window) // 3:
        return idx_para + 2

    idx_nl = window.rfind("\n")
    if idx_nl > len(window) // 2:
        return idx_nl + 1

    return len(window)


def _write_more_state(state: dict[str, Any]) -> None:
    path = mem.more_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _clear_more_state() -> None:
    path = mem.more_state_path()
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass


def read_more_state() -> Optional[dict[str, Any]]:
    path = mem.more_state_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def next_more() -> tuple[bool, str, dict[str, Any]]:
    """
    Advance chunk for current gen_id.
    Returns (ok, message, meta) where meta has gen_id, chunk_index, more_available, etc.
    """
    state = read_more_state()
    if not state:
        return False, "No large response to continue. Run /aiweb or /aiweb-chat first.", {
            "more_available": False,
        }

    gen_id = state.get("gen_id")
    index = int(state.get("index", 0))
    chunk_size = int(state.get("chunk_size", response_default_size()))
    full = mem.read_last_response()
    if not full:
        return False, "last_response.md missing or empty.", {"more_available": False}

    chunks = chunk_for_chat(full, size=chunk_size)
    next_index = index + 1
    if next_index >= len(chunks):
        return False, "No more chunks.", {
            "gen_id": gen_id,
            "more_available": False,
            "chunk_index": index,
            "chunk_total": len(chunks),
        }

    piece = chunks[next_index]
    state["index"] = next_index
    state["chunk_total"] = len(chunks)
    _write_more_state(state)
    more = next_index + 1 < len(chunks)
    msg = piece
    if more:
        msg += f"\n\n_(large {next_index + 1}/{len(chunks)} · /aiweb-more)_"
    else:
        msg += f"\n\n_(large {next_index + 1}/{len(chunks)} · end)_"

    return True, msg, {
        "gen_id": gen_id,
        "more_available": more,
        "chunk_index": next_index,
        "chunk_total": len(chunks),
        "path": "large",
        "chars": len(full),
        "full_path": str(mem.last_response_path()),
    }


def format_chat_meta(
    *,
    path: str,
    chars: int,
    full_path: str,
    more_available: bool,
    chunk_index: int = 0,
    chunk_total: int = 1,
    inject_note: str = "",
) -> str:
    parts = [f"{path}"]
    if path == "large":
        parts.append(f"{chunk_index + 1}/{chunk_total}")
        parts.append(f"{chars} chars")
        parts.append(f"full: {full_path}")
        if more_available:
            parts.append("/aiweb-more")
    else:
        parts.append(f"{chars} chars")
    if inject_note:
        parts.append(inject_note)
    return " · ".join(parts)


__all__ = [
    "PipelineResult",
    "process_response",
    "chunk_for_chat",
    "next_more",
    "read_more_state",
    "response_default_size",
    "max_capture_chars",
    "new_gen_id",
    "format_chat_meta",
]