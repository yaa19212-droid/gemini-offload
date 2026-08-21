# Gemini Offload Hook Helpers

This folder intentionally is not referenced from `plugin.json` because the
current local plugin validator rejects a top-level `hooks` field.

The helper scripts here are optional building blocks for future Codex hook
integration. Core correctness comes from the MCP tools:

- `call_gemini`
- `manage_gemini_run`

`gemini_run_status.py` reads the authoritative SQLite run store when it exists.
It does not treat per-run `status.json` or `events.jsonl` compatibility exports
as source of truth. Use `manage_gemini_run` for reliable status, progress, and
control; the helper only injects compact active-run context.
