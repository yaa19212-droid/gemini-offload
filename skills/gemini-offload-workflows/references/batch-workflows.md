# Multi-Item And Background Runs

Use `call_gemini` with multiple items when independent artifacts can run
concurrently, such as page OCR, audio chunk transcription, cleanup passes, or
repeated extraction.

## Execution Model

- A run always has one or more items.
- `execution.lifecycle: "blocking"` waits for all items.
- `execution.lifecycle: "background"` starts a child worker and returns paths.
- Items run concurrently up to `execution.max_concurrency`.
- The old `gemini_generate_batch` tool is intentionally replaced.

## Template Runs

Prefer template input for repeated work. The template is a request envelope with
`{{placeholder}}` strings; each item supplies vars.

Use this for OCR/chunk runs so repeated system prompts, content structure, and
output path rules live in one file.

Placeholder names are run-local and conservative. Missing vars, unused vars,
relative paths after substitution, or invalid placeholder names are rejected.

## Blocking Aggregate Budget

Blocking multi-item runs have a 4096-byte aggregate budget over final
`structuredContent`.

If the response is too large:

- successful inline `text` and `response_json` results are written to files
- already file-backed successful items are reused
- error items remain inline when possible
- compacted results may move to `results_path`

Follow `read_guidance` and inspect targeted item outputs instead of loading the
whole result set.

## Background Reading Pattern

For long runs:

1. Start `call_gemini` with background lifecycle.
2. Save `run_id`, `run_dir`, `status_path`, and `events_path`.
3. Use `manage_gemini_run` with `status` or `progress`.
4. Read only newly appended events by offset.
5. Inspect failed items or sampled successful output files.
6. Assemble final artifacts locally from saved output paths.

Avoid loading every saved output or the full event log into the main context.
