# JSON Schema And Grounding

Use this reference when a Gemini offload task needs strict JSON Schema output,
Google Search grounding, or both. Plain text is the default mode for this MCP
server and is usually the better fit for OCR, transcription, cleanup, and
unpredictable multimodal inputs.

## Mode Choice

Choose plain text when:

- the source layout is irregular or unknown
- Gemini should preserve headings, tables, uncertain readings, or commentary in
  a natural format
- the next step is human or agent review rather than direct machine ingestion
- OCR or transcription quality matters more than exact field validation

Choose JSON schema when:

- downstream code needs exact fields
- results must be tabulated or merged automatically
- repeated jobs need the same object shape
- validation failures should be explicit and actionable

Do not add a schema just because the task is important. Strict structure helps
when the target fields are known; it can hurt when the source is unusual.

## JSON Schema Inputs

Two request envelope fields are supported:

- `output.json_schema`: inline JSON Schema object
- `output.json_schema_path`: absolute path to a JSON Schema file

They are mutually exclusive. Prefer the path form when the schema is reused,
large, or easier to validate separately.

The schema file must parse as JSON and its root must be an object. The MCP input
name intentionally stays at the abstraction level; the server maps it to the
current Gemini SDK JSON output configuration internally.

## Expected JSON Result

For successful schema output, use `structuredContent.response_json` when it is
present. If the result is file-backed, use `response_json_preview` and
`output_path` instead.

Do not parse `content[0].text`. It is only a receipt.

If `response_json_error` is present, Gemini returned invalid JSON for the schema
request. Inspect the fallback text fields or saved file, then decide whether to
repair locally, retry with a stricter prompt, or fail the step.

For plain text results, use `structuredContent.text` when inline or
`text_preview` plus `output_path` when file-backed.

## Google Search Grounding

Set `tools.google_search: true` only when current or web-grounded information is
part of the task. The server asks Gemini to use Google Search and normalizes
returned grounding data for agent consumption.

The normalized grounding shape favors:

- cited answer spans
- source titles and URLs
- source index links from cited spans
- confidence scores when Gemini returns them

UI-only suggestion HTML is not treated as evidence and should not drive agent
decisions.

## Combining Schema And Search

Schema output and Google Search can be used together. Keep the prompt explicit:

- ask for JSON matching the schema
- include fields for source URLs or citations if downstream validation needs
  them
- keep free-form prose out of schema-only responses

Validate important facts outside Gemini when accuracy is high-stakes.
