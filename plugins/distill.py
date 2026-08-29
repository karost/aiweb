"""AI Web — final-only distill for Hermes model inject.

Architecture v2 rules (ordered):
  1. heading_final — content after last Final/Solution/Answer/Summary/Conclusion
  2. fence_largest — largest fenced block (prefer matching language) when code hint
  3. tail_strip    — strip chrome, take tail, then cap
  4. always apply MODEL_PACK_CAP
  5. empty → method=empty, payload=""
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional

DistillMethod = Literal[
    "heading_final",
    "fence_largest",
    "tail_strip",
    "empty",
]

Hint = Optional[Literal["code", "prose"]]

# Headings that mark the start of the "final" section (last match wins).
_HEADING_RE = re.compile(
    r"(?im)^(#{1,6}\s*)?(final|solution|answer|summary|conclusion)\b[^\n]*\n?"
)

# Fenced code blocks: ```lang?\n...\n```
_FENCE_RE = re.compile(
    r"```([a-zA-Z0-9_+-]*)\s*\n(.*?)```",
    re.DOTALL,
)

# Lines that often appear as browser/UI chrome or non-answer noise.
_CHROME_LINE_RE = re.compile(
    r"(?i)^("
    r"nav\b|home\b|search\b|menu\b|sign in\b|log in\b|"
    r"thinking\b|thought for\b|grok\b.*thinking|"
    r"cookie\b|accept all\b|skip to content\b"
    r").*$"
)


@dataclass(frozen=True)
class DistillResult:
    payload: str
    method: DistillMethod
    capped: bool

    def as_dict(self) -> dict:
        return {
            "payload": self.payload,
            "method": self.method,
            "capped": self.capped,
        }


def final_only(
    text: str,
    *,
    cap: int,
    hint: Hint = None,
    language: Optional[str] = None,
) -> DistillResult:
    """Return capped final-only payload for model_context.md."""
    if cap < 1:
        raise ValueError("cap must be >= 1")

    raw = (text or "").strip()
    if not raw:
        return DistillResult(payload="", method="empty", capped=False)

    # 1) Heading-based final section
    headed = _extract_heading_final(raw)
    if headed is not None and headed.strip():
        return _apply_cap(headed.strip(), "heading_final", cap)

    # 2) Largest fence when code-oriented
    if hint == "code" or language:
        fenced = _extract_fence_largest(raw, language=language)
        if fenced is not None and fenced.strip():
            return _apply_cap(fenced.strip(), "fence_largest", cap)

    # 3) Tail after chrome strip
    tail = _extract_tail_strip(raw)
    if tail.strip():
        return _apply_cap(tail.strip(), "tail_strip", cap)

    return DistillResult(payload="", method="empty", capped=False)


def _apply_cap(payload: str, method: DistillMethod, cap: int) -> DistillResult:
    if len(payload) <= cap:
        return DistillResult(payload=payload, method=method, capped=False)
    return DistillResult(payload=payload[:cap], method=method, capped=True)


def _extract_heading_final(text: str) -> Optional[str]:
    """Content after the last matching final-style heading."""
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return None
    last = matches[-1]
    return text[last.end() :]


def _extract_fence_largest(
    text: str,
    *,
    language: Optional[str] = None,
) -> Optional[str]:
    """Largest fenced body; prefer fences whose lang matches `language`."""
    blocks: list[tuple[str, str]] = []  # (lang, body)
    for m in _FENCE_RE.finditer(text):
        lang = (m.group(1) or "").strip().lower()
        body = m.group(2) or ""
        blocks.append((lang, body))

    if not blocks:
        return None

    lang_pref = (language or "").strip().lower()
    if lang_pref:
        preferred = [(lang, body) for lang, body in blocks if lang == lang_pref]
        if preferred:
            blocks = preferred

    # Prefer largest body by character length
    _, best = max(blocks, key=lambda lb: len(lb[1]))
    return best


def _extract_tail_strip(text: str) -> str:
    """Drop chrome-like lines; return a tail window before cap is applied."""
    lines = text.splitlines()
    kept = [ln for ln in lines if ln.strip() and not _CHROME_LINE_RE.match(ln.strip())]
    if not kept:
        return text.strip()

    # Tail window: last ~12k chars of cleaned text (cap applied later)
    cleaned = "\n".join(kept).strip()
    window = 12000
    if len(cleaned) > window:
        cleaned = cleaned[-window:]
    return cleaned


# --- helpers for tests / service ---

def distill_method_name(result: DistillResult) -> str:
    return result.method


__all__ = [
    "DistillResult",
    "DistillMethod",
    "final_only",
    "distill_method_name",
]