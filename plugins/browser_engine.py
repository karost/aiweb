"""AI Web V3 — Playwright engine (daemon-only).

V1-style anti-block launch + same-chat continuity:
  - One warm headed persistent context
  - NEVER page.goto(root) after first successful open
  - Store / restore conversation_url
  - Simple V1 capture loop (text-diff + Stop button + stall)
  - Only /aiweb-new forces a fresh chat
  - Capture confidence on new-diff vs chrome-stripped (not full page)
  - Dead-browser detection so service._get_engine() can restart
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import memory_manager as mem
from .artifacts import append_debug_log, capture_page_artifacts

DEFAULT_GROK_URL = os.environ.get("HERMES_AIWEB_GROK_URL", "https://x.com/i/grok")
GROK_URL = DEFAULT_GROK_URL

# Stall after last growth before considering response complete (ms)
STALL_MS = 2200.0
MIN_ELAPSED = 5.0

_DEAD_BROWSER_MARKERS = (
    "target closed",
    "target crashed",
    "browser has been closed",
    "context closed",
    "connection closed",
    "execution context was destroyed",
    "browser not started",
    "playwright connection",
)


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
    # V3: headed by default (anti-block). Override with HERMES_AIWEB_HEADED=0
    v = os.environ.get("HERMES_AIWEB_HEADED", "1").strip().lower()
    return v in {"1", "true", "yes", "on", ""}


def run_async(coro):
    """Run coroutine from sync context (daemon service handlers)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        try:
            import nest_asyncio
            nest_asyncio.apply()
            return loop.run_until_complete(coro)
        except ImportError:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Chrome / UI helpers (V1-style, minimal surface) — do not "improve" these
# ---------------------------------------------------------------------------

_CHROME_LINES = re.compile(
    r"^(sign in|log in|continue with|cookie|accept all|skip to content|"
    r"new chat|new conversation|thought for|thinking\.\.\.|searching\.\.\.)",
    re.I,
)


def _is_chrome_line(text: str) -> bool:
    t = (text or "").strip()
    if not t or len(t) < 3:
        return True
    return bool(_CHROME_LINES.match(t))


def _looks_like_nav_block(text: str) -> bool:
    t = (text or "").strip().lower()
    if len(t) < 8:
        return True
    hits = sum(
        1
        for k in ("sign in to continue", "continue with x", "log in to x", "cookie settings")
        if k in t
    )
    return hits >= 1 and len(t) < 400


def _strip_chrome(text: str, sent: str = "") -> str:
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    cleaned = []
    for ln in lines:
        if _is_chrome_line(ln):
            continue
        if sent and ln == sent:
            continue
        if ln.startswith("@") and len(ln) < 40:
            continue
        cleaned.append(ln)
    return "\n".join(cleaned).strip()


def _diff_new_text(before: str, after: str) -> str:
    if not after:
        return ""
    if not before:
        return after
    # Simple suffix diff
    if after.startswith(before):
        return after[len(before) :].lstrip()
    # Fallback: take the longer tail
    i = 0
    min_len = min(len(before), len(after))
    while i < min_len and before[i] == after[i]:
        i += 1
    return after[i:].lstrip()


def _is_dead_browser_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    name = type(exc).__name__.lower()
    if "targetclosed" in name or "targetclosederror" in name:
        return True
    return any(m in msg for m in _DEAD_BROWSER_MARKERS)


def _persisted_conversation_url() -> Optional[str]:
    """Last conversation_url from state.json (daemon restart). Avoids importing session."""
    try:
        path = mem.data_dir() / "state.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    url = data.get("conversation_url")
    if not isinstance(url, str):
        return None
    url = url.strip()
    low = url.lower()
    if url and "grok" in low and "login" not in low and "flow/" not in low:
        return url
    return None


def _capture_confidence(
    *,
    raw_new: str,
    cleaned: str,
    network: str = "",
    stable: str = "",
    extract: str = "",
) -> float:
    """Score extraction quality.

    Compare chrome-stripped reply to the *new-diff window*, never to the
    full page body (that ratio is always tiny and false-alarms).

    Short non-chrome answers ("Yes.", "42", "Done.") stay high-confidence.
    Low confidence = we threw away most of a large new-diff, or candidates disagree.
    """
    cleaned = (cleaned or "").strip()
    raw_new = (raw_new or "").strip()
    if not cleaned:
        return 0.0
    if _is_chrome_line(cleaned) or _looks_like_nav_block(cleaned):
        return 0.15

    # Real short answers are fine.
    if len(cleaned) < 40:
        base = 0.85
    else:
        base = 0.9

    if raw_new:
        ratio = len(cleaned) / max(1, len(raw_new))
        if len(raw_new) > 400 and ratio < 0.15:
            base = min(base, 0.28)
        elif len(raw_new) > 400 and ratio < 0.35:
            base = min(base, 0.45)
        elif ratio >= 0.5:
            base = max(base, 0.75)

    cands = [c.strip() for c in (stable, extract, network) if (c or "").strip()]
    if len(cands) >= 2:
        lengths = sorted(len(c) for c in cands)
        if lengths[-1] > 80 and lengths[0] * 3 < lengths[-1]:
            base = min(base, 0.5)

    if network and cleaned == network:
        base = max(base, 0.7)

    return round(min(1.0, max(0.0, base)), 2)


@dataclass
class CaptureResult:
    ok: bool
    text: str = ""
    page_url: str = ""
    conversation_url: Optional[str] = None
    needs_login: bool = False
    error: Optional[str] = None
    error_code: Optional[str] = None
    artifacts: list = field(default_factory=list)
    confidence: float = 1.0


class BrowserEngine:
    """
    Persistent Playwright context owned by the daemon.
    start() once; stop() only on explicit /aiweb-stop.
    Never navigates to root after first successful open.
    """

    def __init__(self, headless: Optional[bool] = None) -> None:
        self.headless = True if headless is None else headless
        self._playwright = None
        self._context = None
        self._page = None
        self._up = False
        self._last_network_text: Optional[str] = None
        self._network_done = False
        self._text_before_send = ""
        self._sent_message = ""
        self._last_stable_reply = ""
        self._last_raw_new = ""
        self._last_confidence = 1.0
        self._conversation_url: Optional[str] = None  # same-chat key
        self._first_open_done = False
        self._closing = False

    def is_up(self) -> bool:
        return bool(self._up and self._page is not None and not self._closing)

    def _mark_dead(self) -> None:
        """Page/context is gone. Keep refs so stop() can still try to close."""
        self._up = False

    @property
    def page(self):
        return self._page

    def current_url(self) -> str:
        try:
            return self._page.url if self._page else ""
        except Exception as e:
            if _is_dead_browser_error(e):
                self._mark_dead()
            return ""

    def conversation_url(self) -> Optional[str]:
        return self._conversation_url

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
                u = (p.url or "").lower()
                if "grok" in u:
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

    def _error_code_for(self, exc: BaseException) -> str:
        if _is_dead_browser_error(exc):
            self._mark_dead()
            return "browser_dead"
        return "internal"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

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
        self._closing = False

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
        # First open only: go to Grok if needed. After that we never re-navigate to root.
        await self.ensure_on_grok(force=False)

    async def stop(self) -> None:
        self._closing = True
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
        self._conversation_url = None
        self._first_open_done = False
        self._closing = False

    async def ensure_on_grok(self, *, force: bool = False) -> None:
        """
        Critical V3 rule:
          - force=False  → only navigate if we have no usable Grok tab
          - force=True   → used only by /aiweb-new
        After first successful open we never go back to root URL.
        On a cold start, prefer restoring the last conversation_url.
        """
        await self.start()
        assert self._page is not None

        # Prefer an already-open Grok tab
        picked = self._pick_grok_page()
        if picked is not None:
            self._bind_page(picked)

        url = (self._page.url or "").lower()

        if not force and self._first_open_done:
            # Already opened once → stay on current page (same-chat)
            try:
                await self._find_input()
                self._remember_conversation()
            except Exception as e:
                if _is_dead_browser_error(e):
                    self._mark_dead()
                    raise
            return

        if not force and "grok" in url:
            try:
                await self._find_input()
                self._first_open_done = True
                self._remember_conversation()
                return
            except Exception as e:
                if _is_dead_browser_error(e):
                    self._mark_dead()
                    raise
                # On Grok but composer missing → still do not force-reload unless forced
                self._first_open_done = True
                return

        # First open or explicit force.
        # Restore last conversation when this is not /aiweb-new.
        target = DEFAULT_GROK_URL
        if not force:
            restored = self._conversation_url or _persisted_conversation_url()
            if restored:
                target = restored

        try:
            await self._page.goto(target, wait_until="domcontentloaded", timeout=90_000)
        except Exception as e:
            if _is_dead_browser_error(e):
                self._mark_dead()
                raise
            if target != DEFAULT_GROK_URL:
                await self._page.goto(
                    DEFAULT_GROK_URL, wait_until="domcontentloaded", timeout=90_000
                )
            else:
                raise
        await asyncio.sleep(1.5)
        self._first_open_done = True
        self._remember_conversation()

    def _remember_conversation(self) -> None:
        try:
            url = self._page.url if self._page else ""
            if url and "grok" in url.lower():
                self._conversation_url = url
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def is_login_required(self) -> bool:
        if not self._page:
            return True
        try:
            content = (await self._page.locator("body").inner_text(timeout=3000)).lower()
        except Exception as e:
            if _is_dead_browser_error(e):
                self._mark_dead()
                raise
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
                        await asyncio.sleep(0.8)
                        return True
                    except Exception:
                        pass
            except Exception as e:
                if _is_dead_browser_error(e):
                    self._mark_dead()
                    return False
            await asyncio.sleep(2.0)
        return False

    async def login_interactive(self, *, timeout_sec: float = 600.0) -> CaptureResult:
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
                    self._remember_conversation()
                    return CaptureResult(
                        ok=True,
                        page_url=self.current_url(),
                        conversation_url=self._conversation_url,
                        text="login_ok",
                        confidence=1.0,
                    )
                except Exception:
                    pass
            ok = await self.wait_until_logged_in(timeout_sec=int(timeout_sec))
            if ok:
                self._remember_conversation()
                return CaptureResult(
                    ok=True,
                    page_url=self.current_url(),
                    conversation_url=self._conversation_url,
                    text="login_ok",
                    confidence=1.0,
                )
            arts = await self._artifacts("login", rid, "login wall still present", steps)
            return CaptureResult(
                ok=False,
                needs_login=True,
                error="login timeout",
                error_code="needs_login",
                artifacts=arts,
                page_url=self.current_url(),
                confidence=0.0,
            )
        except Exception as e:
            self._dbg(rid, f"login exception {e!r}", steps)
            return CaptureResult(
                ok=False,
                error=str(e),
                error_code=self._error_code_for(e),
                artifacts=[],
                confidence=0.0,
            )

    # ------------------------------------------------------------------
    # Input discovery (V1 selectors, ordered by stability)
    # ------------------------------------------------------------------

    async def _find_input(self, *, save_on_fail: bool = True):
        assert self._page is not None
        selectors = [
            # Current Grok / x.com patterns
            "textarea[placeholder*='Ask']",
            "textarea[placeholder*='ask']",
            "textarea[placeholder*='Message']",
            "textarea[placeholder*='message']",
            "textarea[placeholder*='Grok']",
            "textarea[placeholder*='What']",
            "[data-testid*='grok-input']",
            "[data-testid*='chat-input']",
            "[data-testid*='message-input']",
            "[data-testid='tweetTextarea_0']",
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
                    except Exception as e:
                        if _is_dead_browser_error(e):
                            self._mark_dead()
                            raise
                        continue
            except Exception as e:
                if _is_dead_browser_error(e):
                    self._mark_dead()
                    raise
                continue
        if save_on_fail:
            await self.save_failure_artifact("no_input")
        raise RuntimeError(
            "Could not find Grok chat input.\n"
            f"Log in at {DEFAULT_GROK_URL} in the browser window, then retry."
        )

    # ------------------------------------------------------------------
    # Network sniff (lightweight, same as V1)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Send + V1-style capture loop
    # ------------------------------------------------------------------

    async def send_message(self, message: str, timeout: Optional[int] = None) -> str:
        if not self._page:
            raise RuntimeError("Browser not started")
        timeout = int(timeout if timeout is not None else _timeout_sec())
        self._last_network_text = None
        self._network_done = False
        self._last_stable_reply = ""
        self._last_raw_new = ""
        self._last_confidence = 0.0
        self._sent_message = message

        # Critical: stay on current page (same-chat). No goto.
        try:
            input_box = await self._find_input()
            await input_box.click()
            await asyncio.sleep(0.25)
            await input_box.fill("")
            await input_box.fill(message)
            await asyncio.sleep(0.35)
            await input_box.press("Enter")

            self._text_before_send = await self._page_text()
            print(
                f"  [send] entered; waiting up to {timeout}s for NEW page text "
                f"(baseline_len={len(self._text_before_send)})",
                file=__import__("sys").stderr,
                flush=True,
            )

            await self._wait_for_response_stable(timeout=timeout)
            response = await self._extract_last_response()
        except Exception as e:
            if _is_dead_browser_error(e):
                self._mark_dead()
            raise

        candidates = []
        if self._last_stable_reply and self._last_stable_reply.strip():
            candidates.append(self._last_stable_reply.strip())
        if response and response.strip():
            candidates.append(response.strip())
        net = ""
        if self._last_network_text:
            net = _strip_chrome(self._last_network_text, self._sent_message)
            if net:
                candidates.append(net)

        result = max(candidates, key=len) if candidates else ""

        if result:
            lines = result.splitlines()
            if lines and lines[0].lower().startswith("thought for"):
                result = "\n".join(lines[1:]).strip() or result

        self._last_confidence = _capture_confidence(
            raw_new=self._last_raw_new,
            cleaned=result,
            network=net,
            stable=self._last_stable_reply,
            extract=response or "",
        )

        if not result or _is_chrome_line(result) or _looks_like_nav_block(result):
            await self.save_failure_artifact("empty_response")
            self._last_confidence = 0.0
            print(
                f"  [send] EMPTY extract stable_len={len(self._last_stable_reply or '')} "
                f"extract_len={len(response or '')} conf={self._last_confidence}",
                file=__import__("sys").stderr,
                flush=True,
            )
            return "(No response extracted)"

        self._remember_conversation()
        print(
            f"  [send] return_len={len(result)} "
            f"(stable={len(self._last_stable_reply or '')} extract={len(response or '')} "
            f"conf={self._last_confidence})",
            file=__import__("sys").stderr,
            flush=True,
        )
        return result

    async def _page_text(self) -> str:
        try:
            return await self._page.locator("body").inner_text(timeout=4000)
        except Exception as e:
            if _is_dead_browser_error(e):
                self._mark_dead()
            return ""

    async def _ui_still_generating(self) -> bool:
        if self._closing or not self._page:
            return False
        try:
            stop = self._page.locator(
                "button:has-text('Stop'), [aria-label*='Stop generating'], "
                "[aria-label*='Stop'], [data-testid*='stop']"
            )
            if await stop.count() > 0:
                try:
                    if await stop.first.is_visible(timeout=200):
                        return True
                except Exception as e:
                    if _is_dead_browser_error(e):
                        self._mark_dead()
                        raise
        except Exception as e:
            if _is_dead_browser_error(e):
                self._mark_dead()
                raise
        try:
            low = (await self._page_text()).lower()
            for marker in (
                "thinking about",
                "thinking...",
                "searching the web",
                "searching...",
                "generating",
            ):
                if marker in low:
                    return True
        except Exception:
            pass
        return False

    async def _new_content(self) -> tuple[int, str]:
        after = await self._page_text()
        new = _diff_new_text(self._text_before_send, after)
        sent = self._sent_message
        if sent and new.startswith(sent):
            new = new[len(sent) :].lstrip()
        # Keep pre-strip new-diff for confidence (not the full page).
        self._last_raw_new = new
        new = _strip_chrome(new, sent)
        if _looks_like_nav_block(new):
            new = ""
        if self._last_network_text:
            net = _strip_chrome(self._last_network_text, sent)
            if net and not _looks_like_nav_block(net) and len(net) > len(new):
                return len(net), net
        return len(new), new

    async def _wait_for_response_stable(self, timeout: int = 300) -> None:
        """V1-style simple loop: growth + Stop button + stall. Low CDP surface."""
        poll = 0.4
        started = False
        stable_ms = 0.0
        last_len = 0
        last_new = ""
        t0 = time.monotonic()
        last_log = 0.0

        while True:
            elapsed = time.monotonic() - t0
            if elapsed >= timeout:
                print(
                    f"  [wait] TIMEOUT {timeout}s started={started} last_len={last_len}",
                    file=__import__("sys").stderr,
                    flush=True,
                )
                if last_new.strip():
                    self._last_stable_reply = last_new.strip()
                await self.save_failure_artifact("timeout")
                return

            async def _poll():
                gen = await self._ui_still_generating()
                t, new = await self._new_content()
                return gen, t, new

            try:
                generating, t, new = await asyncio.wait_for(_poll(), timeout=4.0)
            except asyncio.TimeoutError:
                print(
                    f"  [wait] {elapsed:.0f}s poll-timeout (locator slow)",
                    file=__import__("sys").stderr,
                    flush=True,
                )
                await asyncio.sleep(poll)
                continue

            if new and (_is_chrome_line(new) or _looks_like_nav_block(new)):
                t, new = 0, ""

            grew = t > last_len or (
                new and new != last_new and len(new) >= len(last_new) and new != last_new
            )
            if t > 0 and (grew or (not started and t > 0)):
                if not started or grew:
                    started = True
                    if grew:
                        stable_ms = 0.0
                        last_len = t
                        last_new = new
            if started and not grew and not generating:
                stable_ms += poll * 1000.0
            elif generating:
                stable_ms = 0.0

            if elapsed - last_log >= 5.0:
                last_log = elapsed
                prev = (new or last_new or "")[:80].replace("\n", " ")
                print(
                    f"  [wait] {elapsed:.0f}s started={started} new_len={t} "
                    f"stable_ms={stable_ms:.0f} generating={generating} preview={prev!r}",
                    file=__import__("sys").stderr,
                    flush=True,
                )

            if (
                started
                and t >= 1
                and not generating
                and stable_ms >= STALL_MS
                and elapsed >= MIN_ELAPSED
                and not _is_chrome_line(new or last_new)
                and not _looks_like_nav_block(new or last_new)
            ):
                frozen = (new or last_new or "").strip()
                self._last_stable_reply = frozen
                print(
                    f"  [wait] DONE at {elapsed:.1f}s new_len={len(frozen)} "
                    f"preview={frozen[:80]!r}",
                    file=__import__("sys").stderr,
                    flush=True,
                )
                await asyncio.sleep(0.4)
                return

            await asyncio.sleep(poll)

    async def _extract_last_response(self) -> str:
        if self._last_stable_reply and len(self._last_stable_reply) > 20:
            body = self._last_stable_reply.strip()
            if body.isupper() and " " not in body and len(body) <= 24:
                return body
            return body

        if self._last_network_text and self._last_network_text.strip():
            net = _strip_chrome(self._last_network_text, self._sent_message)
            if net and not _looks_like_nav_block(net) and not _is_chrome_line(net):
                return net

        after = await self._page_text()
        new = _diff_new_text(self._text_before_send, after)
        sent = self._sent_message
        if sent and new.startswith(sent):
            new = new[len(sent) :].strip()
        if not self._last_raw_new:
            self._last_raw_new = new
        new = _strip_chrome(new, sent)
        if not new or _looks_like_nav_block(new):
            return ""

        lines = [ln.strip() for ln in new.splitlines() if ln.strip()]
        lines = [
            ln
            for ln in lines
            if not _is_chrome_line(ln)
            and ln != sent
            and not ln.startswith("@")
        ]
        if not lines:
            return ""

        for ln in reversed(lines):
            if ln.isupper() and 1 <= len(ln) <= 24 and " " not in ln:
                return ln

        return "\n".join(lines).strip()

    # ------------------------------------------------------------------
    # High-level capture used by service
    # ------------------------------------------------------------------

    async def submit_and_capture(
        self,
        prompt: str,
        *,
        request_id: str = "",
        op: str = "chat",
        force_new: bool = False,
    ) -> CaptureResult:
        steps: list[str] = []
        rid = request_id or "noreqid"
        try:
            self._dbg(
                rid,
                f"submit_and_capture op={op} force_new={force_new} prompt_len={len(prompt)}",
                steps,
            )
            await self.start()
            await self.ensure_on_grok(force=force_new)
            self._dbg(
                rid,
                f"up={self.is_up()} url={self.current_url()} conv={self._conversation_url} tabs={self._tab_urls()}",
                steps,
            )

            try:
                await self._find_input()
            except Exception as e:
                if _is_dead_browser_error(e):
                    self._mark_dead()
                arts = await self._artifacts(op, rid, "no composer on grok tab", steps)
                return CaptureResult(
                    ok=False,
                    needs_login=False,
                    error=str(e),
                    error_code=self._error_code_for(e)
                    if _is_dead_browser_error(e)
                    else "empty_extract",
                    artifacts=arts,
                    page_url=self.current_url(),
                    confidence=0.0,
                )

            if await self.is_login_required():
                arts = await self._artifacts(op, rid, "login wall", steps)
                return CaptureResult(
                    ok=False,
                    needs_login=True,
                    error="login required",
                    error_code="needs_login",
                    artifacts=arts,
                    page_url=self.current_url(),
                    confidence=0.0,
                )

            text = await self.send_message(prompt, timeout=int(_timeout_sec()))
            conf = float(self._last_confidence or 0.0)
            self._dbg(
                rid,
                f"captured_len={len(text or '')} net={bool(self._last_network_text)} conf={conf}",
                steps,
            )

            if not (text or "").strip() or text == "(No response extracted)":
                arts = await self._artifacts(op, rid, "empty response", steps)
                return CaptureResult(
                    ok=False,
                    error="empty extract",
                    error_code="empty_extract",
                    artifacts=arts,
                    page_url=self.current_url(),
                    conversation_url=self._conversation_url,
                    confidence=0.0,
                )

            return CaptureResult(
                ok=True,
                text=text,
                page_url=self.current_url(),
                conversation_url=self._conversation_url,
                confidence=conf,
            )
        except Exception as e:
            self._dbg(rid, f"exception {e!r}", steps)
            arts = await self._artifacts(op, rid, str(e), steps)
            return CaptureResult(
                ok=False,
                error=str(e),
                error_code=self._error_code_for(e),
                artifacts=arts,
                page_url=self.current_url(),
                confidence=0.0,
            )

    async def start_new_conversation(self) -> CaptureResult:
        """Only path that forces a new Grok chat (/aiweb-new)."""
        steps: list[str] = []
        rid = "new"
        try:
            await self.start()
            await self.ensure_on_grok(force=True)

            # Try explicit New chat button
            clicked = False
            for sel in [
                "button:has-text('New chat')",
                "button:has-text('New conversation')",
                "[aria-label*='New chat']",
                "[data-testid*='new-chat']",
                "[data-testid*='NewChat']",
            ]:
                try:
                    btn = self._page.locator(sel).first
                    if await btn.count() > 0 and await btn.is_visible(timeout=1500):
                        await btn.click()
                        await asyncio.sleep(1.2)
                        clicked = True
                        break
                except Exception as e:
                    if _is_dead_browser_error(e):
                        self._mark_dead()
                        raise
                    continue

            if not clicked:
                # Fallback: navigate to root (only place we intentionally do this)
                await self._page.goto(
                    DEFAULT_GROK_URL, wait_until="domcontentloaded", timeout=60_000
                )
                await asyncio.sleep(1.5)

            self._conversation_url = None
            self._first_open_done = True
            self._remember_conversation()

            try:
                await self._find_input()
            except Exception as e:
                arts = await self._artifacts("new", rid, "no composer after new chat", steps)
                return CaptureResult(
                    ok=False,
                    error=str(e),
                    error_code=self._error_code_for(e)
                    if _is_dead_browser_error(e)
                    else "empty_extract",
                    artifacts=arts,
                    page_url=self.current_url(),
                    confidence=0.0,
                )

            return CaptureResult(
                ok=True,
                text="new conversation started",
                page_url=self.current_url(),
                conversation_url=self._conversation_url,
                confidence=1.0,
            )
        except Exception as e:
            return CaptureResult(
                ok=False,
                error=str(e),
                error_code=self._error_code_for(e),
                confidence=0.0,
            )

    # ------------------------------------------------------------------
    # Artifacts
    # ------------------------------------------------------------------

    async def _artifacts(self, op: str, rid: str, note: str, steps: list[str]) -> list[str]:
        try:
            return await capture_page_artifacts(
                self._page,
                op=op,
                request_id=rid,
                note=note,
                extra_text="\n".join(steps),
            )
        except Exception:
            return []

    async def save_failure_artifact(self, reason: str) -> None:
        try:
            await capture_page_artifacts(
                self._page,
                op="failure",
                request_id=reason,
                note=reason,
            )
        except Exception:
            pass


__all__ = [
    "BrowserEngine",
    "CaptureResult",
    "PROFILE_DIR",
    "GROK_URL",
    "DEFAULT_GROK_URL",
    "run_async",
    "_headed_default",
    "_capture_confidence",
]