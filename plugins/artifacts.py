"""AI Web V3 — failure artifacts (screenshots, HTML, text dumps) + debug logs."""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from . import memory_manager as mem


def failures_root() -> Path:
    d = mem.data_dir() / "failures"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def debug_logs_root() -> Path:
    d = mem.data_dir() / "debug_logs"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _safe_slug(s: str, max_len: int = 40) -> str:
    s = (s or "op").strip().lower()
    s = re.sub(r"[^a-z0-9._-]+", "_", s)
    return (s[:max_len] or "op").strip("_")


def new_failure_dir(*, op: str = "op", request_id: str = "") -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rid = _safe_slug(request_id or str(int(time.time())), 16)
    name = f"{ts}_{_safe_slug(op)}_{rid}"
    path = failures_root() / name
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def write_text_artifact(dir_path: Path, name: str, content: str) -> Path:
    p = dir_path / name
    p.write_text(content or "", encoding="utf-8")
    return p


def write_bytes_artifact(dir_path: Path, name: str, data: bytes) -> Path:
    p = dir_path / name
    p.write_bytes(data or b"")
    return p


async def capture_page_artifacts(
    page,
    *,
    op: str,
    request_id: str,
    note: str = "",
    extra_text: str = "",
) -> list[str]:
    """
    Capture HTML + full-page screenshot + metadata for a failure or diagnostic.
    Returns list of artifact paths (including the failure directory itself).
    Safe to call even if page is partially dead.
    """
    paths: list[str] = []
    out = new_failure_dir(op=op, request_id=request_id)

    if note:
        write_text_artifact(out, "note.txt", note)
    if extra_text:
        write_text_artifact(out, "debug_steps.txt", extra_text)

    try:
        html = await page.content()
        paths.append(str(write_text_artifact(out, "page.html", html)))
    except Exception as e:
        write_text_artifact(out, "html_error.txt", repr(e))

    try:
        png = await page.screenshot(full_page=True)
        paths.append(str(write_bytes_artifact(out, "screenshot.png", png)))
    except Exception as e:
        write_text_artifact(out, "screenshot_error.txt", repr(e))

    try:
        write_text_artifact(out, "url.txt", getattr(page, "url", "") or "")
    except Exception:
        pass

    write_text_artifact(
        out,
        "meta.txt",
        f"op={op}\nrequest_id={request_id}\nts={datetime.now(timezone.utc).isoformat()}\n",
    )
    paths.append(str(out))
    return paths


def capture_text_failure(
    *,
    op: str,
    request_id: str,
    note: str,
    body: str = "",
) -> list[str]:
    """Text-only failure (no live page). Useful when capture already failed."""
    out = new_failure_dir(op=op, request_id=request_id)
    paths = [str(write_text_artifact(out, "note.txt", note))]
    if body:
        excerpt = body if len(body) <= 200_000 else body[:200_000] + "\n<!-- truncated -->\n"
        paths.append(str(write_text_artifact(out, "body.txt", excerpt)))
    paths.append(str(out))
    return paths


def append_debug_log(request_id: str, line: str) -> Path:
    """Append one line to a per-request debug log under debug_logs/."""
    p = debug_logs_root() / f"{_safe_slug(request_id, 64)}.log"
    with p.open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")
    return p


def format_artifacts_suffix(artifacts: Sequence[str]) -> str:
    if not artifacts:
        return ""
    joined = ", ".join(artifacts[:5])
    extra = f" (+{len(artifacts) - 5} more)" if len(artifacts) > 5 else ""
    return f"\nArtifacts: {joined}{extra}"


__all__ = [
    "failures_root",
    "debug_logs_root",
    "new_failure_dir",
    "write_text_artifact",
    "write_bytes_artifact",
    "capture_page_artifacts",
    "capture_text_failure",
    "append_debug_log",
    "format_artifacts_suffix",
]