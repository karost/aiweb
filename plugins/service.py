"""AI Web V3 — daemon-side op dispatcher.

All heavy work (browser, capture, pipeline, write) lives here.
Client only does IPC; this module never runs inside the TUI worker.
"""

from __future__ import annotations

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


def _get_engine() -> BrowserEngine:
    sess = get_session()
    if sess.engine is None or not getattr(sess.engine, "is_up", lambda: False)():
        headed = os.environ.get("HERMES_AIWEB_HEADED", "1").strip().lower() in {
            "1", "true", "yes", "on", ""
        }
        engine = BrowserEngine(headless=not headed)
        run_async(engine.start())
        sess.bind_engine(engine)
    return sess.engine  # type: ignore[return-value]


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
        "inject": inject or mem.inject_info_snapshot(
            written=False, pending=False, chars=0, mode="none"
        ),
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
        f"Inject pending: {st.get('inject_pending')} (sticky={mem.is_model_sticky()})",
        f"Model pack cap: {mem.model_pack_cap()} chars",
        f"Response default size: {default_response_size()} chars",
        f"Profile: `{PROFILE_DIR}`",
        f"Out dir: `{mem.get_out_dir()}`",
        f"Last gen: `{st.get('last_gen_id') or '—'}`",
        f"",
        mem.load_hot_context(max_chars=1500),
        f"",
        "_Same-chat by default. Only `/aiweb-new` starts a fresh conversation._",
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
    if not sess.try_begin_heavy():
        return _base_result(
            ok=False,
            message="AI Web busy.",
            request_id=request_id,
            op="login",
            error_code="busy",
            error="busy",
        )
    try:
        engine = _get_engine()
        # Force headed for login
        cap: CaptureResult = run_async(engine.login_interactive(timeout_sec=600))
        if cap.ok:
            sess.mark_ready(page_url=cap.page_url, conversation_url=cap.conversation_url)
            sess.end_heavy(ok=True)
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
        sess.end_heavy(ok=False, error=cap.error)
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
        sess.end_heavy(ok=False, error=str(e))
        return _base_result(
            ok=False,
            message=f"login error: {e}",
            request_id=request_id,
            op="login",
            error=str(e),
            error_code="internal",
        )


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

    do_inject = op == "aiweb"
    sess = get_session()
    if not sess.try_begin_heavy():
        return _base_result(
            ok=False,
            message="AI Web busy.",
            request_id=request_id,
            op=op,
            error_code="busy",
            error="busy",
        )

    try:
        engine = _get_engine()
        cap: CaptureResult = run_async(
            engine.submit_and_capture(message, request_id=request_id, op=op, force_new=False)
        )

        if cap.needs_login:
            sess.mark_needs_login(page_url=cap.page_url)
            sess.end_heavy(ok=False, error=cap.error)
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
            sess.end_heavy(ok=False, error=cap.error)
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

        meta = format_chat_meta(
            path=pipe.path,
            chars=pipe.chars,
            full_path=pipe.file_path,
            more_available=pipe.path == "large" and pipe.total_chunks > 1,
            chunk_index=1 if pipe.path == "large" else 0,
            chunk_total=pipe.total_chunks,
            inject_note=inject_note if do_inject else "no inject",
        )
        body = format_chat_piece(pipe, 0)
        msg = f"{body}\n\n_({meta})_"

        mem.append_hot_summary(f"[{op}] {message[:80]} → {pipe.path} {pipe.chars}c")
        sess.end_heavy(ok=True)

        return _base_result(
            ok=True,
            message=msg,
            request_id=request_id,
            op=op,
            path=pipe.path,
            chars=pipe.chars,
            full_path=pipe.file_path,
            gen_id=gen_id,
            more_available=pipe.path == "large" and pipe.total_chunks > 1,
            chunk_index=1 if pipe.path == "large" else 0,
            chunk_total=pipe.total_chunks,
            inject=inject_meta,
        )
    except Exception as e:
        sess.end_heavy(ok=False, error=str(e))
        return _base_result(
            ok=False,
            message=f"internal error: {e}",
            request_id=request_id,
            op=op,
            error=str(e),
            error_code="internal",
        )


def _handle_new(request_id: str, **kwargs: Any) -> dict[str, Any]:
    """Only command that starts a fresh Grok conversation."""
    extra = (kwargs.get("message") or "").strip()
    sess = get_session()
    if not sess.try_begin_heavy():
        return _base_result(
            ok=False,
            message="AI Web busy.",
            request_id=request_id,
            op="new",
            error_code="busy",
            error="busy",
        )
    try:
        engine = _get_engine()
        cap = run_async(engine.start_new_conversation())
        if not cap.ok:
            sess.end_heavy(ok=False, error=cap.error)
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
        sess.end_heavy(ok=True)

        note = "Fresh Grok conversation started."
        if extra:
            # Optional first message into the new chat
            cap2 = run_async(
                engine.submit_and_capture(extra, request_id=request_id, op="new", force_new=False)
            )
            if cap2.ok:
                pipe = process_response(cap2.text, inject_model=False)
                body = format_chat_piece(pipe, 0)
                note = f"Fresh conversation + first reply:\n\n{body}"
                sess.mark_ready(page_url=cap2.page_url, conversation_url=cap2.conversation_url)

        return _base_result(
            ok=True,
            message=note + f"\n\n_Page: `{cap.page_url}`_",
            request_id=request_id,
            op="new",
        )
    except Exception as e:
        sess.end_heavy(ok=False, error=str(e))
        return _base_result(
            ok=False,
            message=f"new chat error: {e}",
            request_id=request_id,
            op="new",
            error=str(e),
            error_code="internal",
        )


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
    if not sess.try_begin_heavy():
        return _base_result(
            ok=False,
            message="AI Web busy.",
            request_id=request_id,
            op="write",
            error_code="busy",
            error="busy",
        )

    lang = language_from_path(resolved.target)
    system_hint = (
        f"Reply with a single fenced code block"
        f"{f' in {lang}' if lang else ''} containing only the file content. "
        f"No outer explanation before the fence."
    )
    full_prompt = f"{system_hint}\n\n{prompt}"

    try:
        engine = _get_engine()
        cap: CaptureResult = run_async(
            engine.submit_and_capture(full_prompt, request_id=request_id, op="write", force_new=False)
        )
        if not cap.ok:
            sess.end_heavy(ok=False, error=cap.error)
            code = cap.error_code or "empty_extract"
            if cap.needs_login:
                sess.mark_needs_login(page_url=cap.page_url)
                code = "needs_login"
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
            sess.end_heavy(ok=False, error=reason)
            arts = capture_text_failure(
                op="write",
                request_id=request_id,
                note=reason or "extract failed",
                body=pipe.full_text,
            )
            return _base_result(
                ok=False,
                message=f"Write extract failed: {reason}. See raw last_response.md."
                + format_artifacts_suffix(arts),
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
        sess.end_heavy(ok=True)
        mem.append_hot_summary(f"[write] {resolved.target.name} ({nbytes}b)")

        return _base_result(
            ok=True,
            message=(
                f"✅ Wrote `{resolved.target}` ({nbytes} bytes)\n"
                f"```\n{preview}\n```\n"
                f"_(no full-body inject · raw: {pipe.file_path or LAST_RESPONSE_FILE})_"
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
        sess.end_heavy(ok=False, error=str(e))
        return _base_result(
            ok=False,
            message=f"write error: {e}",
            request_id=request_id,
            op="write",
            error=str(e),
            error_code="internal",
        )


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
    # No local exec in V3 — just ask Grok for code (same as /aiweb path)
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

    # Concurrency gate
    sess = get_session()
    can, reason = sess.can_run(op)
    if not can:
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


__all__ = ["handle", "PROTOCOL_VERSION", "model_pack_cap"]