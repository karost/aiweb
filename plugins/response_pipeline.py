"""AI Web V3 — response pipeline: size threshold, fence-aware chunking, model pack.

Small path (len <= DEFAULT_RESPONSE_SIZE): memory only, no file, no chunks.
Large path: last_response.md + chunks; Hermes shows one part; /aiweb-more continues.
Model inject always uses one packed string (not N chat fragments).

Logging: local _plog() → same dir as other aiweb logs.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import memory_manager as mem

DATA_DIR = mem.data_dir()
LAST_RESPONSE_FILE = DATA_DIR / "last_response.md"
PIPELINE_STATE_FILE = DATA_DIR / "response_pipeline_state.json"
_TRACE_DIR = DATA_DIR / "debug_logs"

DEFAULT_RESPONSE_SIZE = int(os.environ.get("HERMES_AIWEB_RESPONSE_DEFAULT_SIZE", "2500"))
CHAT_SOFT_MAX = int(os.environ.get("HERMES_AIWEB_CHAT_CHUNK", "2500"))
CHAT_HARD_MAX = int(os.environ.get("HERMES_AIWEB_CHAT_HARD", "12000"))
MODEL_PACK_MAX = int(os.environ.get("HERMES_AIWEB_MODEL_PACK", "6000"))

_FENCE_OPEN = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")

_PLOG_FILE: Path | None = None


def _plog(msg: str) -> None:
    """Fact log for pipeline (does not import commands)."""
    global _PLOG_FILE
    _TRACE_DIR.mkdir(parents=True, exist_ok=True)
    if _PLOG_FILE is None:
        _PLOG_FILE = _TRACE_DIR / f"pipeline_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    line = f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}"
    try:
        with _PLOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(f"[aiweb-pipeline] {msg}", file=sys.stderr, flush=True)


def default_response_size() -> int:
    return DEFAULT_RESPONSE_SIZE


@dataclass
class PipelineResult:
    full_text: str
    path: str  # "small" | "large"
    chars: int
    chunks: list[str] = field(default_factory=list)
    chunk_index: int = 0
    file_path: Optional[str] = None
    model_pack: str = ""
    model_truncated: bool = False
    forced_large_reason: str = ""

    @property
    def total_chunks(self) -> int:
        return max(1, len(self.chunks)) if self.chunks else 1


def _strip_thought_prefix(text: str) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    if lines and lines[0].lower().startswith("thought for"):
        return "\n".join(lines[1:]).strip() or text.strip()
    return text.strip()


def _parse_blocks(text: str) -> list[str]:
    """Split into atomic blocks; fenced code is one block (CommonMark-ish)."""
    lines = (text or "").splitlines(keepends=True)
    blocks: list[str] = []
    i = 0
    n = len(lines)
    buf: list[str] = []

    def flush_buf() -> None:
        nonlocal buf
        if buf:
            blocks.append("".join(buf).rstrip("\n"))
            buf = []

    while i < n:
        line = lines[i]
        m = _FENCE_OPEN.match(line.rstrip("\n"))
        if m:
            flush_buf()
            fence_char = m.group(2)[0]
            fence_len = len(m.group(2))
            code_lines = [line]
            i += 1
            while i < n:
                code_lines.append(lines[i])
                raw = lines[i].rstrip("\n")
                m2 = _FENCE_OPEN.match(raw)
                if (
                    m2
                    and m2.group(2)[0] == fence_char
                    and len(m2.group(2)) >= fence_len
                    and not (m2.group(3) or "").strip()
                ):
                    i += 1
                    break
                i += 1
            blocks.append("".join(code_lines).rstrip("\n"))
            continue
        buf.append(line)
        i += 1
    flush_buf()
    return [b for b in blocks if b.strip()]


def _is_fence_block(block: str) -> bool:
    first = block.lstrip().split("\n", 1)[0] if block else ""
    return bool(_FENCE_OPEN.match(first))


def _max_fence_len(text: str) -> int:
    blocks = _parse_blocks(text)
    return max((len(b) for b in blocks if _is_fence_block(b)), default=0)


def _audit_fences(chunk: str) -> bool:
    n = 0
    for ln in chunk.splitlines():
        s = ln.strip()
        if s.startswith("```") or s.startswith("~~~"):
            n += 1
    return n % 2 == 0


def chunk_for_chat(
    text: str, soft_max: int = CHAT_SOFT_MAX, hard_max: int = CHAT_HARD_MAX
) -> list[str]:
    """Fence-aware pack: never split mid-fence; oversized fence = its own chunk."""
    text = _strip_thought_prefix(text)
    if not text:
        return []
    blocks = _parse_blocks(text)
    if not blocks:
        return [text] if text else []

    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0

    def flush() -> None:
        nonlocal cur, cur_len
        if cur:
            piece = "\n\n".join(cur).strip()
            if piece:
                chunks.append(piece)
            cur, cur_len = [], 0

    for block in blocks:
        blen = len(block)
        if _is_fence_block(block):
            if cur and cur_len + blen + 2 > soft_max:
                flush()
            if blen > soft_max:
                flush()
                if blen <= hard_max:
                    chunks.append(block)
                else:
                    chunks.append(
                        block[:hard_max] + "\n\n…[code truncated for chat hard max]"
                    )
                    _plog(f"code block truncated blen={blen} hard_max={hard_max}")
                continue
            cur.append(block)
            cur_len += blen + 2
            continue

        if cur_len + blen + 2 > soft_max and cur:
            flush()
        if blen > soft_max and not cur:
            parts = re.split(r"\n\n+", block)
            for p in parts:
                p = p.strip()
                if not p:
                    continue
                if cur_len + len(p) + 2 > soft_max and cur:
                    flush()
                cur.append(p)
                cur_len += len(p) + 2
            continue
        cur.append(block)
        cur_len += blen + 2

    flush()

    # Fix unbalanced fences by merging adjacent chunks when needed
    fixed: list[str] = []
    i = 0
    while i < len(chunks):
        c = chunks[i]
        if not _audit_fences(c) and i + 1 < len(chunks):
            c = c + "\n\n" + chunks[i + 1]
            i += 2
            fixed.append(c)
            _plog("merged two chunks to fix unbalanced fence")
        else:
            fixed.append(c)
            i += 1
    return fixed or ([text] if text else [])


def pack_for_model(text: str, max_chars: int = MODEL_PACK_MAX) -> tuple[str, bool]:
    """Single pack for model context. Returns (pack, truncated)."""
    text = _strip_thought_prefix(text or "")
    if len(text) <= max_chars:
        return text, False
    head = max_chars * 2 // 3
    tail = max_chars - head - 80
    if tail < 200:
        _plog(f"model pack hard-truncate chars={len(text)} max={max_chars}")
        return text[:max_chars] + "\n\n…[truncated for model context]", True
    pack = (
        text[:head]
        + "\n\n…[middle omitted for model context budget; full text in last_response.md]…\n\n"
        + text[-tail:]
    )
    _plog(f"model pack head+tail chars={len(text)} → {len(pack)}")
    return pack, True


def _save_state(result: PipelineResult) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    state = {
        "path": result.path,
        "chars": result.chars,
        "chunk_index": result.chunk_index,
        "total_chunks": result.total_chunks,
        "file_path": result.file_path,
        "updated": datetime.now().isoformat(timespec="seconds"),
        "chunks": result.chunks,
    }
    PIPELINE_STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _plog(
        f"state saved path={result.path} chunks={result.total_chunks} "
        f"index={result.chunk_index} file={PIPELINE_STATE_FILE}"
    )


def load_pipeline_state() -> Optional[dict]:
    try:
        if PIPELINE_STATE_FILE.exists():
            return json.loads(PIPELINE_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        _plog(f"load_pipeline_state fail: {e}")
    return None


def process_response(full_text: str, *, inject_model: bool = False) -> PipelineResult:
    """Main entry after capture. Decides small vs large; prepares chat/model outputs."""
    text = _strip_thought_prefix(full_text or "")
    chars = len(text)
    fence_max = _max_fence_len(text)
    force_large = fence_max > DEFAULT_RESPONSE_SIZE
    is_large = chars > DEFAULT_RESPONSE_SIZE or force_large

    _plog(
        f"process_response chars={chars} default={DEFAULT_RESPONSE_SIZE} "
        f"fence_max={fence_max} → {'large' if is_large else 'small'} "
        f"inject_model={inject_model}"
    )

    result = PipelineResult(
        full_text=text,
        path="large" if is_large else "small",
        chars=chars,
        forced_large_reason=(
            "fence>default" if force_large and chars <= DEFAULT_RESPONSE_SIZE else ""
        ),
    )

    if not is_large:
        result.chunks = [text] if text else []
        result.chunk_index = 1
        if inject_model:
            pack, trunc = pack_for_model(text)
            result.model_pack = pack
            result.model_truncated = trunc
        _save_state(result)
        return result

    # Large path
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LAST_RESPONSE_FILE.write_text(text + "\n", encoding="utf-8")
    result.file_path = str(LAST_RESPONSE_FILE)
    result.chunks = chunk_for_chat(text)
    result.chunk_index = 0
    _plog(
        f"large path file={LAST_RESPONSE_FILE} "
        f"chunks={len(result.chunks)} sizes={[len(c) for c in result.chunks]}"
    )
    if inject_model:
        pack, trunc = pack_for_model(text)
        result.model_pack = pack
        result.model_truncated = trunc
    _save_state(result)
    return result


def format_chat_piece(result: PipelineResult, chunk_i: int) -> str:
    """Format one Hermes-visible piece."""
    if not result.chunks:
        body = result.full_text or "(empty)"
        return body
    i = max(0, min(chunk_i, len(result.chunks) - 1))
    body = result.chunks[i]
    if result.path == "small" or len(result.chunks) <= 1:
        return body
    total = len(result.chunks)
    footer = f"\n\n_Part {i + 1}/{total}"
    if i + 1 < total:
        footer += " · next: `/aiweb-more`"
    if result.file_path:
        footer += f" · full: `{result.file_path}`"
    footer += "_"
    return body + footer


def next_more() -> str:
    """Return next chunk text for /aiweb-more or a status message."""
    st = load_pipeline_state()
    if not st or not st.get("chunks"):
        _plog("next_more: no pending chunks")
        return (
            "No multi-part Grok answer pending.\n"
            "Run `/aiweb-chat` or `/aiweb` with a long prompt first."
        )
    chunks = st["chunks"]
    idx = int(st.get("chunk_index", 0))
    if idx >= len(chunks):
        _plog(f"next_more: already done idx={idx} total={len(chunks)}")
        return (
            f"All **{len(chunks)}** parts already shown.\n"
            f"Full text: `{st.get('file_path') or LAST_RESPONSE_FILE}`"
        )
    body = chunks[idx]
    total = len(chunks)
    st["chunk_index"] = idx + 1
    try:
        PIPELINE_STATE_FILE.write_text(
            json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        _plog(f"next_more state save fail: {e}")
    _plog(f"next_more showed {idx + 1}/{total} len={len(body)}")
    footer = f"\n\n_Part {idx + 1}/{total}"
    if idx + 1 < total:
        footer += " · next: `/aiweb-more`"
    else:
        footer += " · done"
    if st.get("file_path"):
        footer += f" · full: `{st['file_path']}`"
    footer += "_"
    return body + footer


__all__ = [
    "PipelineResult",
    "process_response",
    "format_chat_piece",
    "next_more",
    "default_response_size",
    "load_pipeline_state",
    "LAST_RESPONSE_FILE",
    "PIPELINE_STATE_FILE",
    "DEFAULT_RESPONSE_SIZE",
    "MODEL_PACK_MAX",
]