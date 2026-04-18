---
name: gemini-offload-workflows
description: Use when Codex needs to offload a bounded multimodal subtask to the local gemini-offload MCP server, especially OCR, audio transcription, text cleanup, file-grounded extraction, or Gemini image generation. Prefer this skill when the main agent should keep its context small, when large outputs should be written to disk via output_path, or when repeated prompts should be loaded from system_prompt_path or history_path.
---

# Gemini Offload Workflows

Use `gemini-offload` as a stateless worker for one bounded subtask at a time. Keep planning, batching, retries, and result synthesis in the orchestrator. Let Gemini handle the heavy multimodal call, then read only the output files you actually need.

## Quick Start

1. Confirm the `gemini-offload` MCP server is available.
2. Validate each input file with `detect_mime` when file type support is uncertain.
3. Prefer `output_path` for any non-trivial response so the full result is written to disk.
4. Use absolute paths for `files`, `output_path`, `system_prompt_path`, and `history_path`.
5. Call `gemini_generate` sequentially on a single stdio server connection. This server does not parallelize batched calls in one message.

## Decision Rules

- Use `gemini_generate` for one self-contained subtask with clear inputs and a clear expected artifact.
- Use `system_prompt_path` instead of inline `system_prompt` when the prompt is long or reused.
- Use `history_path` instead of inline `history` when few-shot examples already exist as JSON.
- Use `list_gemini_models` before choosing a model if the task is model-sensitive or the available surface may have changed.
- Use image-capable models only when you truly need image output. Text-only tasks should stay on text models.
- Do not expect server-side batching, retries across subtasks, or aggregation. Own that logic in the orchestrator.

## Core Workflow

### 1. Shape the subtask

Write a prompt that asks for one artifact, not a whole project. Good examples:

- OCR one PDF into markdown
- Transcribe one audio file into clean Korean text
- Normalize noisy OCR output into a CSV schema
- Extract key fields from one invoice image
- Generate one reference image from a text prompt

Avoid prompts that ask Gemini to coordinate multiple files, chunking policy, or downstream synthesis logic unless that coordination itself is the artifact you want back.

### 2. Choose the output strategy

Prefer `output_path` unless the answer is definitely short.

- With `output_path`, full text is written to disk and the inline response is only a preview.
- If Gemini returns images and `output_path` is set, sibling image files are written next to that path.
- Without `output_path`, only a truncated preview is recoverable inline.

Use unique output paths per subtask so parallel orchestration does not overwrite results.

### 3. Reuse prompt assets

This repo already includes OCR prompt assets under `prompts/`.

- [`prompts/ocr_system.md`](D:/work/cross-provider-mcp/gemini-offload/prompts/ocr_system.md)
- [`prompts/ocr_fewshot.json`](D:/work/cross-provider-mcp/gemini-offload/prompts/ocr_fewshot.json)
- [`prompts/_smoke.md`](D:/work/cross-provider-mcp/gemini-offload/prompts/_smoke.md)

When those assets match the task, pass them by path instead of copying them inline. This keeps the orchestrator context small and makes repeated runs consistent.

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

If the preview looks wrong, adjust the prompt before launching more similar calls.

### 5. Read only what you need

After a successful call, read the output file only if the next step truly needs the full content. For fan-out workflows, it is often enough to inspect previews first and only open the winning or suspicious outputs.

## Common Patterns

### OCR to markdown

Use for scanned PDFs or images when the main agent should not absorb the full OCR result inline.

Recommended call shape:

```json
{
  "prompt": "Convert this document to clean markdown. Preserve headings, tables, list structure, and uncertain text markers.",
  "files": ["D:/work/input/brochure.pdf"],
  "system_prompt_path": "D:/work/cross-provider-mcp/gemini-offload/prompts/ocr_system.md",
  "history_path": "D:/work/cross-provider-mcp/gemini-offload/prompts/ocr_fewshot.json",
  "model": "gemini-3.1-pro-preview",
  "output_path": "D:/work/out/brochure.md"
}
```

Use this when OCR quality and structure preservation matter more than speed.

### Audio transcription or cleanup

Use for MP3/WAV/FLAC/OGG/M4A files. Ask for a concrete output shape.

Example:

```json
{
  "prompt": "Transcribe this audio into Korean. Remove filler words only when they do not affect meaning. Return plain text paragraphs.",
  "files": ["D:/work/input/interview.m4a"],
  "model": "gemini-3-flash-preview",
  "output_path": "D:/work/out/interview.txt"
}
```

### Structured extraction

Use Gemini to pull a bounded schema out of a file, then keep validation in the orchestrator.

Example:

```json
{
  "prompt": "Extract invoice_number, invoice_date, vendor_name, total_amount, and currency. Return minified JSON only.",
  "files": ["D:/work/input/invoice.png"],
  "model": "gemini-2.5-flash",
  "output_path": "D:/work/out/invoice.json"
}
```

Follow with local JSON validation or repair if needed.

### Text correction or normalization

Use for post-processing noisy OCR or transcripts without re-reading the original source in the main context.

Example:

```json
{
  "prompt": "Rewrite this OCR text into readable English while preserving factual content and paragraph order. Do not summarize.",
  "files": ["D:/work/intermediate/raw-ocr.txt"],
  "model": "gemini-3.1-flash-lite-preview",
  "output_path": "D:/work/out/clean.txt"
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

## Orchestration Guidance

- Split large jobs into independent subtasks yourself.
- Give every subtask its own `output_path`.
- Reuse prompt assets by path when available.
- Start with one representative sample before launching a wide batch.
- Inspect previews before scaling out.
- Keep retry logic outside the server. The server is intentionally thin and stateless.

## Failure Modes To Watch

- Relative paths: this server requires absolute paths.
- Missing `output_path`: full text becomes unrecoverable from the inline preview.
- Wrong model: image output requires an image-capable model.
- Over-batched orchestration: multiple calls in one message do not become parallel work.
- Overloaded prompt: if a prompt asks for extraction, cleanup, summarization, and formatting at once, split it into multiple subtasks instead.

## Minimal Checklist

- Pick one bounded subtask.
- Use absolute paths everywhere.
- Prefer `output_path`.
- Reuse `system_prompt_path` and `history_path` for repeated workflows.
- Inspect the preview receipt before scaling out.
- Read the saved artifact only when the next step truly needs it.
