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

The management tool should inspect live runtime state, not infer liveness from
the registry log.

Preferred architecture:

- `call_gemini` starts each background run as a supervised worker process, or as
  a supervised task with an equivalent live handle.
- The run directory stores durable locator data such as `run_id`, worker pid,
  worker start token, control channel path, plan path, and output directory.
- `manage_gemini_run` checks the live worker handle when the MCP server still
  owns it.
- If the MCP server was restarted, `manage_gemini_run` uses the locator data to
  inspect the OS process table and verify the worker identity with the run token
  or command line.
- A heartbeat file or status snapshot can help distinguish active, waiting,
  stale, and unknown states, but it is supporting evidence rather than the sole
  authority.

Possible live statuses:

- `running`
- `waiting_rate_limit`
- `stopping`
- `canceling`
- `stopped`
- `completed`
- `failed`
- `unknown`

`unknown` means the durable files say a run should exist, but the management
tool cannot prove current liveness. Examples include a missing process, a stale
heartbeat, a restarted MCP server without a matching worker handle, or a worker
process whose identity cannot be verified.

### Background Registry

A background run needs an append-friendly registry on disk. The registry must
let an agent inspect only newly appended content instead of rereading the whole
history every time.

Minimum registry content:

- run id
- lifecycle
- start timestamp
- latest status timestamp
- item ids
- output paths
- manifest path or run directory
- append-only progress events
- error events with timestamp, item id, error type, and message
- completion events
- cancellation events

The registry must not be treated as the source of truth for process liveness.
Process liveness is runtime state and must be checked directly. Likewise,
cancel, stop, and resume are runtime controls; they cannot be implemented
reliably by editing or reading a static registry file alone.

The registry can still record observed liveness checks and management actions
as append-only events, including unknown state, rate-limit waiting, cancellation
requests, stop requests, and resume attempts.

The registry should be treated primarily as a debugging and audit artifact.
Runtime state should come from the worker handle, OS process inspection, and
current worker status.

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

## Agent-Owned Follow-Up

The following items are not product questions for the user. They are engineering
research and design work owned by the implementer before coding starts.

### Registry Event Model

Design a JSONL-style append log for debugging and audit only. The log should
record observations without claiming authority over live state.

Proposed event families:

- `run_started`
- `item_started`
- `item_completed`
- `item_failed`
- `progress_observed`
- `liveness_observed`
- `rate_limit_wait_started`
- `rate_limit_wait_ended`
- `stop_requested`
- `cancel_requested`
- `resume_requested`
- `resume_started`
- `run_stopped`
- `run_canceled`
- `run_completed`
- `run_failed`

Every event should include timestamp, run id, event type, and source. Item
events should include item id. Liveness events should include the method used to
observe state and a confidence level, because the registry is recording an
observation rather than defining the truth.

### Background Worker Architecture

Prefer a child worker process by default.

Reasoning:

- in-process supervised tasks are easy to start but fragile if the MCP server
  process exits or is restarted
- detached workers can survive longer but are harder to supervise, cancel, and
  attribute to a run
- child worker processes give each background run an inspectable OS process,
  durable locator data, a control channel, and cleaner failure isolation

The implementation should still allow the MCP server to reattach after restart
using run-directory locator files.

### Windows Runtime Control

Design Windows worker identity and cancellation around runtime inspection:

- store worker pid, process creation/start token when available, command line
  marker, run id, and control channel path in the run directory
- verify a process by pid plus command-line marker or run token, not pid alone
- use cooperative stop/cancel through the worker control channel first
- if hard termination is needed, terminate the verified process tree rather than
  relying on registry state
- treat missing or unverifiable processes as `unknown` until completed outputs,
  failure markers, or explicit resume confirms the next state

The exact Windows APIs and Python library choices should be confirmed during
implementation.

### Codex Desktop Integration

Treat hooks as the supported plugin-side integration surface unless a stronger
Codex Desktop API is found during implementation.

Current evidence:

- Codex hooks provide interaction-point context injection and continuation
  support.
- Codex app-server appears to be a separate client/protocol integration surface,
  not clearly a plugin API for Windows Desktop background completion triggers.
- No currently confirmed plugin-side API can start a new assistant response at
  the exact moment an external background run completes while the session is
  idle.

Implementation posture:

- use hooks for prompt-time and stop/continuation status injection
- make `manage_gemini_run` the reliable manual status/progress entry point
- if a Desktop-supported completion trigger is discovered later, add it as an
  integration improvement without changing the core run model

### Placeholder Character Set

Keep placeholder names conservative until a real need appears.

Proposed rule:

- allow Unicode letters and numbers
- allow spaces
- allow `_-.()[]@+=,~`
- reject newlines
- reject `{` and `}`
- reject Windows-reserved path characters: `< > : " / \ | ? *`
- reject empty or whitespace-only names

Substituted values may be more flexible than placeholder names, but path fields
must still validate as absolute paths after substitution.

## User-Level Decisions Remaining

No remaining item in this document is waiting on the user for investigation.
Future user decisions should be limited to product-level tradeoffs, such as
whether the proposed behavior feels right for agent ergonomics, not runtime API
or platform research.
