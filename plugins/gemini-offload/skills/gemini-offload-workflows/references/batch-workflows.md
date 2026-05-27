# Batch Workflows

Use `gemini_generate_batch` for many independent jobs that can run concurrently,
such as page-range OCR, audio chunk transcription, plain-text cleanup, or
repeated structured extraction.

## Execution Model

- The MCP tool call is synchronous: it returns after all jobs finish.
- Jobs run concurrently up to `max_concurrency`.
- The server uses a semaphore and task group internally; the aggregate budget is
  evaluated only after all jobs have completed.
- Rate limits are tracked per Vertex project/location/model quota slot.

Do not use batch for jobs that depend on each other's outputs.

## Aggregate Budget

The batch response has a 4096-byte aggregate budget over final
`structuredContent`.

If the initial response is too large:

- successful inline `text` and `response_json` results are written to per-job
  files
- already file-backed successful jobs are reused and not written again
- error jobs remain inline when possible for diagnosis
- `results_compacted`, `aggregate_byte_count`, `aggregate_inline_limit`, and
  `read_guidance` explain what happened

If compacted results are still too large:

- full compacted results are written to `results_path`
- inline `results` becomes an empty array
- `results_omitted` and `omitted_result_count` tell the agent not to expect
  inline job details

## Recommended OCR Or Extraction Loop

1. Build a manifest of chunks in the orchestrator.
2. Give every job a stable `id` and unique `output_path`.
3. Use shared `system_prompt_path`, `history_path`, or
   `response_json_schema_path` when that mode fits the task.
4. Set `max_concurrency` to the desired parallelism.
5. After the batch returns, inspect summary counts first.
6. Open only failed jobs, suspicious previews, or sampled successful outputs.
7. Assemble final output locally from the saved artifacts.

## Reading Results Safely

Prefer targeted reads:

- inspect the top-level summary
- inspect failed job entries
- read one or two sampled output files
- read `results_path` only by targeted ranges or JSON filtering when possible

Avoid loading every saved output or the full manifest into the main context.
For OCR and irregular multimodal sources, prefer plain text or markdown output
unless a downstream automated merge needs strict fields.
