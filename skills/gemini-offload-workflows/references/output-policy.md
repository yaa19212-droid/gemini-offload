# Output Policy

`structuredContent` is the authoritative result object. `content[0].text` is a
short receipt or read guide and is not a serialized copy of `structuredContent`.

## Inline Limits

- The shared inline limit is 4096 UTF-8 bytes.
- `output.path` is manual spill. When provided, the full output is written to
  disk even if it is short.
- Without `output.path`, responses above the limit are saved under
  `GEMINI_OFFLOAD_OUTPUT_DIR` when set, otherwise under the OS temp directory.
- Auto-saved filenames use a timestamp plus a short UUID.
- Background runs always write final bodies to durable paths and return only
  run receipts/status paths from the start call.
- If background `output.path` is omitted, the server creates a managed artifact
  under `<run_dir>/outputs/` using an index-derived storage key. Caller item IDs
  remain metadata and never determine managed filenames.
- An explicit absolute `output.path` remains supported as caller-selected output;
  it is recorded as unmanaged rather than being confused with the managed
  `outputs/` namespace.

## Text Responses

Plain text is the default response mode. Prefer it for OCR, transcription,
cleanup, and unusual multimodal inputs where Gemini may need to adapt its
format or explain uncertainty.

- Short text without `output.path` returns `text`.
- Long text without `output.path` returns `text_preview`, `output_path`,
  `byte_count`, `line_count`, `truncated`, and `read_guidance`.
- Text with `output.path` returns the same compact file-backed fields, even
  when the text is short.

Use `read_guidance` as a warning against loading whole files into context. It
includes byte and line counts and recommends targeted reads first.

## JSON Schema Responses

JSON schema is an opt-in strict mode for structured extraction and downstream
automation. It is not the default OCR or transcription mode.

When `output.mode` is `json_schema` and `output.json_schema` or
`output.json_schema_path` is present:

- Valid short JSON without `output.path` returns parsed `response_json`.
- Valid long JSON returns `response_json_preview`, `output_path`, counts, and
  `read_guidance`.
- Valid JSON with `output.path` is file-backed even when short.
- Invalid JSON returns `response_json_error` and then follows the text response
  policy for the raw Gemini text.

For budget decisions, the server uses the raw Gemini text byte count, not a
pretty-printed or compact reserialization that might hide whitespace.

## Image Sidecars

If Gemini returns image parts and `output.path` is set, image files are written
as siblings next to the text file. The MCP result includes image metadata and
paths, not image bytes.

## Background Integrity And Resume

The SQLite run store records artifact path, byte count, SHA-256 digest, role, and
whether the path is managed. A background item marked `completed` is skipped on
resume only after every recorded artifact passes this integrity check. Missing,
non-regular, escaped managed paths, size mismatches, or checksum mismatches cause
that item to become pending and execute again.

`status.json` and `events.jsonl` are compatibility/debug exports. Prefer
`manage_gemini_run status` and `progress`, which read the committed durable store.

## Agent Reading Pattern

1. Check receipt fields first: `ok`, `model`, `usage`, `elapsed_ms`, counts,
   previews, and paths.
2. If a preview or count looks suspicious, inspect a targeted range from the
   saved file.
3. Read the full file only when full synthesis or final assembly requires it.
4. For background runs, use `manage_gemini_run progress` and appended event
   offsets instead of repeatedly reading the whole event log.
