# MCP Architecture Refactor Notes

This document captures the run-oriented gemini-offload MCP architecture after
the breaking refactor to `call_gemini` and `manage_gemini_run`. It clarifies the
request axes, their cross-axis effects, and which concepts should not be
promoted to top-level modes.

## Goals

- Preserve freedom to construct rich Gemini requests.
- Keep the MCP abstraction balanced for context optimization.
- Avoid hidden context growth, especially in repeated OCR and batch-like work.
- Make blocking and background execution use the same request model.
- Keep plain text and JSON schema output as first-class choices.
- Leave `code_execution` as a future design topic, not part of this refactor.

## Implemented Durable Runtime (0.3.0)

The current implementation uses a SQLite WAL database under the configured run
root as the authoritative background-run store. `runs`, `items`, `artifacts`,
`events`, and `worker_leases` are relational tables with schema versioning and
explicit transactions. Per-run `status.json` and `events.jsonl` files are
compatibility/debug exports; they are not source of truth.

Run state transitions are enforced (`queued`, `starting`, `running`, `stopping`,
`canceling`, `completed`, `failed`, `stopped`, `canceled`) and item transitions
are likewise explicit (`pending`, `running`, `completed`, `failed`, `stopped`,
`canceled`). A state mutation and its corresponding event are committed in the
same SQLite transaction where applicable.

Worker ownership uses a monotonically increasing lease generation and random
token. Worker state/artifact mutations verify that fence inside the transaction
that publishes them. Heartbeats renew the lease; forced cancellation revokes it
before terminal cancellation is committed, preventing a stale worker from
publishing after ownership changes.

Managed background outputs are confined beneath `<run_dir>/outputs/` and use
index-derived storage keys. Caller item IDs remain opaque metadata and never
become filesystem names. Explicit caller-provided absolute output paths remain
supported, but are recorded as unmanaged/user-selected artifacts.

Completed items are resumable without re-execution only when all recorded
artifacts still exist as regular files and match recorded byte counts and
SHA-256 digests. Missing or tampered artifacts transition back to pending and
produce a recovery event. Startup recovery maps stale `starting`/`running` runs
without a live lease to `failed`, `stopping` to `stopped`, and `canceling` to
`canceled`.

The implementation is separated by responsibility:

- `run_store.py`: durable SQLite state, transitions, events, leases/fencing.
- `artifacts.py`: managed path confinement, atomic writes, hashes, validation.
- `output_policy.py`: text/JSON/image spill and compact return policy.
- `run_service.py`: request materialization and run/item orchestration.
- `worker.py` / `run_worker.py`: worker process lifecycle and background entrypoint.
- `server.py`: MCP schemas/handlers, compact result wrapping, and narrow adapters.
- `gemini_client.py` / `keys.py`: Gemini API and quota/credential concerns.

`run_worker.py` intentionally imports `worker.py` directly rather than importing
the MCP transport server, so background execution does not depend on the stdio
transport layer.

## Core Model

The central unit is a run.

A run executes one or more request items. A one-item run is still a normal run;
there is no separate single mode. Multiple items may run concurrently depending
on execution settings.

Each request item is materialized into a Gemini request envelope:

- `system`: system instruction, inline text or path-backed text.
- `contents[]`: ordered Gemini-style content entries.
- `contents[].parts[]`: text, text path, local file path, and future part types.
- `output`: output contract and result storage intent.
- `tools`: runtime Gemini tools such as Google Search.

## Top-Level Axes

### Request Materialization

How the final request item envelope is produced.

- `explicit`: every item provides a complete request envelope.
- `template`: a reusable request template is combined with each item's vars.

This axis is independent of item count. A template run may contain one item.
That is useful for pilot calls, retrying one failed chunk, or handling a late
"41st chunk" after a 40-chunk OCR run.

### Execution Lifecycle

How the MCP call relates to run completion.

- `blocking`: the MCP call waits until all items finish.
- `background`: the MCP call starts a run and returns a receipt immediately.

Background execution requires durable result storage and run bookkeeping.

### Execution Concurrency

How many request items may run at the same time.

- `max_concurrency`: applies when a run has more than one item.

For a one-item run, effective concurrency is naturally one. `item_count` is not
an input mode; it is derived from the item array length.

### Content Model

How Gemini context is represented.

The canonical model should be `system` plus ordered `contents[]`, not
`prompt/files/history` as separate top-level categories. Prompt text, text files,
local files, and prior turns are all part of the content graph.

The current `history` concept is better understood as preloaded content turns.
It should not encourage agents to copy the full Codex conversation into Gemini.

### Output Contract

What the model is asked to return.

- `text`: default, flexible output for OCR, transcription, cleanup, and unusual
  multimodal tasks.
- `json_schema`: strict structured output when exact fields, validation,
  tabulation, or automation matter.

This is a single-axis choice. Plain text and JSON schema are mutually exclusive
values of the same axis, not two independent axes.

### Runtime Tools

Additional Gemini tools used during generation.

- `google_search`: current scope.
- `code_execution`: future planning only; excluded from this refactor.

Google Search can combine with text or JSON schema output when the model/API
capability supports the combination.

### Result Storage

Where full result bodies are written.

- explicit output path
- output path template
- automatic run/output directory

Result storage is separate from inline return policy. Background execution
depends on storage; blocking execution may still return small results inline.

### Inline Return Policy

How much result body is returned in the immediate MCP tool response.

Blocking runs may return small result bodies inline and spill large ones.
Background runs should return receipt/status information only, not final result
bodies.

## Derived Concepts

These should not be top-level request modes.

- `single`: derived from `items.length == 1`.
- `batch`: derived from `items.length > 1`, concurrency, and lifecycle.
- `item_count`: result summary, not input.
- `manifest`: run bookkeeping artifact, not a delivery mode.
- `history`: content model detail, not a top-level axis.
- `prompt` and `files`: part types inside `contents[].parts[]`, not top-level
  axes.

## Cross-Axis Effects

This section lists effects between different axes only. It intentionally omits
same-axis mutual exclusions.

### Execution Lifecycle -> Result Storage

If lifecycle is `background`, full result bodies must be written to durable
paths. The initial MCP response cannot be the only place that contains the
result.

Design implication: background runs need either explicit output paths, output
path templates, or a server-managed run output directory.

### Execution Lifecycle -> Inline Return Policy

If lifecycle is `background`, final `text` or `response_json` should not be
returned inline in the start response. The start response should contain a
receipt, run id, status path or status handle, manifest path when available, and
read guidance.

Blocking runs can use the existing inline/spill policy.

### Execution Lifecycle -> Run Bookkeeping

Background execution requires run state that survives beyond the initial tool
call:

- run id
- status
- item statuses
- output paths
- errors
- cancellation state
- timestamps

Design implication: bookkeeping is a server responsibility derived from
background lifecycle, not a user-selected delivery mode.

### Request Materialization -> Result Storage

Template materialization naturally interacts with result paths. Repeated OCR
items often need output paths derived from item variables.

Design implication: output path templates should be valid template fields, and
their resolved values must be absolute paths.

### Request Materialization -> Content Model

Template placeholders may appear in content text, text paths, local file paths,
system paths, and output paths.

Design implication: placeholder substitution should be simple and predictable,
for example `{{name}}` in string fields only. After substitution, all paths must
be validated as absolute paths.

### Request Materialization -> Context Size

Explicit multi-item runs can repeat large request envelopes. Template runs can
keep common context in one template and send only vars per item.

Design implication: explicit materialization should remain available for
heterogeneous work, but template materialization should be the preferred path
for repeated OCR/chunk workflows.

### Content Model -> Context Size

Large inline system instructions, text parts, and prior turns increase MCP
payload and Gemini context. Path-backed parts reduce repeated MCP payload size
and make reusable prompts easier to audit.

Design implication: path-backed text should be first-class in `system` and
`contents[].parts[]`.

### Runtime Tools -> Result Metadata

Google Search changes result metadata by adding normalized grounding fields. It
does not change the requested output body by itself.

Design implication: grounding should remain metadata in the result envelope,
not part of the plain text or JSON schema contract unless the prompt/schema asks
for sources explicitly.

### Runtime Tools -> Output Contract

Google Search should be composable with text and JSON schema output when
supported by the selected model/API.

Design implication: validation should reject unsupported combinations before
starting a run, especially for background execution where failures are harder to
repair interactively.

### Output Contract -> Result Storage And Inline Return

The output contract changes the shape of stored and inline result fields:

- text output stores raw text and may inline `text`.
- JSON schema output stores raw JSON text and may inline parsed `response_json`.
- JSON parse failure must preserve raw text plus `response_json_error`.

Design implication: storage and inline policy should operate on a normalized
result envelope while respecting output-contract-specific field names.

### Item Array Length -> Concurrency

If a run has one item, `max_concurrency` has no practical effect. If a run has
multiple items, `max_concurrency` controls fan-out inside the run.

Design implication: no separate single/set mode is needed.

### Item Array Length -> Run Bookkeeping

Multi-item runs need an index of item results even when blocking. Background
runs always need bookkeeping. Blocking one-item runs may not need a persisted
manifest.

Design implication: manifest-like artifacts should be derived from item count
and lifecycle.

### Item Array Length -> Inline Return Policy

Blocking multi-item runs can exceed inline budget even when each item is small.

Design implication: aggregate inline budgeting is still needed for blocking
multi-item runs. Background runs should avoid final-body inline return entirely.

## Settled Design Decisions

The following decisions came out of the first Q&A pass.

### Materialization Shapes

`explicit` and `template` should be exposed as different top-level input shapes,
not as values of one `source.type` object.

Rationale: the two shapes ask agents to think differently. Explicit runs provide
complete request envelopes per item. Template runs provide one reusable envelope
plus per-item placeholder values. Combining both under one discriminated object
can hide that difference and make validation less clear.

Both shapes should be accepted by the same primary tool, `call_gemini`.

### Lifecycle Parameter

Blocking and background execution should be selected by a tool parameter, not by
separate tool names.

The return shape changes by lifecycle:

- blocking returns the completed run result, subject to inline/spill policy.
- background returns a hardcoded receipt and durable paths, for example:
  background run started, results will be written under the returned path,
  progress will be surfaced by Codex hooks, and manual status inspection can use
  the appended registry log.

The primary tool name is `call_gemini`.

### Run Management Tool

Status, progress, cancel, stop, and resume should be handled by one management
tool rather than several separate tools.

The management tool should be named `manage_gemini_run`.

Use an `action` parameter rather than separate command-object shapes. MCP tool
schemas are easier for agents to discover and use when the tool has one stable
shape with an action enum:

- `list`
- `status`
- `progress`
- `cancel`
- `stop`
- `resume`

This management tool must inspect runtime state directly when answering
process-liveness questions or controlling a running process. A static registry
file is useful history, but it is not an authority for live process state.

`stop` and `cancel` should have different meanings:

- `stop`: ask the run to stop cooperatively, usually after the current item, so
  the run can be resumed later.
- `cancel`: mark the run as intentionally terminated and stop work as soon as
  practical.
- `resume`: start or reattach execution for a stopped, interrupted, unknown, or
  partially complete run when the durable plan and completed-item records allow
  it.

### Live Runtime State

Durable lifecycle state comes from SQLite; OS process inspection is supporting
evidence for liveness and process control rather than a competing state store.
`manage_gemini_run list/status/progress` queries the durable run store. Status and
control paths may additionally validate `locator.json` against the OS process table
using PID, process creation time, and the run token.

An active worker owns a SQLite lease `(run_id, generation, token)` with heartbeat
expiry. The generation is monotonic, so a reclaimed run fences out every older
worker. A worker may finish an in-flight external Gemini request after losing its
lease, but it cannot commit state or publish output artifacts afterward.

### Background Durable Store And Compatibility Exports

The authoritative registry is `.gemini-offload-runs.sqlite3` in the run root,
using SQLite WAL mode. It stores query-critical run/item states relationally plus
artifact integrity metadata, events, and worker leases. Status mutations and their
corresponding events are committed transactionally.

Each run directory still exposes `plan.json`, `status.json`, `events.jsonl`,
`locator.json`, `control/`, and `outputs/`. These files serve compatibility, audit,
and targeted debugging needs. In particular, `status.json` and `events.jsonl` are
exports from committed state and must not be used as authority when they disagree
with SQLite. `manage_gemini_run progress` provides the supported cursorable event
view.

Control semantics are:

- `stop`: transition toward `stopping`, then `stopped`; the run is resumable.
- `cancel`: transition toward `canceling`, then `canceled`; forced cancel revokes
  the lease before terminal publication.
- `resume`: clear old control files, transition to `starting`, acquire a new lease
  generation, and re-run only non-terminal or artifact-invalid items.

Startup reconciliation handles interrupted runs using durable state plus leases.
A stale `starting` or `running` run without active ownership becomes `failed`; a
stale `stopping` run becomes `stopped`; a stale `canceling` run becomes `canceled`.

### Codex Hooks

Hooks should surface active background runs but should not be the worker. The
MCP server owns execution and registry updates.

Desired hook behavior:

- inject active run progress into prompts when relevant
- if Codex is already processing a turn, steer the next model step with compact
  progress or completion context
- if Codex is idle when a run completes, trigger a new prompt if Codex supports
  that flow
- avoid reading full result files or full registries into context

Current Codex hook docs show these relevant capabilities:

- `PostToolUse` can see MCP tool results and add model-visible context after a
  tool call.
- `UserPromptSubmit` and `SessionStart` can add extra developer context.
- `Stop` can ask Codex to continue by producing a continuation prompt.
- Plugin-bundled hooks are supported.
- Command hooks are the supported handler type today; async command hooks are
  parsed but skipped.

The docs do not show a general idle-time external trigger that starts a new
assistant response at the exact moment an arbitrary background process finishes.
Therefore the reliable design should use hooks for context injection and
continuation points, while the MCP server and management tool remain the
authoritative background run system.

Reference: <https://developers.openai.com/codex/hooks>

Codex app-server documentation shows a separate protocol that can start turns,
steer active turns, inject items, and stream notifications. That is promising
for custom clients, but it is not the same thing as a plugin hook API for making
Codex Desktop for Windows start a new response when an arbitrary external worker
finishes.

References:

- <https://developers.openai.com/codex/hooks>
- <https://developers.openai.com/codex/app-server>

### Placeholder Rules

Placeholder scope is per run. A placeholder defined for one run must not affect
any other run.

Syntax direction:

- placeholder delimiters use braces, such as `{{name}}`
- placeholder names must not contain newlines
- placeholder names must not contain `{` or `}`
- placeholder names may contain Unicode letters and numbers, Korean text, ASCII
  letters and numbers, space, and this conservative punctuation set:
  `_-.()[]@+=,~`
- after substitution, path fields must still pass absolute-path validation

The punctuation set is intentionally narrower than "anything Windows allows".
Windows file names have reserved characters and names, and trailing spaces or
periods are problematic. Placeholder names are identifiers rather than literal
file names, so they should stay predictable even when the substituted values are
more flexible.

Reference: <https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file>

### Breaking Change

The new run-oriented tool surface should replace the current
`gemini_generate`/`gemini_generate_batch` surface. Compatibility tools are not a
goal for this refactor.

This is a deliberate breaking change.

## Public Shape

`call_gemini` accepts either explicit request items or a template plus per-item
vars.

Explicit shape:

```json
{
  "items": [
    {
      "id": "one",
      "request": {
        "system": {
          "path": "D:/work/prompts/system.md"
        },
        "contents": [
          {
            "role": "user",
            "parts": [
              {"file_path": "D:/work/input.pdf"},
              {"text": "OCR this file to markdown."}
            ]
          }
        ],
        "output": {
          "mode": "text",
          "path": "D:/work/out/one.md"
        },
        "tools": {
          "google_search": false
        }
      }
    }
  ],
  "execution": {
    "lifecycle": "blocking",
    "max_concurrency": 1
  }
}
```

Template shape:

```json
{
  "template_path": "D:/work/templates/ocr-request.json",
  "items": [
    {
      "id": "page-041",
      "vars": {
        "chunk_path": "D:/work/chunks/page-041.pdf",
        "page": 41
      }
    }
  ],
  "execution": {
    "lifecycle": "background",
    "max_concurrency": 5
  }
}
```

The referenced template could materialize to:

```json
{
  "system": {
    "path": "D:/work/prompts/ocr-system.md"
  },
  "contents": [
    {
      "role": "user",
      "parts": [
        {"file_path": "{{chunk_path}}"},
        {"text": "OCR page {{page}} to markdown."}
      ]
    }
  ],
  "output": {
    "mode": "text",
    "path": "D:/work/out/page-{{page}}.md"
  },
  "tools": {
    "google_search": false
  }
}
```

## Historical Design Notes

The original refactor notes below this point were written before implementation
and are now superseded by the implemented runtime described above. The important
outcomes were:

- a child worker process was selected for background execution;
- PID/create-time/token verification and verified process-tree termination were
  implemented for Windows-compatible control;
- SQLite WAL replaced the proposed JSONL registry as the authoritative state
  store, while JSONL remains an audit/debug compatibility export;
- Codex hook helpers remain optional context-injection aids rather than workers or
  authorities;
- placeholder names keep the conservative character rules described in the public
  request model; and
- implementation discoveries, deviations, and validation evidence are recorded in
  `IMPLEMENTATION_LOG.md`.

## User-Level Decisions Remaining

No remaining item in this document is waiting on the user for investigation.
Future user decisions should be limited to product-level tradeoffs, such as
whether the proposed behavior feels right for agent ergonomics, not runtime API
or platform research.

## Setup, Model Registry, and Runtime Policy

Credential diagnostics are exposed through the read-only `check_gemini_setup`
tool. It shares manifest resolution with the runtime credential loader, performs
local manifest/key validation plus a bounded OAuth refresh, and never returns
private key or token material. A verified result proves credential refresh, not
model quota or every Vertex permission. On Windows, `install-local.ps1`
configures `%LOCALAPPDATA%/gemini-offload/runs` as the persistent run root by
default; the core server retains its OS-temp fallback for manual setups.

Model support is maintainer-curated in `model_registry.py`; runtime model
metadata never auto-authorizes a newly discovered model. The current selection
policy is `gemini-3.7-flash` for normal work, `gemini-3.1-pro-preview` as a
quality-first option, and `gemini-3.6-flash` then `gemini-3.5-flash` only as
explicit 429 fallbacks. `gemini-3-flash-preview` is removed. Capability
preflight covers public combinations such as input modality, thinking summary,
Google Search, JSON schema, and media resolution.

The Gemini client explicitly sets supported configurable harm-filter categories
to `OFF` as an internal policy; this does not imply that all Vertex/provider
protections are disabled. Remote artifact reading is intentionally outside this
MCP's current surface: callers may pair it with any filesystem-capable companion,
with no specific companion dependency. Registered-artifact retrieval remains a
roadmap item.
