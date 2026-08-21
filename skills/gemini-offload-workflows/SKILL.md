---
name: gemini-offload-workflows
description: Use when Codex needs to offload a bounded Gemini subtask through the local gemini-offload MCP server, especially OCR, transcription, flexible plain-text multimodal analysis, file-grounded extraction, remote file URI input, media resolution defaults or overrides, optional strict JSON schema output, Google Search grounding, text cleanup, background runs, or concurrent item processing. Prefer this skill when the main agent should keep context small, large outputs should be written to disk, source material should be sampled before a run, or reusable prompts/templates should be loaded from path.
---

# Gemini Offload Workflows

Use `gemini-offload` as a bounded Gemini worker. Keep planning, chunking,
validation, and synthesis in Codex. Let Gemini handle the heavy multimodal or
grounded call, then inspect only receipts, status fields, and targeted files.

## Quick Start

1. Use `call_gemini` for the Gemini run.
2. Use `list_gemini_models` or `detect_mime` only when model choice or file
   support is uncertain.
3. Use `manage_gemini_run` after starting a background run.
4. Use absolute paths for file, text, schema, template, output, and run paths.
5. Default to plain text output. Use JSON schema only when exact fields matter.
6. Use one `call_gemini` item for a bounded artifact, or multiple items with
   `max_concurrency` for independent artifacts.
7. Use `execution.lifecycle: "background"` for long OCR/transcription runs.
8. Treat `structuredContent` as authoritative. `content[0].text` is only a
   short receipt or read guide.

## Request Model

`call_gemini` takes ordered Gemini-style `contents[]`, not `prompt/files/history`
top-level fields.

- `system.text` or `system.path`: optional system instruction.
- `contents[]`: ordered user/model turns.
- `parts[]`: exactly one of `text`, `text_path`, `file_path`, or
  `file_uri`.
- `file_uri` parts must include `mime_type`.
- Media resolution is automatic by default: images use `ultra_high`; PDFs and
  videos use `high`; audio gets no media resolution option.
- Use `request.media_resolution` only to change image/PDF/video defaults, and
  `parts[].media_resolution` only for a specific file part.
- Valid media resolution values are `low`, `medium`, `high`, `ultra_high`, and
  `off`. `ultra_high` is image-only. Do not set media resolution on text,
  text_path, or audio parts.
- `output.mode`: `text` by default, or `json_schema`.
- `output.path`: file-backed result path; background auto-generates one if
  omitted.
- `tools.google_search`: enable only when web grounding is part of the task.

Use template input for repeated OCR/chunk runs:

```json
{
  "template_path": "D:/work/templates/ocr-request.json",
  "items": [
    {"id": "page-001", "vars": {"chunk_path": "D:/work/chunks/page-001.pdf", "page": 1}}
  ],
  "execution": {"lifecycle": "background", "max_concurrency": 5}
}
```

## Response Mode Choice

Plain text and JSON schema are both first-class output modes.

- Use plain text by default for OCR, transcription, cleanup, document reading,
  uncertain visual layouts, and exploratory multimodal analysis.
- Use JSON schema for strict extraction, repeatable rows, downstream automation,
  or cases where Codex must validate exact fields.
- Do not wrap irregular OCR in JSON just to make it look structured.

## Output Handling

- Blocking short text may return `results[].text`.
- Large or explicit-path outputs return previews, `output_path`, counts, and
  `read_guidance`.
- JSON schema success returns `response_json` when short, or
  `response_json_preview` plus `output_path` when file-backed.
- Multi-item blocking runs have an aggregate inline budget and may return
  `results_path`.
- Background starts return `run_id`, `run_dir`, `status_path`, `events_path`,
  and guidance. Use `manage_gemini_run` for progress.
- Omitted background `output.path` creates a managed file under
  `<run_dir>/outputs/` using an index-derived storage key; caller item IDs stay
  metadata and are not used as filenames.
- Explicit absolute `output.path` remains supported as a caller-selected output
  and is not treated as a managed run artifact path.

## Read References When Needed

- Read `references/schema-and-grounding.md` for JSON schema or Google Search
  grounding.
- Read `references/batch-workflows.md` for multi-item, template, or background
  runs.
- Read `references/output-policy.md` when interpreting spills, previews,
  manifests, or `read_guidance`.

## Background Runs

Use background lifecycle when waiting would block Codex for a long time:

1. Start `call_gemini` with `execution.lifecycle: "background"`.
2. Store the returned `run_id` and paths.
3. Use `manage_gemini_run` with `status` or `progress`.
4. Read only targeted output files or appended event ranges.
5. Use `stop` to pause after current work, `cancel` to terminate intent, and
   `resume` when a partial run can continue.

Background run state is authoritative in the SQLite run store. Use
`manage_gemini_run` for status/progress/control; it reads committed durable state
and may supplement it with verified OS-process liveness. Per-run `status.json`
and `events.jsonl` are compatibility/debug exports, not source of truth.

## Common Call Shapes

OCR one chunk, blocking:

```json
{
  "items": [{
    "id": "chunk-01",
    "request": {
      "model": "gemini-3.1-pro-preview",
      "system": {"path": "D:/path/to/gemini-offload/prompts/ocr_system.md"},
      "contents": [{"role": "user", "parts": [
        {"file_path": "D:/work/input/chunk-01.pdf"},
        {"text": "Convert this chunk to clean markdown."}
      ]}],
      "output": {"mode": "text", "path": "D:/work/out/chunk-01.md"}
    }
  }],
  "execution": {"lifecycle": "blocking"}
}
```

Structured extraction:

```json
{
  "items": [{
    "id": "invoice-01",
    "request": {
      "contents": [{"role": "user", "parts": [
        {"file_path": "D:/work/input/invoice-01.png"},
        {"text": "Extract the requested fields."}
      ]}],
      "output": {
        "mode": "json_schema",
        "json_schema_path": "D:/work/schemas/invoice.schema.json",
        "path": "D:/work/out/invoice-01.json"
      }
    }
  }]
}
```

Grounded short answer:

```json
{
  "items": [{
    "id": "release-date",
    "request": {
      "contents": [{"role": "user", "parts": [
        {"text": "Find the latest official release date and answer concisely."}
      ]}],
      "tools": {"google_search": true}
    }
  }]
}
```

## Failure Modes

- Relative paths are rejected.
- Invalid template placeholders or unused vars are rejected.
- JSON schema parse failures return `response_json_error` plus text fallback
  fields.
- Background worker liveness can be `unknown`; use `manage_gemini_run status`
  before assuming a run is still active.
- Resume trusts a completed item only after recorded output artifacts pass size
  and SHA-256 verification. Missing or tampered outputs are re-executed rather
  than silently skipped.
- Do not read full output files, manifests, or event logs unless required.

## Minimal Checklist

- Sample first for large sources.
- Use one bounded artifact per item.
- Prefer path-backed system prompts, templates, schemas, and outputs.
- Use plain text unless strict structure is needed.
- For background runs, follow `read_guidance` and inspect only targeted files or
  appended event ranges.
