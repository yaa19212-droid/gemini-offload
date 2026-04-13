# gemini-offload-mcp

A thin stdio MCP server that wraps Google's Gemini API so an orchestrator
agent (Claude Code, Codex, etc.) can offload individual subtasks — OCR,
speech-to-text, text correction, or any `generate_content` call — without
bloating its own context.

The server is **stateless**. All batching, splitting, retry policy, and
result aggregation stay with the client.

## Install

```bash
pip install -e .
# or, without cloning:
pip install mcp google-genai httpx
```

## API keys

Resolved in this order:

1. File path in `GEMINI_OFFLOAD_KEYS` env var — JSON `{ name: key }` map.
2. `./api_keys.json` in the server's current working directory.
3. Env vars `GEMINI_API_KEY` and/or `GOOGLE_API_KEY`.

Example `api_keys.json` (multiple keys are round-robin rotated):

```json
{
  "GOOGLE_API_KEY1": "your-api-key",
  "GOOGLE_API_KEY2": "your-api-key"
}
```

## Run

```bash
python -m mcp_server
# or, after install:
gemini-offload-mcp
```

## Register with Claude Code

```bash
claude mcp add gemini-offload --scope user -- python -m mcp_server
```

`.mcp.json` example:

```json
{
  "mcpServers": {
    "gemini-offload": {
      "command": "python",
      "args": ["-m", "mcp_server"],
      "env": { "GEMINI_OFFLOAD_KEYS": "C:/secrets/gemini_keys.json" }
    }
  }
}
```

## Tools

### `gemini_generate`

Upload local absolute-path files and run `generate_content`.

| param | type | notes |
|---|---|---|
| `prompt` | string | required |
| `files` | string[] | optional, absolute paths |
| `system_prompt` | string | optional |
| `model` | string | default `gemini-2.5-flash` |
| `include_thinking` | bool | default `false` |
| `history` | `[{role, text}]` | optional few-shot turns |
| `output_path` | string | **recommended**, absolute path |

**Output policy:**
- With `output_path`: full UTF-8 text is written to disk, response returns
  `{output_path, byte_count, char_count, text_preview (100 chars), truncated, model, usage, elapsed_ms}`.
- Without `output_path`: response returns `{text_preview (300 chars), char_count, truncated, ...}`. The full text is **not recoverable** from the inline response — use `output_path` for anything non-trivial.

### `list_gemini_models`

Returns the supported model list. No input.

### `detect_mime`

Input: `{path: string}` (absolute). Returns `{mime, supported}`.

## Supported file types

PDF, plain text / markdown / CSV, PNG/JPEG/GIF/BMP/WEBP, MP3/WAV/FLAC/OGG/M4A.

## Orchestration pattern

The orchestrator agent splits a big job into N subtasks, picks distinct
`output_path` values for each, and calls `gemini_generate` in parallel.
Each call responds with a tiny preview payload. The orchestrator reads only
the files it actually needs afterward.
