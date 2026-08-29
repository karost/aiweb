"""AI Web V3 — safe write path resolution and atomic file writes.

Jail: all outputs under OUT_DIR (realpath). Reject .., absolute escapes,
and sensitive filenames.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import memory_manager as mem


# Basename patterns that must never be written via /aiweb-write
_SENSITIVE_NAMES = re.compile(
    r"(?i)^("
    r"\.env.*|"
    r".*\.pem|"
    r".*\.key|"
    r".*\.p12|"
    r".*\.pfx|"
    r"id_rsa.*|"
    r"id_ed25519.*|"
    r"credentials\.json|"
    r"service.?account.*\.json|"
    r"secrets?\..*|"
    r"\.git.*|"
    r"\.ssh.*"
    r")$"
)

_FENCE_RE = re.compile(
    r"```([a-zA-Z0-9_+-]*)\s*\n(.*?)```",
    re.DOTALL,
)


@dataclass
class WriteResolveOk:
    target: Path
    root: Path


@dataclass
class WriteResolveErr:
    error_code: str  # write_rejected
    error: str


def default_out_dir() -> Path:
    env = (os.environ.get("HERMES_AIWEB_OUT_DIR") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return (mem.data_dir() / "out").resolve()


def ensure_out_dir(root: Optional[Path] = None) -> Path:
    root = (root or default_out_dir()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def is_sensitive_name(name: str) -> bool:
    base = Path(name).name
    return bool(_SENSITIVE_NAMES.match(base))


def resolve_out_path(
    user_path: str,
    *,
    out_dir: Optional[Path] = None,
) -> WriteResolveOk | WriteResolveErr:
    """
    Resolve user_path strictly under out_dir.
    Relative paths only (or absolute only if still under root after resolve).
    """
    root = ensure_out_dir(out_dir)
    raw = (user_path or "").strip()
    if not raw:
        return WriteResolveErr("write_rejected", "empty path")

    if is_sensitive_name(raw):
        return WriteResolveErr(
            "write_rejected",
            f"sensitive name rejected: {Path(raw).name}",
        )

    p = Path(raw)
    if any(part == ".." for part in p.parts):
        return WriteResolveErr("write_rejected", "path traversal (..) not allowed")

    if p.is_absolute():
        candidate = p.resolve()
    else:
        candidate = (root / p).resolve()

    try:
        candidate.relative_to(root)
    except ValueError:
        return WriteResolveErr(
            "write_rejected",
            f"path escapes out dir ({root})",
        )

    if is_sensitive_name(candidate.name):
        return WriteResolveErr(
            "write_rejected",
            f"sensitive name rejected: {candidate.name}",
        )

    return WriteResolveOk(target=candidate, root=root)


def extract_primary_code(
    text: str,
    *,
    language: Optional[str] = None,
) -> tuple[Optional[str], str]:
    """
    Extract primary fenced code body.
    Returns (body_or_None, reason_if_none).
    Fail-closed: no fence → refuse to dump whole prose as "code".
    """
    raw = (text or "").strip()
    if not raw:
        return None, "empty capture"

    blocks: list[tuple[str, str]] = []
    for m in _FENCE_RE.finditer(raw):
        lang = (m.group(1) or "").strip().lower()
        body = (m.group(2) or "").strip("\n")
        if body.strip():
            blocks.append((lang, body))

    if not blocks:
        return None, "no fenced code block found"

    lang_pref = (language or "").strip().lower()
    if not lang_pref:
        body = max(blocks, key=lambda lb: len(lb[1]))[1]
        return body, "ok"

    preferred = [b for b in blocks if b[0] == lang_pref]
    if preferred:
        body = max(preferred, key=lambda lb: len(lb[1]))[1]
        return body, "ok"

    # Fallback: largest any fence
    body = max(blocks, key=lambda lb: len(lb[1]))[1]
    return body, "ok"


def language_from_path(path: str | Path) -> Optional[str]:
    ext = Path(path).suffix.lower().lstrip(".")
    mapping = {
        "py": "python",
        "pyw": "python",
        "ts": "typescript",
        "tsx": "tsx",
        "js": "javascript",
        "jsx": "jsx",
        "java": "java",
        "go": "go",
        "rs": "rust",
        "rb": "ruby",
        "php": "php",
        "c": "c",
        "h": "c",
        "cpp": "cpp",
        "cc": "cpp",
        "hpp": "cpp",
        "cs": "csharp",
        "kt": "kotlin",
        "swift": "swift",
        "sh": "bash",
        "bash": "bash",
        "zsh": "bash",
        "sql": "sql",
        "md": "markdown",
        "json": "json",
        "yaml": "yaml",
        "yml": "yaml",
        "html": "html",
        "css": "css",
    }
    return mapping.get(ext)


def atomic_write(target: Path, content: str) -> int:
    """Write via temp file in same directory then replace. Returns byte length."""
    target.parent.mkdir(parents=True, exist_ok=True)
    data = content if content.endswith("\n") else content + "\n"
    tmp = target.with_suffix(target.suffix + ".aiweb_tmp")
    try:
        tmp.write_text(data, encoding="utf-8")
        tmp.replace(target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return len(data.encode("utf-8"))


def looks_like_chrome(text: str) -> bool:
    """Heuristic: reject extracts that are mostly UI chrome."""
    t = (text or "").strip().lower()
    if len(t) < 8:
        return True
    chrome_hits = sum(
        1
        for k in (
            "sign in",
            "log in",
            "cookie",
            "accept all",
            "nav ",
            "skip to content",
            "continue with x",
            "continue with twitter",
        )
        if k in t
    )
    if chrome_hits >= 2 and len(t) < 400:
        return True
    return False


def write_inject_mode() -> str:
    """
    HERMES_AIWEB_WRITE_INJECT:
      none      → no model inject after write (default)
      path_only → inject only the written path
    """
    v = (os.environ.get("HERMES_AIWEB_WRITE_INJECT") or "none").strip().lower()
    if v in {"path_only", "path", "file"}:
        return "path_only"
    return "none"


__all__ = [
    "WriteResolveOk",
    "WriteResolveErr",
    "default_out_dir",
    "ensure_out_dir",
    "resolve_out_path",
    "extract_primary_code",
    "language_from_path",
    "atomic_write",
    "looks_like_chrome",
    "is_sensitive_name",
    "write_inject_mode",
]