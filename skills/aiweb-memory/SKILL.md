---
name: aiweb-memory
description: >
  Use when the user runs AI Web (/aiweb*) or when Grok research should
  inform Hermes without dumping full research into the model context.
  V3: daemon + same-chat continuity + V1-style anti-block browser.
version: 3.0.0
metadata:
  hermes:
    tags: [aiweb, grok, browser, daemon, memory, model-context, same-chat]
---

# AI Web Memory — Skill (V3)

Grok is driven by the **aiweb plugin daemon**, not by the TUI slash worker
and not by Playwright inside the LLM.

## Architecture (V3)

- Slash handlers only call `client.request` over `$HERMES_HOME/data/aiweb/daemon.sock`.
- Playwright lives in `python -m aiweb.daemon` (one long-lived process for CLI and TUI).
- Browser lifetime is V1-style: one warm headed persistent context, minimal restarts.
- **Same-chat by default.** The engine never navigates to root after the first open.
- Only `/aiweb-new` starts a fresh Grok conversation.
- Capture is simple V1-style: fill + network sniff + text-diff + Stop button + stall.
- Inject is final-only, capped, applied on the **next** agent LLM call (`pre_llm_call`).
- Safe `/aiweb-write` jails all files under the out dir.

Personal use. Automating x.com / Grok may violate ToS and can risk the account.
Do not share `browser_profile/`.

## Commands

| Command | Effect | Model inject |
|---------|--------|--------------|
| `/aiweb <prompt>` | Grok research in **current** conversation | Final-only, capped, **next** Hermes turn |
| `/aiweb-chat <prompt>` | Same research, no inject | None |
| `/aiweb-new [optional first message]` | **Only** command that starts a fresh Grok chat | None (unless you then use `/aiweb`) |
| `/aiweb-write <path> <prompt>` | Extract code → `$HERMES_HOME/data/aiweb/out` | None or path-only |
| `/aiweb-more` | Next chunk of last **large** answer | None |
| `/aiweb-login` | Headed login; session stays in daemon | None |
| `/aiweb-stop` | Close browser (`daemon` also stops daemon) | None |
| `/aiweb-status` | Daemon / browser / conversation / inject_pending | — |
| `/aiweb-clear-model` | Clear inject buffer + sticky | — |
| `/aiweb-keep-model` | Sticky inject until clear (still capped) | — |
| `/aiweb-run <prompt>` | Ask Grok for Python only (no local exec) | Same as `/aiweb` |
| `/aiweb-load [extra]` | Send hot memory into the **CURRENT** conversation | Same as `/aiweb` |
| `/aiweb-reset` | Reset hot/archive + inject | — |
| `/aiweb-summary [text]` | Append a short memory line | — |

## Same-chat contract (critical)

1. First successful open → remember conversation URL.
2. Every subsequent `/aiweb` / `/aiweb-chat` / `/aiweb-write` / `/aiweb-load` → **stay on current page**, type into the existing composer.
3. `/aiweb-load` injects memory into the current conversation (no “New chat” click).
4. `/aiweb-new` is the **only** place that clicks “New chat” or navigates to root.
5. On rare browser restart → prefer restoring last known conversation URL; fall back to root only if that fails.

## Inject rules

1. Only `/aiweb` (and `run`/`load`, which share the same path) prepare inject.
2. Inject is **not** applied inside the slash command.
3. Inject applies on the **next** agent LLM call via `pre_llm_call`.
4. Payload is wrapped as untrusted data, not system instructions.
5. Prefer `/aiweb-chat` when Hermes context should stay clean.
6. Default pack cap: `HERMES_AIWEB_MODEL_PACK` (6000). Sticky re-injects until cleared.

## One daemon

Use the same daemon for `hermes --cli` and `hermes --tui`.
If `/aiweb-status` pid differs between CLI and TUI, stop extra processes
and use one profile lock only.

```bash
# Recommended environment
export HERMES_AIWEB_HEADED=1
export HERMES_AIWEB_KEEP_ALIVE=1