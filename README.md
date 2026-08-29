# AI Web (Hermes plugin)

Grok via real browser (Playwright/CDP) with a **session daemon** so `/aiweb*` works on Hermes **CLI and `--tui`**.

- Final-only model inject (next agent turn)
- Large answers → disk + `/aiweb-more`
- `/aiweb-write` → files under out dir

**Personal use at your own risk.** UI automation may violate platform ToS.

## Layout

```text
plugins/          → ~/.hermes/plugins/aiweb
skills/aiweb-memory/ → ~/.hermes/skills/aiweb-memory