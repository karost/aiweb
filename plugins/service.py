"""AI Web V3 — daemon-side op dispatcher.

All heavy work (browser, capture, pipeline, write) lives here.
Client only does IPC; this module never runs inside the TUI worker.

Heavy ops queue on SessionManager.begin_heavy_blocking() instead of
returning busy. The slot is always released in a finally block.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Optional

from . import memory_manager as mem
from .artifacts import capture_text_failure, format_artifacts_suffix
from .browser_engine import BrowserEngine, CaptureResult, run_async, PROFILE_DIR, GROK_URL
from .response_pipeline import (
    process_response,
    format_chat_piece,
    next_more,
    default_response_size,
    LAST_RESPONSE_FILE,
    PIPELINE_STATE_FILE,
    load_pipeline_state,
)
from .session import get_session, HEAVY_OPS, CONTROL_OPS
from .write_policy import (
    resolve_out_path,
    WriteResolveErr,
    extract_primary_code,
    language_from_path,
    atomic_write,
    looks_like_chrome,
    write_inject_mode,
)

PROTOCOL_VERSION = 1
LOW_CONFIDENCE = 0.4
_DEAD_BROWSER_MARKERS = (
    "target closed",
    "target crashed",
    "browser has been closed",
    "context closed",
    "connection closed",
    "browser not started",
)

model_pack_cap = mem.model_pack_cap


def _queue_timeout() -> float:
    try:
        return max(1.0, float(os.environ.get("HERMES_AIWEB_QUEUE_TIMEOUT", "300")))
    except ValueError:
        return 300.0


def _headed() -> bool:
    return os.environ.get("HERMES_AIWEB_HEADED", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "",
    }


def _looks_like_dead_browser(err: Optional[str], code: Optional[str] = None) -> bool:
    if (code or "") == "browser_dead":
        return True
    low = (err or "").lower()
    return any(m in low for m in _DEAD_BROWSER_MARKERS)


def _maybe_mark_dead(sess: Any, cap: CaptureResult) -> None:
    """If Playwright died mid-op, drop the cached engine so the next call can restart."""
    if _looks_like_dead_browser(cap.error, cap.error_code):
        sess.mark_browser_down()


def _confidence_suffix(cap: CaptureResult) -> str:
    """Warn on thin extracts. Safe if CaptureResult has no confidence field yet."""
    conf = float(getattr(cap, "confidence", 1.0) or 1.0)
    if not cap.ok or conf >= LOW_CONFIDENCE:
        return ""
    return (
        f"\n\n_⚠️ low-confidence extraction ({conf:.2f}) — "
        f"page layout may have changed; treat this reply with care._"
    )


def _acquire_heavy(sess: Any, request_id: str, op: str) -> Optional[dict[str, Any]]:
    """Acquire the heavy slot (blocking). Return an error result, or None if held."""
    acquired, reason = sess.begin_heavy_blocking(timeout=_queue_timeout())
    if acquired:
        return None
    return _base_result(
        ok=False,
        message=f"AI Web queue timeout: {reason}",
        request_id=request_id,
        op=op,
        error_code="queue_timeout",
        error=reason,
    )


def _mark_first_chunk_shown(pipe: Any) -> None:
    """Advance pipeline chunk_index so /aiweb-more does not repeat part 1."""
    if getattr(pipe, "path", "") != "large" or getattr(pipe, "total_chunks", 1) <= 1:
        return
    try:
        st = load_pipeline_state()
        if not st or not st.get("chunks"):
            return
        if int(st.get("chunk_index", 0)) == 0:
            st["chunk_index"] = 1
            PIPELINE_STATE_FILE.write_text(
                json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    except Exception:
        pass


def _get_engine(*, retries: int = 2, backoff: float = 2.0) -> BrowserEngine:
    """Return a live engine.

    Retries start-up only (profile lock, crashed Chromium). Never retries a
    login wall. On a failed start(), stop() the half-open engine so retries
    do not orphan Chromium processes.
    """
    sess = get_session()
    current = sess.engine
    if current is not None and getattr(current, "is_up", lambda: False)():
        return current
    if current is not None:
        try:
            run_async(current.stop())
        except Exception:
            pass
        sess.mark_browser_down()

    last_err: Optional[Exception] = None
    attempts = retries + 1
    for attempt in range(1, attempts + 1):
        engine = BrowserEngine(headless=not _headed())
        try:
            run_async(engine.start())
            sess.bind_engine(engine)
            return sess.engine  # type: ignore[return-value]
        except Exception as e:
            last_err = e
            try:
                run_async(engine.stop())
            except Exception:
                pass
            sess.mark_browser_down()
            if attempt < attempts:
                time.sleep(backoff * attempt)
    raise RuntimeError(
        f"browser failed to start after {attempts} attempt(s): {last_err}"
    )


def _base_result(
    *,
    ok: bool,
    message: str,
    request_id: str,
    op: str,
    error: Optional[str] = None,
    error_code: Optional[str] = None,
    artifacts: Optional[list] = None,
    path: str = "",
    chars: int = 0,
    full_path: Optional[str] = None,
    gen_id: Optional[str] = None,
    more_available: bool = False,
    chunk_index: int = 0,
    chunk_total: int = 0,
    inject: Optional[dict] = None,
    file_path: Optional[str] = None,
    file_bytes: int = 0,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "message": message,
        "request_id": request_id,
        "op": op,
        "error": error,
        "error_code": error_code,
        "artifacts": artifacts or [],
        "path": path,
        "chars": chars,
        "full_path": full_path,
        "gen_id": gen_id,
        "more_available": more_available,
        "chunk_index": chunk_index,
        "chunk_total": chunk_total,
        "inject": inject
        or mem.inject_info_snapshot(written=False, pending=False, chars=0, mode="none"),
        "file_path": file_path,
        "file_bytes": file_bytes,
        "protocol_version": PROTOCOL_VERSION,
    }


def format_chat_meta(
    *,
    path: str,
    chars: int,
    full_path: Optional[str],
    more_available: bool,
    chunk_index: int,
    chunk_total: int,
    inject_note: str,
) -> str:
    parts = [f"{path} {chars}c"]
    if more_available:
        parts.append(f"part {chunk_index}/{chunk_total} · next `/aiweb-more`")
    if full_path:
        parts.append(f"full `{full_path}`")
    parts.append(inject_note)
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Op handlers
# ---------------------------------------------------------------------------

def _handle_status(request_id: str) -> dict[str, Any]:
    sess = get_session()
    st = sess.status.to_public_dict()
    engine = sess.engine
    headed = "headed" if (engine and not getattr(engine, "headless", True)) else "headless/unknown"
    qdepth = int(st.get("queue_depth") or 0)
    lines = [
        f"**AI Web Status** (v3.0.0)",
        f"",
        f"Daemon pid: `{st.get('daemon_pid')}`",
        f"Browser: **{'up' if st.get('browser_up') else 'down'}** ({headed})",
        f"State: `{st.get('state')}`",
        f"Page: `{st.get('page_url') or '—'}`",
        f"Conversation: `{st.get('conversation_url') or '—'}`",
        f"Login required: {st.get('login_required')}",
        f"Busy: {st.get('busy')}",
        f"Queue: **{qdepth}** waiting (timeout `{int(_queue_timeout())}s`)",
        f"Inject pending: {st.get('inject_pending')} (sticky={mem.is_model_sticky()})",
        f"Model pack cap: {mem.model_pack_cap()} chars",
        f"Response default size: {default_response_size()} chars",
        f"Profile: `{PROFILE_DIR}`",
        f"Out dir: `{mem.get_out_dir()}`",
        f"Grok URL: `{GROK_URL}`",
        f"Last gen: `{st.get('last_gen_id') or '—'}`",
        f"",
        mem.load_hot_context(max_chars=1500),
        f"",
        "_Same-chat by default. Only `/aiweb-new` starts a fresh conversation._",
        "_Concurrent `/aiweb*` calls queue; they no longer fail instantly with busy._",
    ]
    return _base_result(
        ok=True,
        message="\n".join(lines),
        request_id=request_id,
        op="status",
    )


def _handle_clear_model(request_id: str) -> dict[str, Any]:
    mem.clear_model_context()
    return _base_result(
        ok=True,
        message="✅ Cleared model inject buffer and sticky flag.",
        request_id=request_id,
        op="clear_model",
    )


def _handle_keep_model(request_id: str) -> dict[str, Any]:
    mem.set_sticky(True)
    return _base_result(
        ok=True,
        message="✅ Model context is now **sticky** (re-injected each Hermes turn until `/aiweb-clear-model`).",
        request_id=request_id,
        op="keep_model",
    )


def _handle_more(request_id: str) -> dict[str, Any]:
    body = next_more()
    return _base_result(
        ok=True,
        message=body,
        request_id=request_id,
        op="more",
    )


def _handle_stop(request_id: str, **kwargs: Any) -> dict[str, Any]:
    """Close the browser without taking the heavy slot (interrupt is intentional)."""
    stop_daemon = bool(kwargs.get("daemon") or kwargs.get("stop_daemon"))
    sess = get_session()
    msg_parts = []
    if sess.engine is not None:
        try:
            run_async(sess.engine.stop())
        except Exception as e:
            msg_parts.append(f"browser stop warning: {e}")
        sess.mark_browser_down()
        msg_parts.append("✅ Browser session closed. Profile cookies kept on disk.")
        msg_parts.append(f"`{PROFILE_DIR}`")
    else:
        msg_parts.append("Browser session already closed.")

    if stop_daemon:
        msg_parts.append("Daemon will exit after this response.")
    return _base_result(
        ok=True,
        message="\n".join(msg_parts),
        request_id=request_id,
        op="stop",
    )


def _handle_login(request_id: str) -> dict[str, Any]:
    sess = get_session()
    blocked = _acquire_heavy(sess, request_id, "login")
    if blocked:
        return blocked
    op_ok = False
    op_err: Optional[str] = None
    try:
        engine = _get_engine()
        cap: CaptureResult = run_async(engine.login_interactive(timeout_sec=600))
        if cap.ok:
            sess.mark_ready(page_url=cap.page_url, conversation_url=cap.conversation_url)
            op_ok = True
            return _base_result(
                ok=True,
                message=(
                    "✅ Login OK. Browser stays open (keep-alive).\n"
                    f"Page: `{cap.page_url}`\n"
                    f"Conversation: `{cap.conversation_url or '—'}`\n"
                    "Use `/aiweb` / `/aiweb-chat`. Close with `/aiweb-stop`."
                ),
                request_id=request_id,
                op="login",
            )
        if cap.needs_login:
            sess.mark_needs_login(page_url=cap.page_url)
        else:
            _maybe_mark_dead(sess, cap)
        op_err = cap.error
        return _base_result(
            ok=False,
            message=f"Login failed: {cap.error}" + format_artifacts_suffix(cap.artifacts or []),
            request_id=request_id,
            op="login",
            error=cap.error,
            error_code=cap.error_code or "needs_login",
            artifacts=cap.artifacts or [],
        )
    except Exception as e:
        op_err = str(e)
        return _base_result(
            ok=False,
            message=f"login error: {e}",
            request_id=request_id,
            op="login",
            error=str(e),
            error_code="internal",
        )
    finally:
        sess.end_heavy(ok=op_ok, error=op_err)


def _handle_chat(
    op: str,
    request_id: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Shared path for aiweb / chat / run / load."""
    message = (kwargs.get("message") or kwargs.get("prompt") or "").strip()
    if not message:
        return _base_result(
            ok=False,
            message=f"Usage: /{op} <prompt>",
            request_id=request_id,
            op=op,
            error="empty prompt",
            error_code="invalid_args",
        )

    # Token policy: only /aiweb queues the final-only model inject.
    # /aiweb-chat displays in chat without polluting the model context
    # (saves input tokens); /aiweb pays the inject cost by design.
    do_inject = op == "aiweb"
    sess = get_session()
    blocked = _acquire_heavy(sess, request_id, op)
    if blocked:
        return blocked
    op_ok = False
    op_err: Optional[str] = None
    try:
        engine = _get_engine()
        cap: CaptureResult = run_async(
            engine.submit_and_capture(message, request_id=request_id, op=op, force_new=False)
        )

        if cap.needs_login:
            sess.mark_needs_login(page_url=cap.page_url)
            op_err = cap.error
            return _base_result(
                ok=False,
                message="Login required. Use `/aiweb-login`."
                + format_artifacts_suffix(cap.artifacts or []),
                request_id=request_id,
                op=op,
                error=cap.error,
                error_code="needs_login",
                artifacts=cap.artifacts or [],
            )

        if not cap.ok:
            _maybe_mark_dead(sess, cap)
            op_err = cap.error
            if not cap.artifacts:
                cap.artifacts = capture_text_failure(
                    op=op, request_id=request_id, note=cap.error or "fail", body=cap.text
                )
            return _base_result(
                ok=False,
                message=f"Capture failed: {cap.error}"
                + format_artifacts_suffix(cap.artifacts or []),
                request_id=request_id,
                op=op,
                error=cap.error,
                error_code=cap.error_code or "empty_extract",
                artifacts=cap.artifacts or [],
            )

        pipe = process_response(cap.text, inject_model=do_inject)
        gen_id = str(uuid.uuid4())[:8]
        sess.set_last_gen(gen_id)
        sess.mark_ready(page_url=cap.page_url, conversation_url=cap.conversation_url)

        inject_meta = mem.inject_info_snapshot(
            written=False, pending=False, chars=0, mode="none"
        )
        inject_note = "no inject"
        if do_inject and pipe.model_pack:
            mem.write_model_context(pipe.model_pack)
            inject_meta = mem.inject_info_snapshot(
                written=True,
                pending=True,
                chars=len(pipe.model_pack),
                mode="final_only",
                distill_method="pack",
                capped=pipe.model_truncated,
                cap=mem.model_pack_cap(),
            )
            inject_note = (
                f"inject pending {len(pipe.model_pack)}c · final_only"
                + (" · capped" if pipe.model_truncated else "")
            )

        more = pipe.path == "large" and pipe.total_chunks > 1
        meta = format_chat_meta(
            path=pipe.path,
            chars=pipe.chars,
            full_path=pipe.file_path,
            more_available=more,
            chunk_index=1 if more else 0,
            chunk_total=pipe.total_chunks,
            inject_note=inject_note if do_inject else "no inject",
        )
        body = format_chat_piece(pipe, 0)
        _mark_first_chunk_shown(pipe)
        # [aiweb] /aiweb-chat output lands in the Hermes chat itself; drop the
        # meta footer there (user request) except for multi-chunk answers,
        # where the /aiweb-more + full-file pointer is still needed.
        if op == "chat" and not more:
            msg = f"{body}{_confidence_suffix(cap)}"
        else:
            msg = f"{body}\n\n_({meta})_{_confidence_suffix(cap)}"

        mem.append_hot_summary(f"[{op}] {message[:80]} → {pipe.path} {pipe.chars}c")
        op_ok = True
        return _base_result(
            ok=True,
            message=msg,
            request_id=request_id,
            op=op,
            path=pipe.path,
            chars=pipe.chars,
            full_path=pipe.file_path,
            gen_id=gen_id,
            more_available=more,
            chunk_index=1 if more else 0,
            chunk_total=pipe.total_chunks,
            inject=inject_meta,
        )
    except Exception as e:
        op_err = str(e)
        if _looks_like_dead_browser(str(e)):
            sess.mark_browser_down()
        return _base_result(
            ok=False,
            message=f"internal error: {e}",
            request_id=request_id,
            op=op,
            error=str(e),
            error_code="internal",
        )
    finally:
        sess.end_heavy(ok=op_ok, error=op_err)


def _handle_new(request_id: str, **kwargs: Any) -> dict[str, Any]:
    """Only command that starts a fresh Grok conversation."""
    extra = (kwargs.get("message") or "").strip()
    sess = get_session()
    blocked = _acquire_heavy(sess, request_id, "new")
    if blocked:
        return blocked
    op_ok = False
    op_err: Optional[str] = None
    try:
        engine = _get_engine()
        cap = run_async(engine.start_new_conversation())
        if not cap.ok:
            _maybe_mark_dead(sess, cap)
            op_err = cap.error
            return _base_result(
                ok=False,
                message=f"Failed to start new chat: {cap.error}"
                + format_artifacts_suffix(cap.artifacts or []),
                request_id=request_id,
                op="new",
                error=cap.error,
                error_code=cap.error_code or "empty_extract",
                artifacts=cap.artifacts or [],
            )

        sess.mark_ready(page_url=cap.page_url, conversation_url=cap.conversation_url)
        note = "Fresh Grok conversation started."
        page_url = cap.page_url

        if extra:
            # Keep the heavy slot through the optional first message
            # (original released the lock first — a race with a queued /aiweb).
            cap2 = run_async(
                engine.submit_and_capture(
                    extra, request_id=request_id, op="new", force_new=False
                )
            )
            if cap2.ok:
                pipe = process_response(cap2.text, inject_model=False)
                body = format_chat_piece(pipe, 0)
                _mark_first_chunk_shown(pipe)
                note = (
                    f"Fresh conversation + first reply:\n\n{body}"
                    f"{_confidence_suffix(cap2)}"
                )
                sess.mark_ready(
                    page_url=cap2.page_url, conversation_url=cap2.conversation_url
                )
                page_url = cap2.page_url or page_url
            else:
                _maybe_mark_dead(sess, cap2)
                note = (
                    "Fresh Grok conversation started, but the first message failed: "
                    f"{cap2.error}"
                    + format_artifacts_suffix(cap2.artifacts or [])
                )

        op_ok = True
        return _base_result(
            ok=True,
            message=note + f"\n\n_Page: `{page_url}`_",
            request_id=request_id,
            op="new",
        )
    except Exception as e:
        op_err = str(e)
        if _looks_like_dead_browser(str(e)):
            sess.mark_browser_down()
        return _base_result(
            ok=False,
            message=f"new chat error: {e}",
            request_id=request_id,
            op="new",
            error=str(e),
            error_code="internal",
        )
    finally:
        sess.end_heavy(ok=op_ok, error=op_err)


def _handle_write(request_id: str, **kwargs: Any) -> dict[str, Any]:
    path = (kwargs.get("path") or "").strip()
    prompt = (kwargs.get("message") or kwargs.get("prompt") or "").strip()
    if not path or not prompt:
        return _base_result(
            ok=False,
            message="Usage: /aiweb-write <path> <prompt>",
            request_id=request_id,
            op="write",
            error="path and prompt required",
            error_code="invalid_args",
        )

    resolved = resolve_out_path(path)
    if isinstance(resolved, WriteResolveErr):
        return _base_result(
            ok=False,
            message=resolved.error,
            request_id=request_id,
            op="write",
            error=resolved.error,
            error_code=resolved.error_code,
        )

    sess = get_session()
    blocked = _acquire_heavy(sess, request_id, "write")
    if blocked:
        return blocked

    lang = language_from_path(resolved.target)
    system_hint = (
        f"Reply with a single fenced code block"
        f"{f' in {lang}' if lang else ''} containing only the file content. "
        f"No outer explanation before the fence."
    )
    full_prompt = f"{system_hint}\n\n{prompt}"

    op_ok = False
    op_err: Optional[str] = None
    try:
        engine = _get_engine()
        cap: CaptureResult = run_async(
            engine.submit_and_capture(
                full_prompt, request_id=request_id, op="write", force_new=False
            )
        )
        if not cap.ok:
            code = cap.error_code or "empty_extract"
            if cap.needs_login:
                sess.mark_needs_login(page_url=cap.page_url)
                code = "needs_login"
            else:
                _maybe_mark_dead(sess, cap)
            op_err = cap.error
            return _base_result(
                ok=False,
                message=f"Capture failed: {cap.error}"
                + format_artifacts_suffix(cap.artifacts or []),
                request_id=request_id,
                op="write",
                error=cap.error,
                error_code=code,
                artifacts=cap.artifacts or [],
            )

        pipe = process_response(cap.text, inject_model=False)
        body, reason = extract_primary_code(pipe.full_text, language=lang)
        if not body or looks_like_chrome(body):
            sess.mark_ready(page_url=cap.page_url, conversation_url=cap.conversation_url)
            op_err = reason
            arts = capture_text_failure(
                op="write",
                request_id=request_id,
                note=reason or "extract failed",
                body=pipe.full_text,
            )
            return _base_result(
                ok=False,
                message=f"Write extract failed: {reason}. See raw last_response.md."
                + format_artifacts_suffix(arts)
                + _confidence_suffix(cap),
                request_id=request_id,
                op="write",
                error=reason,
                error_code="write_extract_failed",
                artifacts=arts,
                path=pipe.path,
                chars=pipe.chars,
                full_path=pipe.file_path,
            )

        nbytes = atomic_write(resolved.target, body)
        inject_meta = mem.inject_info_snapshot(
            written=False, pending=False, chars=0, mode="none"
        )
        if write_inject_mode() == "path_only":
            mem.write_path_only_inject(str(resolved.target))
            inject_meta = mem.inject_info_snapshot(
                written=True,
                pending=True,
                chars=len(str(resolved.target)) + 40,
                mode="path_only",
                distill_method="path_only",
                capped=False,
            )

        preview = "\n".join(body.splitlines()[:12])
        if len(body.splitlines()) > 12:
            preview += "\n…"

        sess.mark_ready(page_url=cap.page_url, conversation_url=cap.conversation_url)
        mem.append_hot_summary(f"[write] {resolved.target.name} ({nbytes}b)")
        op_ok = True
        return _base_result(
            ok=True,
            message=(
                f"✅ Wrote `{resolved.target}` ({nbytes} bytes)\n"
                f"```\n{preview}\n```\n"
                f"_(no full-body inject · raw: {pipe.file_path or LAST_RESPONSE_FILE})_"
                f"{_confidence_suffix(cap)}"
            ),
            request_id=request_id,
            op="write",
            path=pipe.path,
            chars=pipe.chars,
            full_path=pipe.file_path,
            file_path=str(resolved.target),
            file_bytes=nbytes,
            inject=inject_meta,
        )
    except Exception as e:
        op_err = str(e)
        if _looks_like_dead_browser(str(e)):
            sess.mark_browser_down()
        return _base_result(
            ok=False,
            message=f"write error: {e}",
            request_id=request_id,
            op="write",
            error=str(e),
            error_code="internal",
        )
    finally:
        sess.end_heavy(ok=op_ok, error=op_err)


def _handle_reset_memory(request_id: str) -> dict[str, Any]:
    mem.clear_model_context()
    mem.set_sticky(False)
    mem.clear_hot_and_archive()
    mem.append_hot_summary("memory reset")
    return _base_result(
        ok=True,
        message="✅ AI Web hot/archive memory reset (model inject cleared).",
        request_id=request_id,
        op="reset_memory",
    )


def _handle_summary(request_id: str, **kwargs: Any) -> dict[str, Any]:
    text = (kwargs.get("message") or kwargs.get("text") or kwargs.get("args") or "").strip()
    if not text:
        text = "Manual summary"
    mem.append_hot_summary(text)
    return _base_result(
        ok=True,
        message="✅ Summary saved.",
        request_id=request_id,
        op="summary",
    )


def _handle_run(request_id: str, **kwargs: Any) -> dict[str, Any]:
    prompt = (kwargs.get("message") or kwargs.get("prompt") or "").strip()
    if not prompt:
        return _base_result(
            ok=False,
            message="Usage: /aiweb-run <prompt>",
            request_id=request_id,
            op="run",
            error="empty prompt",
            error_code="invalid_args",
        )
    # No local exec in V3 — ask Grok for code. Shares /aiweb inject path.
    return _handle_chat(
        "aiweb",
        request_id,
        message=(
            f"{prompt}\n\n"
            "IMPORTANT: Reply with ONLY complete runnable Python code. "
            "Markdown code block OK."
        ),
    )


def _handle_load(request_id: str, **kwargs: Any) -> dict[str, Any]:
    """Inject hot memory into the CURRENT conversation (no New chat)."""
    extra = (kwargs.get("message") or kwargs.get("prompt") or "").strip()
    hot = mem.load_hot_context()
    context_message = (
        "You are continuing a previous conversation. Context:\n\n"
        f"{hot}\n\n"
        "Acknowledge briefly that you received this context."
    )
    if extra:
        context_message += f"\n\nExtra: {extra}"
    return _handle_chat("aiweb", request_id, message=context_message)


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def handle(op: str, request_id: str = "", **kwargs: Any) -> dict[str, Any]:
    op = (op or "").strip().lower()
    rid = request_id or str(uuid.uuid4())

    sess = get_session()
    can, reason = sess.can_run(op)
    if not can:
        # CONTROL_OPS and HEAVY_OPS both pass in the queued SessionManager.
        # Kept as a backstop if can_run grows a real deny (e.g. shutdown).
        return _base_result(
            ok=False,
            message=f"AI Web busy ({reason}).",
            request_id=rid,
            op=op,
            error_code="busy",
            error=reason,
        )

    if op == "hello":
        return {
            "ok": True,
            "protocol_version": PROTOCOL_VERSION,
            "daemon_version": "3.0.0",
            "message": "hello",
            "request_id": rid,
        }
    if op == "status":
        return _handle_status(rid)
    if op == "clear_model":
        return _handle_clear_model(rid)
    if op == "keep_model":
        return _handle_keep_model(rid)
    if op == "more":
        return _handle_more(rid)
    if op == "stop":
        return _handle_stop(rid, **kwargs)
    if op == "login":
        return _handle_login(rid)
    if op == "aiweb":
        return _handle_chat("aiweb", rid, **kwargs)
    if op == "chat":
        return _handle_chat("chat", rid, **kwargs)
    if op == "new":
        return _handle_new(rid, **kwargs)
    if op == "write":
        return _handle_write(rid, **kwargs)
    if op == "run":
        return _handle_run(rid, **kwargs)
    if op == "load":
        return _handle_load(rid, **kwargs)
    if op == "reset_memory":
        return _handle_reset_memory(rid)
    if op == "summary":
        return _handle_summary(rid, **kwargs)

    return _base_result(
        ok=False,
        message=f"Unknown op: {op}",
        request_id=rid,
        op=op,
        error_code="invalid_args",
        error=f"unknown op {op}",
    )


__all__ = ["handle", "PROTOCOL_VERSION", "model_pack_cap", "HEAVY_OPS", "CONTROL_OPS"]