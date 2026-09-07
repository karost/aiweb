"""AI Web — Playwright engine (daemon-only).

V1 capture (network sniff, fill, decide_completion) + V2 keep-alive adapters.
Prefers restored Grok tab over about:blank pages[0].
"""

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
GROK_URL = DEFAULT_GROK_URL


def _profile_dir() -> Path:
    env = (os.environ.get("HERMES_AIWEB_PROFILE") or "").strip()
    if env:
        d = Path(env).expanduser()
    else:
        d = mem.data_dir() / "browser_profile"
    d.mkdir(parents=True, exist_ok=True)
    return d


PROFILE_DIR = _profile_dir()


def _timeout_sec() -> float:
    try:
        return max(30.0, float(os.environ.get("HERMES_AIWEB_TIMEOUT", "300")))
    except ValueError:
        return 300.0


def _headed_default() -> bool:
    return os.environ.get("HERMES_AIWEB_HEADED", "0") in ("1", "true", "yes")


def decide_completion(
    *,
    network_done: bool,
    ui_complete: bool,
    started: bool,
    text_len: int,
    html_len: int,
    child_count: int,
    last_text_len: int,
    last_html_len: int,
    last_child_count: int,
    stable_ms: float,
    stall_ms: float = 2500.0,
    min_chars: int = 20,
) -> tuple[bool, bool, float, int, int, int]:
    grew = (
        text_len > last_text_len
        or html_len > last_html_len
        or child_count > last_child_count
    )
    if grew:
        started = True
        stable_ms = 0.0

    if network_done:
        return True, started, stable_ms, text_len, html_len, child_count
    if ui_complete and started and text_len >= min_chars:
        return True, started, stable_ms, text_len, html_len, child_count
    if started and text_len >= min_chars and stable_ms >= stall_ms and not grew:
        return True, started, stable_ms, text_len, html_len, child_count

    return False, started, stable_ms, text_len, html_len, child_count


@dataclass
class CaptureResult:
    ok: bool
    text: str = ""
    page_url: str = ""
    needs_login: bool = False
    error: Optional[str] = None
    error_code: Optional[str] = None
    artifacts: list = field(default_factory=list)


class BrowserEngine:
    """Persistent Playwright context. start() once; stop() only on explicit stop."""

    def __init__(self, headless: Optional[bool] = None) -> None:
        self.headless = True if headless is None else headless
        self._playwright = None
        self._context = None
        self._page = None
        self._up = False
        self._last_network_text: Optional[str] = None
        self._network_done = False

    def is_up(self) -> bool:
        return bool(self._up and self._page is not None)

    @property
    def page(self):
        return self._page

    def current_url(self) -> str:
        try:
            return self._page.url if self._page else ""
        except Exception:
            return ""

    def _tab_urls(self) -> list[str]:
        if self._context is None:
            return []
        urls: list[str] = []
        for p in self._context.pages:
            try:
                urls.append(p.url or "")
            except Exception:
                urls.append("?")
        return urls

    def _pick_grok_page(self):
        if self._context is None:
            return None
        for p in self._context.pages:
            try:
                if "grok" in (p.url or "").lower():
                    return p
            except Exception:
                continue
        return None

    def _bind_page(self, page) -> None:
        self._page = page
        try:
            self._page.on("response", self._on_response_complete)
        except Exception:
            pass

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
            if headed is True and self.headless:
                await self.stop()
            else:
                picked = self._pick_grok_page()
                if picked is not None and picked is not self._page:
                    self._bind_page(picked)
                return

        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise RuntimeError(
                "Playwright not installed in daemon env. "
                "pip install playwright && playwright install chromium"
            ) from e

        use_headed = _headed_default() if headed is None else headed
        self.headless = not use_headed
        self._network_done = False
        self._last_network_text = None
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(_profile_dir()),
            headless=self.headless,
            viewport={"width": 1280, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            ignore_default_args=["--enable-automation"],
        )
        picked = self._pick_grok_page()
        if picked is not None:
            self._bind_page(picked)
        elif self._context.pages:
            self._bind_page(self._context.pages[0])
        else:
            self._bind_page(await self._context.new_page())
        self._up = True
        await self.ensure_on_grok(force=False)

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
        self._last_network_text = None
        self._network_done = False

    async def ensure_on_grok(self, *, force: bool = False) -> None:
        await self.start()
        picked = self._pick_grok_page()
        if picked is not None:
            self._bind_page(picked)
        assert self._page is not None
        url = (self._page.url or "").lower()
        if not force and "grok" in url:
            try:
                await self._find_input()
            except Exception:
                # Already on Grok. Do not reload (triggers JSD).
                return
            return
        await self._page.goto(DEFAULT_GROK_URL, wait_until="domcontentloaded", timeout=90_000)
        await asyncio.sleep(1.5)

    async def detect_login_wall(self) -> bool:
        return await self.is_login_required()

    async def is_login_required(self) -> bool:
        if not self._page:
            return True
        try:
            content = (await self._page.locator("body").inner_text()).lower()
        except Exception:
            return False
        url = (self._page.url or "").lower()
        if "flow/login" in url or url.rstrip("/").endswith("/login"):
            return True
        signals = (
            "sign in to x",
            "log in to x",
            "continue with x",
            "continue with twitter",
            "sign in to continue",
            "sign up to continue",
        )
        hits = sum(1 for s in signals if s in content)
        if hits >= 1:
            try:
                await self._find_input()
                return False
            except Exception:
                return True
        return False

    async def wait_until_logged_in(self, timeout_sec: int = 600) -> bool:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            try:
                if not await self.is_login_required():
                    try:
                        await self._find_input()
                        await asyncio.sleep(1.0)
                        return True
                    except Exception:
                        pass
            except Exception:
                pass
            await asyncio.sleep(2.0)
        return False

    async def login_interactive(self, *, timeout_sec: float = 300.0) -> CaptureResult:
        steps: list[str] = []
        rid = "login"
        try:
            self._dbg(rid, "login_interactive headed=True (keep-alive)", steps)
            await self.start(headed=True)
            await self.ensure_on_grok(force=False)
            self._dbg(rid, f"url={self.current_url()} tabs={self._tab_urls()}", steps)
            if not await self.is_login_required():
                try:
                    await self._find_input()
                    return CaptureResult(ok=True, page_url=self.current_url(), text="login_ok")
                except Exception:
                    pass
            ok = await self.wait_until_logged_in(timeout_sec=int(timeout_sec))
            if ok:
                return CaptureResult(ok=True, page_url=self.current_url(), text="login_ok")
            arts = await self._artifacts("login", rid, "login wall still present", steps)
            return CaptureResult(
                ok=False,
                needs_login=True,
                error="login timeout",
                error_code="needs_login",
                artifacts=arts,
                page_url=self.current_url(),
            )
        except Exception as e:  # noqa: BLE001
            self._dbg(rid, f"login exception {e!r}", steps)
            return CaptureResult(ok=False, error=str(e), error_code="browser_dead", artifacts=[])

    async def _on_response(self, response) -> None:
        try:
            url = response.url.lower()
            if response.status != 200:
                return
            if not any(
                k in url
                for k in ("grok", "conversation", "chat", "response", "completions", "stream")
            ):
                return
            ctype = response.headers.get("content-type", "")
            if "application/json" not in ctype and "text/event-stream" not in ctype:
                return
            try:
                data = await response.json()
            except Exception:
                return
            text = None
            if isinstance(data, dict):
                for key in ("response", "message", "content", "text", "output"):
                    if key in data and isinstance(data[key], str) and len(data[key]) > 20:
                        text = data[key]
                        break
                if not text and "choices" in data and isinstance(data["choices"], list):
                    try:
                        text = data["choices"][0]["message"]["content"]
                    except Exception:
                        pass
            if text and len(text.strip()) > 20:
                self._last_network_text = text.strip()
        except Exception:
            pass

    async def _on_response_complete(self, response) -> None:
        try:
            await self._on_response(response)
            url = response.url.lower()
            if response.status != 200:
                return
            if not any(
                k in url
                for k in (
                    "grok",
                    "conversation",
                    "chat",
                    "stream",
                    "responses",
                    "graphql",
                    "completions",
                )
            ):
                return
            try:
                await response.finished()
                self._network_done = True
            except Exception:
                pass
        except Exception:
            pass

    async def _find_input(self):
        assert self._page is not None
        selectors = [
            "textarea[placeholder*='Ask']",
            "textarea[placeholder*='ask']",
            "textarea[placeholder*='Message']",
            "textarea[placeholder*='message']",
            "textarea[placeholder*='Grok']",
            "textarea[placeholder*='What']",
            "[data-testid*='grok-input']",
            "[data-testid*='chat-input']",
            "[data-testid*='message-input']",
            "[role='textbox']",
            "[aria-label*='Ask']",
            "[aria-label*='Message']",
            "div[role='textbox'][contenteditable='true']",
            ".tiptap.ProseMirror[contenteditable='true']",
            ".ProseMirror[contenteditable='true']",
            "[contenteditable='true']",
            "textarea",
        ]
        for sel in selectors:
            try:
                loc = self._page.locator(sel)
                n = await loc.count()
                for i in range(n):
                    item = loc.nth(i)
                    try:
                        if await item.is_visible(timeout=400):
                            return item
                    except Exception:
                        continue
            except Exception:
                continue
        raise RuntimeError(
            "Could not find Grok chat input.\n"
            f"Log in at {DEFAULT_GROK_URL} in the browser window, then retry."
        )

    async def send_message(self, message: str, timeout: Optional[int] = None) -> str:
        if not self._page:
            raise RuntimeError("Browser not started")
        timeout = int(timeout if timeout is not None else _timeout_sec())
        self._last_network_text = None
        self._network_done = False

        input_box = await self._find_input()
        await input_box.click()
        await asyncio.sleep(0.25)
        await input_box.fill("")
        await input_box.fill(message)
        await asyncio.sleep(0.35)
        await input_box.press("Enter")

        await self._wait_for_response_stable(timeout=timeout)
        response = await self._extract_last_response()
        if self._last_network_text and len(self._last_network_text) > len(response or ""):
            return self._last_network_text
        return response.strip() if response else ""

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
            await self.ensure_on_grok(force=False)
            self._dbg(
                rid,
                f"up={self.is_up()} url={self.current_url()} tabs={self._tab_urls()}",
                steps,
            )

            try:
                await self._find_input()
            except Exception as e:
                arts = await self._artifacts(op, rid, "no composer on grok tab", steps)
                return CaptureResult(
                    ok=False,
                    needs_login=False,
                    error=str(e),
                    error_code="empty_extract",
                    artifacts=arts,
                    page_url=self.current_url(),
                )

            if await self.detect_login_wall():
                arts = await self._artifacts(op, rid, "login wall", steps)
                return CaptureResult(
                    ok=False,
                    needs_login=True,
                    error="login required",
                    error_code="needs_login",
                    artifacts=arts,
                    page_url=self.current_url(),
                )

            text = await self.send_message(prompt, timeout=int(_timeout_sec()))
            self._dbg(rid, f"captured_len={len(text or '')} net={bool(self._last_network_text)}", steps)
            if not (text or "").strip():
                arts = await self._artifacts(op, rid, "empty response", steps)
                return CaptureResult(
                    ok=False,
                    error="empty extract",
                    error_code="empty_extract",
                    artifacts=arts,
                    page_url=self.current_url(),
                )
            return CaptureResult(ok=True, text=text.strip(), page_url=self.current_url())
        except Exception as e:  # noqa: BLE001
            self._dbg(rid, f"exception {e!r}", steps)
            err = str(e).lower()
            code = "timeout" if "timeout" in err else "browser_dead"
            arts: list = []
            try:
                arts = await self._artifacts(op, rid, repr(e), steps)
            except Exception:
                pass
            return CaptureResult(ok=False, error=str(e), error_code=code, artifacts=arts)

    async def _ui_turn_complete(self) -> bool:
        assert self._page is not None
        try:
            stop = self._page.locator(
                "button:has-text('Stop'), [aria-label*='Stop generating'], [aria-label*='Stop']"
            )
            if await stop.count() > 0:
                try:
                    if await stop.first.is_visible(timeout=300):
                        return False
                except Exception:
                    pass
            actions = self._page.locator(
                "button:has-text('Copy'), [aria-label*='Copy'], [data-testid*='copy']"
            )
            if await actions.count() > 0:
                try:
                    if await actions.last.is_visible(timeout=300):
                        return True
                except Exception:
                    pass
        except Exception:
            pass
        return False

    async def _measure_last_assistant(self) -> tuple[int, int, int]:
        assert self._page is not None
        sels = [
            "[data-testid*='assistant']",
            "[data-message-author-role='assistant']",
            ".response-content-markdown",
            "[class*='assistant']",
            "[class*='response']",
        ]
        for sel in sels:
            try:
                loc = self._page.locator(sel).last
                if await loc.count() == 0:
                    continue
                text = (await loc.inner_text()) or ""
                html = (await loc.inner_html()) or ""
                children = await loc.evaluate("el => el.children.length")
                return len(text.strip()), len(html), int(children or 0)
            except Exception:
                continue
        try:
            body = await self._page.locator("body").inner_text()
            return len(body or ""), 0, 0
        except Exception:
            return 0, 0, 0

    async def _wait_for_response_stable(self, timeout: int = 300) -> None:
        poll = 0.5
        stall_ms = 2500.0
        started = False
        stable_ms = 0.0
        last_t = last_h = last_c = 0
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            ui_done = await self._ui_turn_complete()
            t, h, c = await self._measure_last_assistant()
            grew = t > last_t or h > last_h or c > last_c
            if grew:
                started = True
                stable_ms = 0.0
                last_t, last_h, last_c = t, h, c
            elif started:
                stable_ms += poll * 1000.0
            done, started, stable_ms, last_t, last_h, last_c = decide_completion(
                network_done=self._network_done,
                ui_complete=ui_done,
                started=started,
                text_len=t,
                html_len=h,
                child_count=c,
                last_text_len=last_t,
                last_html_len=last_h,
                last_child_count=last_c,
                stable_ms=stable_ms,
                stall_ms=stall_ms,
                min_chars=20,
            )
            if done:
                await asyncio.sleep(0.3)
                return
            await asyncio.sleep(poll)

    async def _extract_last_response(self) -> str:
        assert self._page is not None
        preferred = [
            "[data-testid*='response']",
            "[data-testid*='assistant']",
            "[data-message-author-role='assistant']",
            "[data-author='assistant']",
            ".response-content-markdown",
            "[class*='assistant']",
            "[class*='response']",
            "[class*='message-bubble']",
            "[class*='Message']",
        ]
        for sel in preferred:
            try:
                locs = self._page.locator(sel)
                count = await locs.count()
                if count > 0:
                    for i in range(count - 1, -1, -1):
                        try:
                            text = await locs.nth(i).inner_text()
                            clean = (text or "").strip()
                            if len(clean) > 25:
                                return clean
                        except Exception:
                            continue
            except Exception:
                continue
        generic = ["[class*='message']", "[class*='bubble']", "article", "div[role='article']"]
        for sel in generic:
            try:
                locs = self._page.locator(sel)
                count = await locs.count()
                if count > 0:
                    text = await locs.nth(count - 1).inner_text()
                    clean = (text or "").strip()
                    if len(clean) > 40:
                        return clean
            except Exception:
                continue
        if self._last_network_text:
            return self._last_network_text
        try:
            body = await self._page.locator("body").inner_text()
            return body[-4500:].strip()
        except Exception:
            return ""

    async def _artifacts(self, op: str, request_id: str, note: str, steps: list[str]) -> list:
        if self._page is None:
            return []
        return await capture_page_artifacts(
            self._page,
            op=op,
            request_id=request_id or "na",
            note=note,
            extra_text="\n".join(steps),
        )


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
    "GROK_URL",
    "PROFILE_DIR",
    "decide_completion",
    "run_async",
]