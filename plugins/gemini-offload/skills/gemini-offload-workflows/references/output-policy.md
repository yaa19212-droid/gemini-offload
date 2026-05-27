# Output Policy

`structuredContent` is the authoritative result object. `content[0].text` is a
short receipt or read guide and is not a serialized copy of `structuredContent`.

## Inline Limits

- The shared inline limit is 4096 UTF-8 bytes.
- `output_path` is manual spill. When provided, the full output is written to
  disk even if it is short.
- Without `output_path`, responses above the limit are saved under
  `GEMINI_OFFLOAD_OUTPUT_DIR` when set, otherwise under the OS temp directory.
- Auto-saved filenames use a timestamp plus a short UUID.

## Text Responses

Plain text is the default response mode. Prefer it for OCR, transcription,
cleanup, and unusual multimodal inputs where Gemini may need to adapt its
format or explain uncertainty.

- Short text without `output_path` returns `text`.
- Long text without `output_path` returns `text_preview`, `output_path`,
  `byte_count`, `line_count`, `truncated`, and `read_guidance`.
- Text with `output_path` returns the same compact file-backed fields, even
  when the text is short.

Use `read_guidance` as a warning against loading whole files into context. It
includes byte and line counts and recommends targeted reads first.

## JSON Schema Responses

JSON schema is an opt-in strict mode for structured extraction and downstream
automation. It is not the default OCR or transcription mode.

When `response_json_schema` or `response_json_schema_path` is present:

- Valid short JSON without `output_path` returns parsed `response_json`.
- Valid long JSON returns `response_json_preview`, `output_path`, counts, and
  `read_guidance`.
- Valid JSON with `output_path` is file-backed even when short.
- Invalid JSON returns `response_json_error` and then follows the text response
  policy for the raw Gemini text.

For budget decisions, the server uses the raw Gemini text byte count, not a
pretty-printed or compact reserialization that might hide whitespace.

## Image Sidecars

If Gemini returns image parts and `output_path` is set, image files are written
as siblings next to the text file. The MCP result includes image metadata and
paths, not image bytes.

## Agent Reading Pattern

1. Check receipt fields first: `ok`, `model`, `usage`, `elapsed_ms`, counts,
   previews, and paths.
2. If a preview or count looks suspicious, inspect a targeted range from the
   saved file.
3. Read the full file only when full synthesis or final assembly requires it.
