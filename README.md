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
- Set `GEMINI_OFFLOAD_OUTPUT_DIR` to an absolute path if you want automatic
  large-response spill files outside the OS temp directory.
- Set `GEMINI_OFFLOAD_RUN_DIR` to an absolute path if you want background run
  directories outside the OS temp directory.
- After updating `config.toml`, restart Codex or open a new session so the MCP server list is reloaded.

## Tools

### `call_gemini`

Run one or more Gemini request items. This is a breaking replacement for the
old `gemini_generate` and `gemini_generate_batch` tools.

Two input shapes are supported:

- explicit: each item provides a complete request envelope
- template: `template_path` contains one request envelope and each item provides
  placeholder vars

Request envelopes use ordered Gemini-style `contents[]`:

```json
{
  "items": [
    {
      "id": "chunk-01",
      "request": {
        "model": "gemini-3.1-pro-preview",
        "system": {"path": "D:/work/prompts/ocr.md"},
        "contents": [
          {"role": "user", "parts": [
            {"file_path": "D:/work/in/chunk-01.pdf"},
            {"text": "OCR this chunk to markdown."}
          ]}
        ],
        "output": {"mode": "text", "path": "D:/work/out/chunk-01.md"},
        "tools": {"google_search": false}
      }
    }
  ],
  "execution": {"lifecycle": "blocking", "max_concurrency": 1}
}
```

Background template run:

```json
{
  "template_path": "D:/work/templates/ocr-request.json",
  "items": [
    {"id": "chunk-01", "vars": {"chunk_path": "D:/work/in/chunk-01.pdf", "page": 1}},
    {"id": "chunk-02", "vars": {"chunk_path": "D:/work/in/chunk-02.pdf", "page": 2}}
  ],
  "execution": {"lifecycle": "background", "max_concurrency": 4}
}
```

| field | notes |
|---|---|
| `system.text` / `system.path` | optional system instruction, exactly one when present |
| `contents[].parts[].text` | inline text part |
| `contents[].parts[].text_path` | absolute path to UTF-8 text |
| `contents[].parts[].file_path` | absolute path to supported local file |
| `output.mode` | `text` default, or `json_schema` |
| `output.path` | explicit result path; background auto-generates one if omitted |
| `output.json_schema` / `output.json_schema_path` | required exactly one for `json_schema` mode |
| `tools.google_search` | default `false`; enables Google Search grounding |
| `rate_limit.mode` | `fail_fast` default, or `wait` |
| `rate_limit.fallback_models` | optional explicit fallback list |
| `rate_limit.max_wait_seconds` | wait-mode cap, default `120` |

**Output policy:**
- Short responses up to 4096 UTF-8 bytes return full inline `text`.
- Providing `output.path` forces file output even for short responses.
- Larger responses are written to `output_path` when provided, or to
  `GEMINI_OFFLOAD_OUTPUT_DIR` / OS temp when omitted. The inline response
  returns `text_preview`, `output_path`, `byte_count`, `line_count`, and
  `read_guidance`.
- With `output.json_schema` or `output.json_schema_path`, short valid JSON
  returns parsed `response_json`. Larger JSON returns `response_json_preview`
  plus `output_path`.
- With `tools.google_search: true`: response may include normalized `grounding`
  metadata with `queries`, `sources`, and `supports`. Google Search UI payloads
  such as `renderedContent` and `sdkBlob` are intentionally omitted.
- `execution.lifecycle: "background"` starts a child worker process and returns
  run paths immediately instead of final result bodies.
- `structuredContent` is the authoritative result. `content[0].text` is only a
  short receipt or read guide, not a serialized copy of `structuredContent`.

### `manage_gemini_run`

Inspect or control background runs.

```json
{
  "action": "status",
  "run_id": "run-20260605T010203000000Z-abcd1234"
}
```

Actions: `list`, `status`, `progress`, `stop`, `cancel`, `resume`.

Background run directories contain `plan.json`, `status.json`, `events.jsonl`,
`locator.json`, `control/`, and `outputs/`. The event log is append-only
debug/audit data; live state is checked from the worker process where possible.

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

The orchestrator agent samples large sources, writes reusable prompt/template
files, then calls `call_gemini` with either one item or many items. Multi-item
runs preserve input order and run concurrently up to `max_concurrency`.

Use blocking lifecycle for small or immediate tasks. Use background lifecycle
for long OCR/transcription runs; then poll with `manage_gemini_run` or inspect
only targeted output files/events.
