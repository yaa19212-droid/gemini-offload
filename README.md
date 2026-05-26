# gemini-offload-mcp

A thin stdio MCP server that wraps Vertex AI Gemini so an orchestrator
agent (Claude Code, Codex, etc.) can offload individual subtasks — OCR,
speech-to-text, text correction, or any `generate_content` call — without
bloating its own context.

The server is **stateless**. All batching, splitting, retry policy, and
result aggregation stay with the client.

## Install

```bash
pip install -e .
```

For personal Windows machines, use a repo-local checkout and run:

```powershell
.\install-local.ps1
```

The script installs the MCP server in editable mode, checks the expected
Vertex credential manifest path, and prints the Codex `config.toml` block for
that machine.

## Packaging model

This repository has two related packaging surfaces:

- `gemini-offload-mcp` is the Python package. Its wheel contains the
  `mcp_server` package and the `gemini-offload-mcp` console script.
- `plugins/gemini-offload` is the repo-local Codex plugin bundle. It contains
  the plugin manifest, MCP launcher script, icon, and workflow skill.

The Codex plugin expects a full repo checkout unless `GEMINI_OFFLOAD_REPO`
points to one. Do not copy only `plugins/gemini-offload` to a new machine and
expect it to run standalone; the launcher starts the server from the checkout.
Reusable prompt assets live under `prompts/`, so keep them with the checkout
when using the bundled workflow skill.

## Vertex AI credentials

Resolved in this order:

1. Manifest file path in `GEMINI_OFFLOAD_VERTEX_CREDENTIALS`.
2. Manifest file path in `VERTEX_AI_CREDENTIALS`.
3. `C:/Users/<user>/.secrets/vertex-ai/service-accounts/manifest.json`.

Recommended manifest:

```json
[
  {
    "project_id": "my-project",
    "client_email": "service-account@my-project.iam.gserviceaccount.com",
    "path": "./my-project-key.json"
  }
]
```

For easier machine-to-machine migration, put both the manifest and service
account JSON files under:

```text
C:/Users/<user>/.secrets/vertex-ai/service-accounts/
```

Relative `path` values are resolved from the manifest directory, so the same
manifest can be reused on another Windows machine after copying the whole
service-account folder into that user's home directory.

Set `GOOGLE_CLOUD_LOCATION` or `VERTEX_AI_LOCATION` to override the default
Vertex location (`global`). Rate limits are tracked per Vertex
project/location/model quota slot and round-robin rotated across configured
credentials.

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
      "env": {
        "GEMINI_OFFLOAD_VERTEX_CREDENTIALS": "C:/path/to/vertex-ai/service-accounts/manifest.json",
        "GOOGLE_CLOUD_LOCATION": "global"
      }
    }
  }
}
```

## Add to Codex

Add a local MCP entry to `~/.codex/config.toml`.

Minimal example:

```toml
[mcp_servers.gemini-offload]
command = "python"
args = ["-m", "mcp_server"]
startup_timeout_sec = 1800
tool_timeout_sec = 1800

[mcp_servers.gemini-offload.env]
GEMINI_OFFLOAD_VERTEX_CREDENTIALS = "C:/path/to/vertex-ai/service-accounts/manifest.json"
GOOGLE_CLOUD_LOCATION = "global"
```

Repo-local checkout example:

```toml
[mcp_servers.gemini-offload]
command = "powershell"
args = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "D:/path/to/gemini-offload/plugins/gemini-offload/scripts/start-gemini-offload.ps1"]
startup_timeout_sec = 1800
tool_timeout_sec = 1800

[mcp_servers.gemini-offload.env]
GEMINI_OFFLOAD_REPO = "D:/path/to/gemini-offload"
GEMINI_OFFLOAD_VERTEX_CREDENTIALS = "C:/path/to/vertex-ai/service-accounts/manifest.json"
GOOGLE_CLOUD_LOCATION = "global"
```

On a new personal Windows machine, prefer running `.\install-local.ps1` from
the checkout and then paste the printed block into `~/.codex/config.toml`.

Notes:

- Do not set `GEMINI_API_KEY`, `GOOGLE_API_KEY`, or `GEMINI_OFFLOAD_KEYS`; those are AI Studio routes.
- The server sends local files as inline Vertex parts and does not use the Gemini Files API.
- After updating `config.toml`, restart Codex or open a new session so the MCP server list is reloaded.

## Tools

### `gemini_generate`

Upload local absolute-path files and run `generate_content`.

| param | type | notes |
|---|---|---|
| `prompt` | string | required |
| `files` | string[] | optional, absolute paths |
| `system_prompt` | string | optional |
| `model` | string | default `gemini-3.1-pro-preview` |
| `include_thinking` | bool | default `false` |
| `history` | `[{role, text}]` | optional few-shot turns |
| `output_path` | string | **recommended**, absolute path |
| `rate_limit_mode` | string | `fail_fast` default, or `wait` |
| `fallback_models` | string[] | optional explicit fallback list |
| `rate_limit_max_wait_seconds` | number | wait-mode cap, default `120` |

**Output policy:**
- With `output_path`: full UTF-8 text is written to disk, response returns
  `{output_path, byte_count, char_count, text_preview (100 chars), truncated, model, usage, elapsed_ms}`.
- Without `output_path`: response returns `{text_preview (300 chars), char_count, truncated, ...}`. The full text is **not recoverable** from the inline response — use `output_path` for anything non-trivial.

### `list_gemini_models`

Returns the supported model list plus `model_characteristics`. No input.

Supported models:

| model | role |
|---|---|
| `gemini-3.1-pro-preview` | Overall best choice for complex OCR, long-context synthesis, multimodal reasoning, and difficult agentic or coding work. Use it when quality matters more than latency. |
| `gemini-3-flash-preview` | Emergency fallback when the primary path is unavailable or too slow. It keeps Gemini 3 reasoning and multimodal coverage with Flash latency. |
| `gemini-3.5-flash` | Fast default for throughput-sensitive jobs. It offers near-Pro agentic and coding capability at Flash speed, and can outperform 3.1 Pro in some narrower workloads. |

The server intentionally rejects `gemini-2.5*` model IDs and any model not listed above.

### `detect_mime`

Input: `{path: string}` (absolute). Returns `{mime, supported}`.

## Supported file types

PDF, plain text / markdown / CSV, PNG/JPEG/GIF/BMP/WEBP, MP3/WAV/FLAC/OGG/M4A.

## Orchestration pattern

The orchestrator agent splits a big job into N independent subtasks, picks
distinct `output_path` values for each, and calls `gemini_generate_batch`.
The batch tool runs jobs concurrently, defaults concurrency to the configured
Vertex credential count, and caps it at 32. Rate limits are tracked per
Vertex project/location/model quota slot. If a slot returns 429, the default
`fail_fast` mode returns a structured rate-limit result with retry and
fallback guidance; callers may opt into `rate_limit_mode: "wait"` or provide
explicit `fallback_models`.

### `gemini_generate_batch`

Run multiple independent `gemini_generate`-shaped jobs concurrently.

```json
{
  "max_concurrency": 4,
  "jobs": [
    {
      "id": "chunk-01",
      "prompt": "OCR this file to markdown.",
      "files": ["D:/work/in/chunk-01.pdf"],
      "output_path": "D:/work/out/chunk-01.md"
    },
    {
      "id": "chunk-02",
      "prompt": "OCR this file to markdown.",
      "files": ["D:/work/in/chunk-02.pdf"],
      "output_path": "D:/work/out/chunk-02.md"
    }
  ]
}
```

The result preserves input order and returns per-job `ok`, preview, usage,
timing, output path, and error fields.
