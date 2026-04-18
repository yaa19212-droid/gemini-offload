---
name: gemini-offload-workflows
description: Use when Codex needs to offload a bounded multimodal subtask to the local gemini-offload MCP server, especially OCR, audio transcription, text cleanup, file-grounded extraction, or Gemini image generation. Prefer this skill when the main agent should keep its context small, when large outputs should be written to disk via output_path, when a large source should be sampled and chunked before batch processing, or when repeated prompts should be loaded from system_prompt_path or history_path.
---

# Gemini Offload Workflows

Use `gemini-offload` as a stateless worker for one bounded subtask at a time. Keep planning, chunking, prompt design, retries, and result synthesis in the orchestrator. Let Gemini handle the heavy multimodal call, then read only the output files you actually need.

## Quick Start

1. Confirm the `gemini-offload` MCP server is available.
2. Validate each input file with `detect_mime` when file type support is uncertain.
3. Prefer `output_path` for any non-trivial response so the full result is written to disk.
4. Use absolute paths for `files`, `output_path`, `system_prompt_path`, and `history_path`.
5. Call `gemini_generate` sequentially on a single stdio server connection. This server does not parallelize batched calls in one message.

## Decision Rules

- Use `gemini_generate` for one self-contained subtask with clear inputs and one expected artifact.
- Sample a large source before designing a batch plan. For PDFs, inspect a few pages first when possible. For audio, inspect duration and a short excerpt. For image sets, inspect a few representative files.
- Use `system_prompt_path` instead of inline `system_prompt` when the prompt is long, reused, or iteratively refined.
- Use `history_path` instead of inline `history` when few-shot examples already exist as JSON.
- Use `list_gemini_models` before choosing a model if the task is model-sensitive or the available surface may have changed.
- Prefer a high-quality text model for first-pass OCR or transcription pilots when output fidelity matters more than speed.
- Use image-capable models only when you truly need image output. Text-only tasks should stay on text models.
- Do not expect server-side batching, retries across subtasks, or aggregation. Own that logic in the orchestrator.

## Core Workflow

### 1. Shape the subtask

Write a prompt that asks for one artifact, not a whole project. Good examples:

- OCR one PDF chunk into markdown
- Transcribe one audio chunk into clean text
- Normalize one noisy text file into a target schema
- Extract key fields from one invoice image
- Generate one reference image from a text prompt

Avoid prompts that ask Gemini to coordinate multiple files, chunking policy, or downstream synthesis logic unless that coordination itself is the artifact you want back.

### 2. Choose the output strategy

Prefer `output_path` unless the answer is definitely short.

- With `output_path`, full text is written to disk and the inline response is only a preview.
- If Gemini returns images and `output_path` is set, sibling image files are written next to that path.
- Without `output_path`, only a truncated preview is recoverable inline.

Use unique output paths per subtask so repeated or staged runs do not overwrite earlier artifacts.

### 3. Reuse prompt assets

This repo already includes reusable OCR prompt assets under `prompts/`.

- `prompts/ocr_system.md`
- `prompts/ocr_fewshot.json`
- `prompts/_smoke.md`

When those assets match the task, pass them by absolute path instead of copying them inline. This keeps the orchestrator context small and makes repeated runs consistent. When they do not match, create task-specific prompt files and reuse those by path in later calls.

### 4. Inspect the tiny receipt

Treat the MCP response as a receipt, not the whole artifact.

Check:

- `model`
- `elapsed_ms`
- `usage`
- `char_count`
- `text_preview`
- `truncated`
- `image_count`
- `images[].output_path` when image files were produced

If the preview looks wrong, adjust the prompt or chunk plan before launching more similar calls.

### 5. Read only what you need

After a successful call, read the output file only if the next step truly needs the full content. For multi-chunk workflows, it is often enough to inspect the preview first and only open suspicious, failed, or validation-targeted outputs.

## Large Input Runbook

Use this runbook when the source is too large to send as one blind call, such as a long PDF, a long lecture recording, or a folder of heterogeneous images.

### 1. Sample before committing

Inspect a few representative slices before writing the final prompt set.

- For PDFs, prefer 2-3 pages chosen from different parts of the file.
- For audio, prefer a short excerpt from the beginning plus one more excerpt from a different section when format or speaker pattern may drift.
- For image collections, inspect a few files that seem visually different.

Goal:

- infer the layout or speaking style
- spot repeated structure
- detect tables, headers, stamps, timestamps, speaker turns, or noise
- decide whether one prompt is enough or multiple prompt variants are needed

### 2. Design the chunking policy

Choose chunk boundaries in the orchestrator, not in the Gemini prompt.

Prefer chunks that are:

- large enough to preserve local context
- small enough that reruns are cheap
- easy to name and track

Useful heuristics:

- For long PDFs, chunk by page ranges.
- For long audio, chunk by duration or natural silence boundaries.
- For many images, chunk by file groups only when they truly share the same expected output format.

Default stance:

- do not add overlap unless context really spills across boundaries
- do not batch unrelated artifacts together
- keep chunk size stable unless sampling shows clear variation

Add overlap only when boundary loss is a real problem, such as a sentence, table, or speaker turn frequently crossing chunk edges.

### 3. Design prompt assets for the real format

After sampling, write reusable prompt files that match the actual source structure.

Good prompt asset design:

- keep the task narrow
- define the exact output shape
- say what to preserve
- say what to ignore
- say what not to invent

Use `system_prompt_path` for durable rules and `history_path` for one or two representative examples. Keep examples short and format-focused rather than content-specific.

### 4. Pilot one chunk first

Before a full batch, run one representative chunk with the strongest reasonable model. For OCR or transcription quality work, start with a higher-quality model when in doubt.

Pilot goals:

- confirm the output format
- confirm the prompt is specific enough
- estimate latency and token use
- decide whether chunk size is workable

### 5. Gate on quality

After the pilot, inspect both the preview and the saved artifact.

If quality is good enough:

- freeze the prompt assets
- freeze the chunking policy
- start the full run

If quality is not good enough:

- revise the prompt
- revise the few-shot example
- revise the chunk size
- rerun one chunk before scaling out

Repeat only until the results are consistently usable. Do not start a large batch while still guessing.

### 6. Run the batch sequentially

This MCP server does not turn one batched request into parallel work. For multi-chunk jobs, prepare a todo list or manifest in the orchestrator and call the tool sequentially.

Recommended per-chunk loop:

1. pick the next chunk
2. call `gemini_generate`
3. inspect `text_preview` and receipt fields
4. continue only if the result still looks on-format
5. open the saved artifact only when preview or metrics suggest a problem, or when doing periodic spot checks

This pattern works well for long OCR and transcription runs because the preview gives an early signal without forcing the orchestrator to absorb the full output every time.

## Common Patterns

### Document OCR

Use for scanned PDFs or images when the main agent should not absorb the full OCR result inline.

Recommended call shape:

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

Use this when structure preservation matters more than speed.

### Audio transcription or cleanup

Use for MP3, WAV, FLAC, OGG, or M4A files. Ask for a concrete output shape.

Example:

```json
{
  "prompt": "Transcribe this chunk into clean Korean text. Preserve meaning and speaker order. Return plain text paragraphs.",
  "files": ["D:/work/input/lecture-part-01.m4a"],
  "model": "gemini-3-flash-preview",
  "output_path": "D:/work/out/lecture-part-01.txt"
}
```

### Structured extraction

Use Gemini to pull a bounded schema out of one file or one chunk, then keep validation in the orchestrator.

Example:

```json
{
  "prompt": "Extract the target fields and return minified JSON only.",
  "files": ["D:/work/input/invoice-01.png"],
  "model": "gemini-2.5-flash",
  "output_path": "D:/work/out/invoice-01.json"
}
```

Follow with local JSON validation or repair if needed.

### Text correction or normalization

Use for post-processing noisy OCR or transcripts without re-reading the original source in the main context.

Example:

```json
{
  "prompt": "Rewrite this text into readable Korean while preserving factual content and order. Do not summarize.",
  "files": ["D:/work/intermediate/raw-part-01.txt"],
  "model": "gemini-3.1-flash-lite-preview",
  "output_path": "D:/work/out/clean-part-01.txt"
}
```

### Image generation

Use only with an image-capable model. Expect image files, not just text.

Example:

```json
{
  "prompt": "Generate a clean product illustration of a silver desk lamp on a white background.",
  "model": "gemini-2.5-flash-image",
  "output_path": "D:/work/out/lamp.txt"
}
```

Behavior:

- Text, if any, is written to `lamp.txt`.
- Images are written as sibling files such as `lamp.image-1.png`.
- The inline MCP response includes image metadata and file paths, not the full saved image bytes.

## Failure Modes To Watch

- Relative paths: this server requires absolute paths.
- Missing `output_path`: full text becomes unrecoverable from the inline preview.
- Wrong model: image output requires an image-capable model.
- Over-batched orchestration: multiple calls in one message do not become parallel work.
- Starting a full run before a pilot: quality problems get multiplied across every chunk.
- Overloaded prompt: if a prompt asks for extraction, cleanup, summarization, and formatting at once, split it into multiple subtasks instead.

## Minimal Checklist

- Sample the source before designing the batch.
- Pick one bounded subtask per call.
- Use absolute paths everywhere.
- Prefer `output_path`.
- Store reusable prompt assets on disk and reference them by path.
- Pilot one chunk before scaling out.
- Run multi-chunk jobs sequentially and inspect the preview after each call.
- Read the saved artifact only when the next step truly needs it.
