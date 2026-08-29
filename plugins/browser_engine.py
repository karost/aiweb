"""AI Web — Playwright browser engine (daemon-only) with step debug logs."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import memory_manager as mem
from .artifacts import append_debug_log, capture_page_artifacts

DEFAULT_GROK_URL = os.environ.get("HERMES_AIWEB_GROK_URL", "https://x.com/i/grok")


@dataclass
class CaptureResult:
    ok: bool
    text: str = ""
    page_url: str = ""
    needs_login: bool = False
    error: Optional[str] = None
    error_code: Optional[str] = None
    artifacts: list = field(default_factory=list)


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

    def _dbg(self, request_id: str, msg: str, steps: list[str]) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        steps.append(line)
        if request_id:
            try:
                append_debug_log(request_id, line)
            except Exception:
                pass

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
            await asyncio.sleep(1.5)

    async def detect_login_wall(self) -> bool:
        assert self._page is not None
        try:
            content = (await self._page.content()).lower()
        except Exception:
            return False
        url = (self._page.url or "").lower()
        if "login" in url or "flow/login" in url:
            return True
        markers = ("sign in to x", "log in to x", "/i/flow/login")
        hits = sum(1 for m in markers if m in content)
        return hits >= 1 and "grok" not in content[:3000]

    async def login_interactive(self, *, timeout_sec: float = 300.0) -> CaptureResult:
        steps: list[str] = []
        rid = "login"
        try:
            self._dbg(rid, "login_interactive start headed=True", steps)
            await self.start(headed=True)
            await self.ensure_on_grok()
            self._dbg(rid, f"url={self._page.url if self._page else ''}", steps)
            deadline = time.time() + timeout_sec
            while time.time() < deadline:
                if not await self.detect_login_wall():
                    self._dbg(rid, "login wall cleared", steps)
                    return CaptureResult(
                        ok=True,
                        page_url=self._page.url if self._page else "",
                        text="login_ok",
                    )
                await asyncio.sleep(2.0)
            arts = await self._artifacts("login", rid, "login wall still present", steps)
            return CaptureResult(
                ok=False,
                needs_login=True,
                error="login timeout",
                error_code="needs_login",
                artifacts=arts,
                page_url=self._page.url if self._page else "",
            )
        except Exception as e:  # noqa: BLE001
            self._dbg(rid, f"login exception {e!r}", steps)
            return CaptureResult(
                ok=False,
                error=str(e),
                error_code="browser_dead",
                artifacts=[],
            )

    async def _probe_inputs(self, page, steps: list[str], request_id: str) -> None:
        """Record what Playwright can see — for root-cause files."""
        probes = [
            'div[contenteditable="true"]',
            '[contenteditable="true"]',
            '[role="textbox"]',
            "textarea",
            '[data-testid="grok-input"]',
            '[data-testid*="grok" i]',
            '[data-testid*="tweet" i]',
            '[data-testid*="dmComposer" i]',
            'div[role="textbox"]',
        ]
        lines = ["PROBE_INPUTS"]
        for sel in probes:
            try:
                loc = page.locator(sel)
                n = await loc.count()
                vis = 0
                for i in range(min(n, 10)):
                    try:
                        if await loc.nth(i).is_visible():
                            vis += 1
                    except Exception:
                        pass
                lines.append(f"  sel={sel!r} count={n} visible~={vis}")
            except Exception as e:  # noqa: BLE001
                lines.append(f"  sel={sel!r} ERROR {e!r}")
        block = "\n".join(lines)
        self._dbg(request_id, block.replace("\n", " | "), steps)
        steps.append(block)

    async def _find_composer(self, page, steps: list[str], request_id: str):
        selectors = [
            '[data-testid="grok-input"]',
            'div[contenteditable="true"]',
            '[role="textbox"]',
            "textarea",
            'div[role="textbox"]',
        ]
        for sel in selectors:
            loc = page.locator(sel)
            try:
                n = await loc.count()
            except Exception:
                n = 0
            self._dbg(request_id, f"try sel={sel!r} count={n}", steps)
            if n == 0:
                continue
            for i in range(n - 1, -1, -1):
                cand = loc.nth(i)
                try:
                    if await cand.is_visible():
                        self._dbg(request_id, f"chosen sel={sel!r} index={i}", steps)
                        return cand, sel, i
                except Exception as e:  # noqa: BLE001
                    self._dbg(request_id, f"nth({i}) err {e!r}", steps)
        return None, None, None

    async def submit_and_capture(
        self,
        prompt: str,
        *,
        request_id: str = "",
        op: str = "chat",
    ) -> CaptureResult:
        steps: list[str] = []
        rid = request_id or "noreqid"
        try:
            self._dbg(rid, f"submit_and_capture op={op} prompt_len={len(prompt)}", steps)
            await self.start()
            await self.ensure_on_grok()
            self._dbg(rid, f"url={self._page.url if self._page else ''}", steps)

            if await self.detect_login_wall():
                self._dbg(rid, "login wall detected", steps)
                arts = await self._artifacts(op, rid, "login wall", steps)
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
            await self._probe_inputs(page, steps, rid)

            input_sel, sel_name, idx = await self._find_composer(page, steps, rid)
            if input_sel is None:
                self._dbg(rid, "NO_INPUT found", steps)
                arts = await self._artifacts(op, rid, "input not found", steps)
                return CaptureResult(
                    ok=False,
                    error="could not find Grok input",
                    error_code="empty_extract",
                    artifacts=arts,
                    page_url=page.url,
                )

            before = await self._response_fingerprint(page)
            self._dbg(rid, f"before_body_len={len(before)}", steps)

            await input_sel.scroll_into_view_if_needed()
            await input_sel.click(timeout=15_000)
            self._dbg(rid, f"clicked composer {sel_name}#{idx}", steps)

            await page.keyboard.press("Control+a")
            await page.keyboard.press("Backspace")
            await page.keyboard.type(prompt, delay=20)
            self._dbg(rid, "typed prompt via keyboard", steps)

            # Verify something landed in the composer
            try:
                typed = await input_sel.inner_text()
            except Exception:
                try:
                    typed = await input_sel.input_value()
                except Exception:
                    typed = ""
            self._dbg(rid, f"composer_inner_len={len(typed or '')} preview={typed[:80]!r}", steps)

            sent = False
            for bsel in (
                'button[aria-label*="Send" i]',
                'button[data-testid*="send" i]',
                'button:has-text("Send")',
            ):
                btn = page.locator(bsel).last
                try:
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.click(timeout=5000)
                        sent = True
                        self._dbg(rid, f"clicked send button {bsel}", steps)
                        break
                except Exception as e:  # noqa: BLE001
                    self._dbg(rid, f"send btn {bsel} err {e!r}", steps)
            if not sent:
                await page.keyboard.press("Enter")
                self._dbg(rid, "pressed Enter", steps)

            await asyncio.sleep(2.0)
            mid = await self._response_fingerprint(page)
            self._dbg(rid, f"after_send_body_len={len(mid)} delta={len(mid) - len(before)}", steps)

            text = await self._wait_for_response_stable(page, before, rid, steps)
            self._dbg(rid, f"final_text_len={len(text or '')}", steps)

            if not (text or "").strip():
                arts = await self._artifacts(op, rid, "empty response", steps)
                return CaptureResult(
                    ok=False,
                    error="empty extract",
                    error_code="empty_extract",
                    artifacts=arts,
                    page_url=page.url,
                )

            # If composer never received text, fail closed with evidence
            if prompt.strip() and typed is not None and len((typed or "").strip()) < 2:
                self._dbg(rid, "WARN composer still empty after type", steps)
                arts = await self._artifacts(op, rid, "type did not stick in composer", steps)
                return CaptureResult(
                    ok=False,
                    error="type did not stick in composer",
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
            self._dbg(rid, f"exception {e!r}", steps)
            err = str(e).lower()
            code = "timeout" if "timeout" in err else "browser_dead"
            arts: list = []
            try:
                arts = await self._artifacts(op, rid, repr(e), steps)
            except Exception:
                pass
            return CaptureResult(
                ok=False,
                error=str(e),
                error_code=code,
                artifacts=arts,
            )

    async def _response_fingerprint(self, page) -> str:
        try:
            return await page.inner_text("body")
        except Exception:
            return ""

    async def _wait_for_response_stable(
        self, page, before: str, request_id: str, steps: list[str]
    ) -> str:
        timeout_ms = _timeout_ms()
        deadline = time.time() + timeout_ms / 1000.0
        last = before
        stable_hits = 0
        min_growth = 80
        self._dbg(request_id, f"wait stable timeout_ms={timeout_ms}", steps)

        while time.time() < deadline:
            await asyncio.sleep(1.0)
            try:
                now = await page.inner_text("body")
            except Exception:
                continue
            grown = len(now) >= len(before) + min_growth and now != before
            if grown:
                if now == last:
                    stable_hits += 1
                    if stable_hits >= 4:
                        self._dbg(request_id, f"stable hit len={len(now)}", steps)
                        return now
                else:
                    stable_hits = 0
                    last = now
            else:
                stable_hits = 0
                last = now

        self._dbg(request_id, "wait stable TIMEOUT returning best effort", steps)
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
            if low.startswith("cookie") or low == "sign in":
                continue
            lines.append(ln)
        return "\n".join(lines).strip()

    async def _artifacts(
        self, op: str, request_id: str, note: str, steps: list[str]
    ) -> list:
        if self._page is None:
            return []
        return await capture_page_artifacts(
            self._page,
            op=op,
            request_id=request_id or "na",
            note=note,
            extra_text="\n".join(steps),
        )

    def current_url(self) -> str:
        try:
            return self._page.url if self._page else ""
        except Exception:
            return ""


def run_async(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    try:
        import nest_asyncio

        nest_asyncio.apply()
        return loop.run_until_complete(coro)
    except ImportError:
        return loop.run_until_complete(coro)


__all__ = [
    "BrowserEngine",
    "CaptureResult",
    "DEFAULT_GROK_URL",
    "run_async",
]