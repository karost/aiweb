"""AI Web — service.handle(op) orchestration (daemon in-process).

No IPC here. Client/daemon call handle(); tests call handle() with a fake engine.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Optional

from . import memory_manager as mem
from .artifacts import capture_text_failure, format_artifacts_suffix
from .browser_engine import BrowserEngine, CaptureResult, run_async
from .distill import final_only
from .response_pipeline import format_chat_meta, next_more, process_response
from .session import HEAVY_OPS, get_session
from .write_policy import (
    atomic_write,
    extract_primary_code,
    language_from_path,
    looks_like_chrome,
    resolve_out_path,
    WriteResolveErr,
)


PROTOCOL_VERSION = 1


def model_pack_cap() -> int:
    try:
        return max(256, int(os.environ.get("HERMES_AIWEB_MODEL_PACK", "6000")))
    except ValueError:
        return 6000


def write_inject_mode() -> str:
    v = (os.environ.get("HERMES_AIWEB_WRITE_INJECT") or "none").strip().lower()
    return v if v in ("none", "path_only") else "none"


def _empty_inject() -> dict:
    return mem.inject_info_snapshot(
        written=False,
        pending=False,
        chars=0,
        mode="none",
        distill_method=None,
        capped=False,
        cap=None,
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
    path: str = "none",
    chars: int = 0,
    full_path: Optional[str] = None,
    gen_id: Optional[str] = None,
    more_available: bool = False,
    chunk_index: Optional[int] = None,
    chunk_total: Optional[int] = None,
    inject: Optional[dict] = None,
    file_path: Optional[str] = None,
    file_bytes: Optional[int] = None,
) -> dict[str, Any]:
    sess = get_session()
    pub = sess.status.to_public_dict()
    return {
        "ok": ok,
        "message": message,
        "request_id": request_id,
        "session_alive": pub["session_alive"],
        "state": pub["state"],
        "busy": pub["busy"],
        "daemon_up": pub["daemon_up"],
        "browser_up": pub["browser_up"],
        "daemon_pid": pub["daemon_pid"],
        "protocol_version": PROTOCOL_VERSION,
        "page_url": pub["page_url"],
        "login_required": pub["login_required"],
        "path": path,
        "chars": chars,
        "full_path": full_path,
        "gen_id": gen_id,
        "more_available": more_available,
        "chunk_index": chunk_index,
        "chunk_total": chunk_total,
        "inject": inject or _empty_inject(),
        "file_path": file_path,
        "file_bytes": file_bytes,
        "error": error,
        "error_code": error_code,
        "artifacts": artifacts or [],
        "op": op,
    }


def _get_engine() -> BrowserEngine:
    sess = get_session()
    if sess.engine is None:
        sess.bind_engine(BrowserEngine())
    return sess.engine


def handle(op: str, **kwargs: Any) -> dict[str, Any]:
    """Single entry for all ops."""
    request_id = str(kwargs.pop("request_id", None) or uuid.uuid4())
    op = (op or "").strip().lower()

    if op == "hello":
        return _base_result(
            ok=True,
            message="hello",
            request_id=request_id,
            op=op,
        )

    sess = get_session()
    allowed, why = sess.can_run(op)
    if not allowed:
        return _base_result(
            ok=False,
            message="AI Web busy with another operation. Use /aiweb-status.",
            request_id=request_id,
            op=op,
            error="heavy operation in progress",
            error_code="busy",
        )

    if op == "status":
        return _handle_status(request_id)
    if op == "more":
        return _handle_more(request_id)
    if op == "clear_model":
        return _handle_clear_model(request_id)
    if op == "keep_model":
        return _handle_keep_model(request_id)
    if op == "stop":
        return _handle_stop(request_id, **kwargs)
    if op in ("aiweb", "chat"):
        return _handle_chat(op, request_id, **kwargs)
    if op == "write":
        return _handle_write(request_id, **kwargs)
    if op == "login":
        return _handle_login(request_id, **kwargs)

    return _base_result(
        ok=False,
        message=f"Unknown op: {op}",
        request_id=request_id,
        op=op,
        error=f"unknown op {op}",
        error_code="invalid_args",
    )


def _handle_status(request_id: str) -> dict[str, Any]:
    sess = get_session()
    pub = sess.status.to_public_dict()
    pending = mem.inject_pending()
    lines = [
        f"state={pub['state']}",
        f"session_alive={pub['session_alive']}",
        f"browser_up={pub['browser_up']}",
        f"busy={pub['busy']}",
        f"login_required={pub['login_required']}",
        f"inject_pending={pending}",
        f"last_gen_id={pub.get('last_gen_id')}",
        f"pid={pub.get('daemon_pid')}",
        f"url={pub.get('page_url') or '-'}",
    ]
    return _base_result(
        ok=True,
        message="\n".join(lines),
        request_id=request_id,
        op="status",
        inject=mem.inject_info_snapshot(
            written=False,
            pending=pending,
            chars=0,
            mode="none",
        ),
    )


def _handle_more(request_id: str) -> dict[str, Any]:
    ok, msg, meta = next_more()
    return _base_result(
        ok=ok,
        message=msg,
        request_id=request_id,
        op="more",
        path=meta.get("path", "large" if ok else "none"),
        chars=int(meta.get("chars") or 0),
        full_path=meta.get("full_path"),
        gen_id=meta.get("gen_id"),
        more_available=bool(meta.get("more_available")),
        chunk_index=meta.get("chunk_index"),
        chunk_total=meta.get("chunk_total"),
        error=None if ok else msg,
        error_code=None if ok else "invalid_args",
    )


def _handle_clear_model(request_id: str) -> dict[str, Any]:
    mem.clear_model_context()
    mem.set_sticky(False)
    return _base_result(
        ok=True,
        message="Model inject buffer cleared.",
        request_id=request_id,
        op="clear_model",
    )


def _handle_keep_model(request_id: str) -> dict[str, Any]:
    mem.set_sticky(True)
    return _base_result(
        ok=True,
        message="Model inject set sticky until /aiweb-clear-model (still capped).",
        request_id=request_id,
        op="keep_model",
        inject=mem.inject_info_snapshot(
            written=mem.model_context_path().exists(),
            pending=mem.inject_pending(),
            chars=0,
            mode="final_only",
        ),
    )


def _handle_stop(request_id: str, **kwargs: Any) -> dict[str, Any]:
    stop_daemon = bool(kwargs.get("daemon") or kwargs.get("stop_daemon"))
    sess = get_session()
    engine = sess.engine
    if engine is not None:
        try:
            run_async(engine.stop())
        except Exception:
            pass
    sess.mark_browser_down()
    msg = "Browser session closed."
    if stop_daemon:
        msg += " Daemon will exit after response."
        # daemon.py watches this via kwargs passthrough / env flag if needed
    return _base_result(
        ok=True,
        message=msg,
        request_id=request_id,
        op="stop",
    )


def _handle_login(request_id: str, **kwargs: Any) -> dict[str, Any]:
    sess = get_session()
    if not sess.try_begin_heavy():
        return _base_result(
            ok=False,
            message="busy",
            request_id=request_id,
            op="login",
            error_code="busy",
            error="busy",
        )
    try:
        engine = _get_engine()
        cap: CaptureResult = run_async(engine.login_interactive())
        if cap.needs_login or not cap.ok:
            sess.mark_needs_login(page_url=cap.page_url)
            sess.end_heavy(ok=False, error=cap.error)
            return _base_result(
                ok=False,
                message=f"Login incomplete: {cap.error}"
                + format_artifacts_suffix(cap.artifacts or []),
                request_id=request_id,
                op="login",
                error=cap.error,
                error_code=cap.error_code or "needs_login",
                artifacts=cap.artifacts or [],
            )
        sess.mark_ready(page_url=cap.page_url)
        sess.end_heavy(ok=True)
        return _base_result(
            ok=True,
            message="Login OK — session ready.",
            request_id=request_id,
            op="login",
        )
    except Exception as e:  # noqa: BLE001
        sess.end_heavy(ok=False, error=str(e))
        return _base_result(
            ok=False,
            message=f"login failed: {e}",
            request_id=request_id,
            op="login",
            error=str(e),
            error_code="browser_dead",
        )


def _handle_chat(op: str, request_id: str, **kwargs: Any) -> dict[str, Any]:
    message = (kwargs.get("message") or kwargs.get("prompt") or "").strip()
    if not message:
        return _base_result(
            ok=False,
            message="Empty message.",
            request_id=request_id,
            op=op,
            error="empty message",
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
            engine.submit_and_capture(message, request_id=request_id, op=op)
        )
        if cap.needs_login:
            sess.mark_needs_login(page_url=cap.page_url)
            sess.end_heavy(ok=False, error=cap.error)
            return _base_result(
                ok=False,
                message="Login required. Use /aiweb-login."
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

        pipe = process_response(cap.text)
        sess.set_last_gen(pipe.gen_id)
        sess.mark_ready(page_url=cap.page_url)

        inject_meta = _empty_inject()
        inject_note = "no inject"
        if do_inject:
            dist = final_only(pipe.text, cap=model_pack_cap(), hint="prose")
            if dist.payload:
                mem.write_model_context(dist.payload)
                inject_meta = mem.inject_info_snapshot(
                    written=True,
                    pending=True,
                    chars=len(dist.payload),
                    mode="final_only",
                    distill_method=dist.method,
                    capped=dist.capped,
                    cap=model_pack_cap(),
                )
                inject_note = (
                    f"inject pending {len(dist.payload)} chars · "
                    f"final_only/{dist.method}"
                    + (" · capped" if dist.capped else "")
                )
            else:
                mem.clear_model_context()
                inject_meta = mem.inject_info_snapshot(
                    written=False,
                    pending=False,
                    chars=0,
                    mode="final_only",
                    distill_method="empty",
                    capped=False,
                    cap=model_pack_cap(),
                )
                inject_note = "distill empty · no inject"

        meta = format_chat_meta(
            path=pipe.path,
            chars=pipe.chars,
            full_path=pipe.full_path,
            more_available=pipe.more_available,
            chunk_index=pipe.chunk_index,
            chunk_total=pipe.chunk_total,
            inject_note=inject_note if do_inject else "no inject",
        )
        body = pipe.chat_piece
        if len(body) > 50:
            msg = f"{body}\n\n_({meta})_"
        else:
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
            full_path=pipe.full_path,
            gen_id=pipe.gen_id,
            more_available=pipe.more_available,
            chunk_index=pipe.chunk_index,
            chunk_total=pipe.chunk_total,
            inject=inject_meta,
        )
    except Exception as e:  # noqa: BLE001
        sess.end_heavy(ok=False, error=str(e))
        return _base_result(
            ok=False,
            message=f"internal error: {e}",
            request_id=request_id,
            op=op,
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
        f"End with a short ## Final note if needed."
    )
    full_prompt = f"{system_hint}\n\n{prompt}"

    try:
        engine = _get_engine()
        cap: CaptureResult = run_async(
            engine.submit_and_capture(full_prompt, request_id=request_id, op="write")
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

        pipe = process_response(cap.text)
        sess.set_last_gen(pipe.gen_id)

        body, reason = extract_primary_code(pipe.text, language=lang)
        if not body or looks_like_chrome(body):
            sess.mark_ready(page_url=cap.page_url)
            sess.end_heavy(ok=False, error=reason)
            arts = capture_text_failure(
                op="write",
                request_id=request_id,
                note=reason or "extract failed",
                body=pipe.text,
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
                full_path=pipe.full_path,
                gen_id=pipe.gen_id,
            )

        nbytes = atomic_write(resolved.target, body)
        inject_meta = _empty_inject()
        if write_inject_mode() == "path_only":
            mem.write_path_only_inject(str(resolved.target))
            inject_meta = mem.inject_info_snapshot(
                written=True,
                pending=True,
                chars=len(str(resolved.target)) + 40,
                mode="path_only",
                distill_method="path_only",
                capped=False,
                cap=None,
            )

        preview = "\n".join(body.splitlines()[:12])
        if len(body.splitlines()) > 12:
            preview += "\n…"

        sess.mark_ready(page_url=cap.page_url)
        sess.end_heavy(ok=True)
        return _base_result(
            ok=True,
            message=(
                f"Wrote {resolved.target} ({nbytes} bytes)\n"
                f"```\n{preview}\n```\n"
                f"_(no full-body inject · raw: {pipe.full_path})_"
            ),
            request_id=request_id,
            op="write",
            path=pipe.path,
            chars=pipe.chars,
            full_path=pipe.full_path,
            gen_id=pipe.gen_id,
            file_path=str(resolved.target),
            file_bytes=nbytes,
            inject=inject_meta,
        )
    except Exception as e:  # noqa: BLE001
        sess.end_heavy(ok=False, error=str(e))
        return _base_result(
            ok=False,
            message=f"write error: {e}",
            request_id=request_id,
            op="write",
            error=str(e),
            error_code="internal",
        )


__all__ = ["handle", "PROTOCOL_VERSION", "model_pack_cap"]