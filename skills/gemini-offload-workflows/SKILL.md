---
name: gemini-offload-workflows
description: Use when Codex needs to offload a bounded Gemini subtask to the local gemini-offload MCP server, especially OCR, transcription, flexible plain-text multimodal analysis, file-grounded extraction, optional strict JSON schema output, Google Search grounding, text cleanup, or concurrent batch processing. Prefer this skill when the main agent should keep context small, large outputs should be written to disk via output_path, a source should be sampled and chunked before batch work, or reusable prompts/schemas should be loaded from path.
---

# Gemini Offload Workflows

Use `gemini-offload` as a stateless worker for bounded subtasks. Keep planning,
chunking, retries, validation, and synthesis in the orchestrator. Let Gemini do
the heavy multimodal or grounded call, then inspect only the receipt fields and
saved files needed for the next decision.

## Quick Start

1. Confirm the `gemini-offload` MCP server is available.
2. Use absolute paths for `files`, `output_path`, `system_prompt_path`,
   `history_path`, and `response_json_schema_path`.
3. Use `detect_mime` when file type support is uncertain.
4. Use `gemini_generate` for one artifact and `gemini_generate_batch` for
   independent artifacts that can run concurrently.
5. Default to plain text output. Add JSON schema only when the task truly needs
   a strict machine-readable contract.
6. Prefer `output_path` for artifacts you may need to inspect later. It forces
   file output even when the response is short.
7. Treat `structuredContent` as authoritative. `content[0].text` is only a
   short receipt or read guide.

## Decision Rules

- Shape each call as one bounded artifact: one OCR chunk, one transcript chunk,
  one plain-text multimodal analysis, one cleanup pass, one schema extraction,
  or one grounded answer.
- Sample large sources before designing prompts or a batch plan.
- Use `system_prompt_path` for long or reused instructions.
- Use `history_path` for reusable few-shot examples.
- Keep OCR, transcription, cleanup, and unfamiliar image/PDF analysis in plain
  text unless downstream code needs strict fields.
- Use `response_json_schema_path` for reusable structured extraction schemas
  only when validation, tabulation, or automation is more important than
  free-form adaptation.
- Use `google_search: true` only when web grounding is part of the task.
- Use `list_gemini_models` when model choice matters or may have changed.
- Use `gemini_generate_batch` only for independent jobs. The tool waits for all
  jobs, but jobs run concurrently up to `max_concurrency`.

## Response Mode Choice

Plain text and JSON schema are both first-class output modes, but they serve
different work.

- Use plain text by default for OCR, transcription, cleanup, document reading,
  uncertain visual layouts, and exploratory multimodal analysis. This gives
  Gemini room to preserve odd formatting, note uncertainty, and adapt to
  unexpected source structure.
- Use JSON schema for strict extraction, repeatable table rows, downstream
  automation, or cases where the orchestrator must validate exact fields.
- Do not wrap an OCR task in JSON just to make it look structured. Prefer
  markdown or plain text when the source is irregular.
- It is fine to combine a plain text prompt with `output_path`; file-backed
  plain text is the normal path for large OCR and transcription artifacts.

## Output Handling

Default policy:

- Short text without `output_path`: full text is returned in
  `structuredContent.text`.
- Long text without `output_path`: text is auto-saved and the response returns
  `text_preview`, `output_path`, `byte_count`, `line_count`, and
  `read_guidance`.
- Any response with `output_path`: full output is saved to that path and inline
  fields stay compact, even when short.
- JSON schema success returns `response_json` when short, or
  `response_json_preview` plus `output_path` when large or manually spilled.
- Batch applies a 4096-byte aggregate budget after all jobs finish. If compacted
  results are still large, use `results_path` and inspect targeted sections.

For exact fields and edge cases, read
`references/output-policy.md`.

## JSON Schema And Grounding

Plain text is the default. Use `response_json_schema` for a small inline JSON
Schema object, or `response_json_schema_path` for an absolute path to a JSON
schema file, only when strict structure is useful. They are mutually exclusive.
When a schema is present, the server switches Gemini into JSON output mode
internally.

Use `google_search: true` for Google Search grounding. Grounding metadata is
normalized for agent use; do not depend on raw Gemini UI-only fields.

Read `references/schema-and-grounding.md` before requiring strict JSON,
combining JSON schema with Google Search, or designing downstream validation.

## Batch Workflow

For OCR, transcription, or extraction over many chunks:

1. sample a few representative chunks
2. write durable prompt files, and schema files only when strict fields are
   needed
3. pilot one chunk
4. freeze the chunking and prompt policy
5. call `gemini_generate_batch` with one independent job per chunk
6. inspect previews, errors, and only targeted output files

The batch tool is synchronous at the MCP boundary. Do not expect background
queue behavior yet. For detailed batch receipts and budget behavior, read
`references/batch-workflows.md`.

## Prompt Assets

This repo includes reusable OCR assets:

- `prompts/ocr_system.md`
- `prompts/ocr_fewshot.json`
- `prompts/_smoke.md`

Use these by absolute path when they match the source format. Create new prompt
files for task-specific rules instead of copying long instructions inline.
Create schema files only for strict extraction tasks.

## Common Call Shapes

OCR one chunk:

```json
{
  "prompt": "Convert this chunk to clean markdown. Preserve headings, tables, list structure, and uncertain text markers.",
  "files": ["D:/work/input/chunk-01.pdf"],
  "system_prompt_path": "D:/work/gemini-offload/prompts/ocr_system.md",
  "history_path": "D:/work/gemini-offload/prompts/ocr_fewshot.json",
  "model": "gemini-3.1-pro-preview",
  "output_path": "D:/work/out/chunk-01.md"
}
```

Structured extraction:

```json
{
  "prompt": "Extract the requested fields. Return JSON matching the schema.",
  "files": ["D:/work/input/invoice-01.png"],
  "response_json_schema_path": "D:/work/schemas/invoice.schema.json",
  "model": "gemini-3.5-flash",
  "output_path": "D:/work/out/invoice-01.json"
}
```

Grounded short answer:

```json
{
  "prompt": "Find the latest official release date and return a concise sourced answer.",
  "google_search": true,
  "model": "gemini-3.5-flash"
}
```

## Failure Modes

- Relative paths are rejected for file/path inputs.
- Missing `output_path` is acceptable for short one-off answers, but larger
  outputs spill to the configured output directory.
- Overusing JSON schema can make OCR and unusual multimodal tasks brittle.
  Default to plain text when the output shape is not naturally fixed.
- JSON schema parse failures return `response_json_error` plus text fallback
  fields.
- Batch jobs should be independent and should use unique `output_path` values.
- Large batch receipts may omit inline `results` and return `results_path`.
- Do not read full output files or manifests unless the next step requires it.

## Minimal Checklist

- Sample first for large sources.
- Keep one bounded artifact per call.
- Use absolute paths.
- Use path-backed prompts and histories. Use schemas only for strict extraction.
- Prefer explicit `output_path` for reusable artifacts.
- Inspect `structuredContent`, not `content[0].text`.
- For batch results, follow `read_guidance` and open only targeted files.
