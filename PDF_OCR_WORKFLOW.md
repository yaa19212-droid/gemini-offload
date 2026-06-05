# PDF OCR Workflow

This document describes a generic workflow for converting PDF pages into
reviewable OCR inputs, image artifacts, and text outputs with `gemini-offload`.
It intentionally uses placeholder paths and sample names so the repository can
be shared publicly without leaking local course material, client material, or
machine-specific paths.

## Core Principles

- Keep preprocessing and OCR as separate steps. Review the preprocessed page
  images before launching a large OCR run.
- Preserve meaningful figures, screenshots, charts, and diagrams as
  `[[IMG:...]]` placeholders when they should remain linked to the text output.
- Run Gemini OCR in chunks. Each chunk should have its own `output.path`.
- Treat the final manifest as the source of truth for validation.

## Standard Directory Layout

Use one artifact root per source document or document batch.

```text
<artifact-root>/
  raw-pdf/
  review/
  ocr-pages/
  ocr-jobs/
  output/
    img/
    text/
  qa/
    text-layer-baseline/
```

Example placeholder paths:

```text
D:/work/pdf-ocr/example-document
D:/work/pdf-ocr/example-document/output/text
```

## Required Tools

Keep reusable helper scripts and prompt assets under a project-local tools
directory, for example:

```text
tools/pdf_ocr/
tools/pdf_ocr/scripts/
prompts/ocr_system.md
prompts/ocr_fewshot.json
```

Typical helper scripts:

- `preprocess_pdf_review.py`: render PDF pages and optionally split complex
  layouts into OCR-friendly page images.
- `publish_candidate_images.py`: copy selected figure candidates into
  `output/img/`.
- `render_placeholders.py`: insert or verify `[[IMG:...]]` placeholders.
- `create_ocr_jobs.py`: split OCR pages into run item manifests.
- `extract_pdf_text_baseline.py`: extract a text-layer baseline when available.
- `validate_placeholder_outputs.py`: verify page sections and placeholders.

## Workflow

### 1. Intake

Place source PDFs under `<artifact-root>/raw-pdf/`. Prefer ASCII filenames for
batch scripts, but the workflow can support non-ASCII filenames when paths are
handled with literal-path APIs.

```powershell
$tool = "tools/pdf_ocr"
$artifact = "D:/work/pdf-ocr/example-document"
```

### 2. Preprocess and Review

Render source pages into `review/` first. If the PDF uses two-up pages,
slide-plus-notes layouts, or scanned spreads, split them before OCR.

The review gate should answer:

- Are pages in the right order?
- Are rotations and crops correct?
- Are important figures preserved?
- Does each OCR page correspond to the expected source page?

### 3. Create OCR Run Items

Create a manifest where each item has:

- a stable `id`
- one or more page image or PDF chunk paths
- a clear prompt in `contents[].parts[]`
- an absolute `output.path`

Small example:

```json
{
  "execution": {"lifecycle": "background", "max_concurrency": 4},
  "items": [
    {
      "id": "chunk-001",
      "request": {
        "system": {"path": "D:/work/gemini-offload/prompts/ocr_system.md"},
        "contents": [{"role": "user", "parts": [
          {"file_path": "D:/work/pdf-ocr/example-document/ocr-pages/page-001.png"},
          {"text": "OCR this chunk to clean markdown. Preserve headings, lists, tables, and uncertain text markers."}
        ]}],
        "output": {"mode": "text", "path": "D:/work/pdf-ocr/example-document/output/text/chunk-001.md"}
      }
    }
  ]
}
```

### 4. Validate Outputs

After OCR, check:

- expected page count vs. actual `=== PAGE N ===` sections
- placeholder count and missing placeholder IDs
- empty pages that should contain text
- duplicate or skipped page ranges
- unusually short chunks

When only a chunk is wrong, rerun that chunk with the same input files and a new
or overwritten output path. Do not regenerate preprocessing artifacts unless
the page images or placeholder manifest changed.

## Reporting Checklist

Final reports should include:

- artifact root
- image output path
- text output path
- source PDF count and rendered page count
- OCR chunk count
- expected/actual page section count
- placeholder validation result
- manual review notes

Example:

```text
Artifact root:
D:/work/pdf-ocr/example-document

Image output:
D:/work/pdf-ocr/example-document/output/img

OCR output:
D:/work/pdf-ocr/example-document/output/text

Validation:
8 chunks, 32 expected sections, 32 actual sections.
4 expected placeholders, 0 missing.
validate_placeholder_outputs.py: 0 errors, 0 warnings.
```
