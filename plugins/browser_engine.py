"""AI Web — Playwright browser engine (daemon-only).

Keep-alive persistent context under data/aiweb/browser_profile.
Selectors for Grok UI are centralized and may need updates when the site changes.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import memory_manager as mem
from .artifacts import capture_page_artifacts

# Default entry — override with HERMES_AIWEB_GROK_URL
DEFAULT_GROK_URL = os.environ.get(
    "HERMES_AIWEB_GROK_URL",
    "https://x.com/i/grok",
)


@dataclass
class CaptureResult:
    ok: bool
    text: str = ""
    page_url: str = ""
    needs_login: bool = False
    error: Optional[str] = None
    error_code: Optional[str] = None
    artifacts: list[str] = None  # type: ignore

    def __post_init__(self) -> None:
        if self.artifacts is None:
            self.artifacts = []


def _profile_dir() -> Path:
    d = mem.data_dir() / "browser_profile"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _timeout_ms() -> int:
    try:
        sec = float(os.environ.get("HERMES_AIWEB_TIMEOUT", "300"))
    except ValueError:
        sec = 300.0
    return int(max(30.0, sec) * 1000)


def _headed() -> bool:
    return os.environ.get("HERMES_AIWEB_HEADED", "0") in ("1", "true", "yes")


class BrowserEngine:
    """Owns Playwright lifecycle inside the daemon process."""

    def __init__(self) -> None:
        self._playwright = None
        self._context = None
        self._page = None
        self._up = False

    def is_up(self) -> bool:
        return bool(self._up and self._page is not None)

    @property
    def page(self):
        return self._page

    async def start(self, *, headed: Optional[bool] = None) -> None:
        if self.is_up():
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise RuntimeError(
                "Playwright not installed in daemon env. "
                "pip install playwright && playwright install chromium"
            ) from e

        headed = _headed() if headed is None else headed
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(_profile_dir()),
            headless=not headed,
            viewport={"width": 1400, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = await self._context.new_page()
        self._up = True

    async def stop(self) -> None:
        self._up = False
        try:
            if self._context is not None:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:
            pass
        self._context = None
        self._page = None
        self._playwright = None

    async def ensure_on_grok(self) -> None:
        await self.start()
        assert self._page is not None
        url = self._page.url or ""
        if "grok" not in url.lower():
            await self._page.goto(DEFAULT_GROK_URL, wait_until="domcontentloaded")
            await asyncio.sleep(1.0)

    async def detect_login_wall(self) -> bool:
        assert self._page is not None
        try:
            content = (await self._page.content()).lower()
        except Exception:
            return False
        markers = (
            "sign in to x",
            "log in to x",
            "sign in",
            "/i/flow/login",
        )
        url = (self._page.url or "").lower()
        if "login" in url or "flow/login" in url:
            return True
        # Heuristic only — may false-positive
        hits = sum(1 for m in markers if m in content)
        return hits >= 2 and "grok" not in content[:2000]

    async def login_interactive(self, *, timeout_sec: float = 300.0) -> CaptureResult:
        """Open Grok headed and wait until login wall clears or timeout."""
        try:
            await self.start(headed=True)
            await self.ensure_on_grok()
            deadline = time.time() + timeout_sec
            while time.time() < deadline:
                if not await self.detect_login_wall():
                    return CaptureResult(
                        ok=True,
                        page_url=self._page.url if self._page else "",
                        text="login_ok",
                    )
                await asyncio.sleep(2.0)
            arts = await self._artifacts("login", "login-timeout", "login wall still present")
            return CaptureResult(
                ok=False,
                needs_login=True,
                error="login timeout",
                error_code="needs_login",
                artifacts=arts,
                page_url=self._page.url if self._page else "",
            )
        except Exception as e:  # noqa: BLE001
            return CaptureResult(
                ok=False,
                error=str(e),
                error_code="browser_dead",
                artifacts=[],
            )

    async def submit_and_capture(
        self,
        prompt: str,
        *,
        request_id: str = "",
        op: str = "chat",
    ) -> CaptureResult:
        """Type prompt into Grok UI, wait for reply, return visible text."""
        try:
            await self.start()
            await self.ensure_on_grok()
            if await self.detect_login_wall():
                arts = await self._artifacts(op, request_id, "login wall")
                return CaptureResult(
                    ok=False,
                    needs_login=True,
                    error="login required",
                    error_code="needs_login",
                    artifacts=arts,
                    page_url=self._page.url if self._page else "",
                )

            assert self._page is not None
            page = self._page

            # --- input: try several selectors (site may change) ---
            input_sel = await self._find_first(
                page,
                [
                    'div[contenteditable="true"]',
                    "textarea",
                    '[data-testid="grok-input"]',
                    '[role="textbox"]',
                ],
            )
            if input_sel is None:
                arts = await self._artifacts(op, request_id, "input not found")
                return CaptureResult(
                    ok=False,
                    error="could not find Grok input",
                    error_code="empty_extract",
                    artifacts=arts,
                    page_url=page.url,
                )

            before = await self._response_fingerprint(page)
            await input_sel.click()
            await input_sel.fill("")
            await input_sel.type(prompt, delay=5)
            await page.keyboard.press("Enter")

            text = await self._wait_for_response_stable(page, before)
            if not text.strip():
                arts = await self._artifacts(op, request_id, "empty response")
                return CaptureResult(
                    ok=False,
                    error="empty extract",
                    error_code="empty_extract",
                    artifacts=arts,
                    page_url=page.url,
                )

            return CaptureResult(
                ok=True,
                text=self._strip_chrome(text),
                page_url=page.url,
            )
        except Exception as e:  # noqa: BLE001
            err = str(e).lower()
            code = "timeout" if "timeout" in err else "browser_dead"
            arts: list[str] = []
            try:
                arts = await self._artifacts(op, request_id, repr(e))
            except Exception:
                pass
            return CaptureResult(
                ok=False,
                error=str(e),
                error_code=code,
                artifacts=arts,
            )

    async def _find_first(self, page, selectors: list[str]):
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    return loc
            except Exception:
                continue
        return None

    async def _response_fingerprint(self, page) -> str:
        try:
            return await page.inner_text("body")
        except Exception:
            return ""

    async def _wait_for_response_stable(self, page, before: str) -> str:
        """Poll body text until it grows past `before` and stabilizes."""
        timeout_ms = _timeout_ms()
        deadline = time.time() + timeout_ms / 1000.0
        last = before
        stable_hits = 0
        min_growth = 40

        while time.time() < deadline:
            await asyncio.sleep(1.0)
            try:
                now = await page.inner_text("body")
            except Exception:
                continue
            if len(now) >= len(before) + min_growth and now != before:
                if now == last:
                    stable_hits += 1
                    if stable_hits >= 3:
                        return now
                else:
                    stable_hits = 0
                    last = now
            else:
                stable_hits = 0
                last = now
        # Return best effort
        try:
            return await page.inner_text("body")
        except Exception:
            return last or ""

    def _strip_chrome(self, text: str) -> str:
        lines = []
        for ln in (text or "").splitlines():
            s = ln.strip()
            low = s.lower()
            if not s:
                lines.append(ln)
                continue
            if low in ("home", "explore", "notifications", "messages", "grok"):
                continue
            if low.startswith("cookie") or "sign in" == low:
                continue
            lines.append(ln)
        return "\n".join(lines).strip()

    async def _artifacts(self, op: str, request_id: str, note: str) -> list[str]:
        if self._page is None:
            return []
        return await capture_page_artifacts(
            self._page, op=op, request_id=request_id or "na", note=note
        )

    def current_url(self) -> str:
        try:
            return self._page.url if self._page else ""
        except Exception:
            return ""


# Sync wrappers for service when running inside daemon asyncio loop via run helpers

def run_async(coro):
    """Run coroutine; reuse running loop if present (nest_asyncio optional)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    try:
        import nest_asyncio

        nest_asyncio.apply()
        return loop.run_until_complete(coro)
    except ImportError:
        # Schedule not possible cleanly — caller should be async
        fut = asyncio.ensure_future(coro)
        return loop.run_until_complete(fut)


__all__ = [
    "BrowserEngine",
    "CaptureResult",
    "DEFAULT_GROK_URL",
    "run_async",
]