# Gemini Offload Setup and Model Support Update - Implementation Log

Append-only implementation journal. The frozen plan is
`SETUP_MODEL_IMPLEMENTATION_PLAN.md`; do not rewrite earlier log entries.

## 2026-08-21 - Workspace and frozen-plan checkpoint

- Dedicated worktree:
  `C:\Users\PSW\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\WebGPTGitWriter\state\scratch\gemini-offload-setup-model-update\gemini-offload`
- Dedicated branch: `feat/setup-model-registry`.
- Base commit: `302ec94295ae6c5d175526a10c5b5b0e47e6a2fc`
  (`Harden durable Gemini offload runs`).
- The original `main` worktree remains separate and is not the implementation
  workspace for this update.
- No source, test, package, plugin, or documentation implementation changes have
  been made in the new worktree at this checkpoint.

### Frozen plan identity

- File: `SETUP_MODEL_IMPLEMENTATION_PLAN.md`
- Lines: 324
- Bytes: 15,952
- Initial mtime: `2026-08-21T17:16:37.0566341+09:00`
- SHA-256: `0EC868FE7D4243683DFA715ECFD41E717411AE6694ABE4993E0E2594E2F4F7D8`
- The plan is now frozen. All later discoveries, compromises, corrections,
  progress, and validation evidence must be appended to this log instead.

### Confirmed implementation scope

- Compact no-argument setup diagnostic for Vertex credentials and local paths.
- Read-only behavior with sanitized structured output and concise repair guidance.
- Optional cooperation with any filesystem-capable agent tool, with no explicit
  dependency on Desktop Commander or another product.
- Long-term registered-artifact retrieval recorded as roadmap only.
- Curated model capability registry intended to reduce maintainer update cost,
  not to permit arbitrary user-configured models.
- Current-model research and bounded Vertex contract probes before allowlist
  changes.
- Internal explicit safety-filter policy, subject to current SDK/API verification;
  no public safety knob.
- Existing `call_gemini`/`manage_gemini_run` topology and narrow generation
  controls remain unchanged.
- Installer-level persistent run-root configuration; core fallback compatibility
  is preserved.

### Next checkpoint

Phase 0 only: re-read current source and tests, capture the test/runtime baseline,
and research current official Vertex/Gemini behavior. Do not begin behavioral
implementation until that evidence has been logged.

## 2026-08-21 - Phase 0 baseline and evidence capture complete

- Worktree/branch remain `feat/setup-model-registry` at base commit
  `302ec94295ae6c5d175526a10c5b5b0e47e6a2fc`; only the frozen plan and this
  append-only log were untracked before implementation.
- Runtime baseline: Python 3.10.11, `google-genai` 2.19.0, `google-auth` 2.56.3,
  MCP 2.0.0, httpx 0.28.1.
- Full baseline suite: 102/102 tests pass in 2.439s.
- Re-read credential manifest resolution/loading, installer behavior, MCP tool
  definitions, model allowlist/specs, media-resolution validation, thinking
  config, structured output, Google Search, and GenerateContent construction.

### Phase 0 official-source findings (checked 2026-08-21)

- Google Cloud authentication docs confirm service-account JSON credentials are
  a supported explicit credential form; externally sourced credential files
  should be validated before use.
- Current Gemini docs show `gemini-3.7-flash` GA (updated 2026-08-13), with
  text/image/video/audio/PDF input, text output, Search grounding, structured
  output, and low/medium/high thinking. Current release guidance also lists
  Gemini 3.6 Flash and 3.5 Flash-Lite as GA.
- Current thinking docs confirm Gemini 3 models reason by default and expose
  model-specific thinking levels; `include_thoughts` is not equivalent to
  enabling reasoning.
- Current media-resolution docs confirm per-content resolution is Gemini-3-only
  and `ultra_high` is not available for PDF/video.

## 2026-08-21 - Phase 1 compact setup diagnostic initial implementation

- Added `mcp_server/setup_check.py` as a separate read-only diagnostic module.
- Added `check_gemini_setup` MCP tool with an empty input schema and compact
  output contract.
- The diagnostic intentionally does not initialize the shared Vertex credential
  rotator, so checking setup does not mutate cooldown or round-robin state.
- It resolves the existing manifest precedence, parses credential entries, checks
  referenced service-account JSON files, constructs scoped credentials, and
  returns sanitized setup status.
- Updated MCP runtime tests for the additional tool surface.
- Validation after initial Phase 1 wiring: existing suite 102/102 passes.

### Phase 1 follow-up tasks

- Add dedicated setup diagnostic tests for missing/malformed manifests,
  credential parsing failures, secret redaction, and bounded output shape.
- Revisit token refresh depth and network-unverified state handling before calling
  the diagnostic implementation complete.

## 2026-08-21 - Phase 1 setup diagnostic checkpoint

- Added `check_gemini_setup` as a compact no-argument MCP diagnostic tool.
- Added `mcp_server/setup_check.py`; it does not initialize the process-global Vertex credential rotator and therefore does not alter quota cooldown or round-robin state.
- Diagnostic output is structured for agent recovery: manifest source/path, Vertex location, credential count, compact credential statuses, run/output roots, temporary-root hints, and concise next action.
- Credential status semantics were corrected: local JSON parsing alone is not reported as verified. The diagnostic now distinguishes verified OAuth refresh, invalid credential rejection, and unverified network/token-endpoint availability.
- OAuth refresh uses a short bounded transport path; it performs no Gemini inference and never returns private key material, tokens, or service-account JSON.
- Updated MCP runtime tests for the fifth tool and preserved MCP 1.x/2.x compatibility.
- Validation: full existing suite remains green (102/102); focused MCP/server tests pass (48/48); setup/server modules compile successfully.

Next checkpoint: add dedicated setup diagnostic regression coverage for missing/malformed manifests, credential validation states, privacy guarantees, and compact tool-surface constraints before considering Phase 1 complete.

## Phase 1 setup diagnostic validation checkpoint

- Added dedicated `tests/test_setup_check.py` coverage.
- Covered missing manifest, malformed manifest, missing key file, invalid credential JSON, successful credential refresh path, network-unverified refresh behavior, and secret leakage prevention.
- Corrected an implementation issue found during test design: local service-account parsing is not sufficient evidence for `verified`. The diagnostic now distinguishes OAuth refresh verification from local parsing success.
- Corrected credential construction to use the same Vertex scope and quota-project behavior as normal runtime credential loading.
- Prevented setup diagnostics from exposing `client_email` fallback identifiers or credential material; only configured display names are returned.
- Focused setup diagnostic tests: 7/7 pass.
- A test invocation using system Python failed to import MCP because the isolated project environment was not active in this worktree shell. This is an environment invocation issue, not a code failure; rerun the full suite with the repository dependency environment before Phase 1 closure.

## 2026-08-22 - Phase 1 compact setup diagnostic complete

- Added the read-only `check_gemini_setup` MCP tool with an empty input schema and a short description.
- Setup inspection does not initialize the shared credential rotator or mutate quota/cooldown state.
- Diagnostic output reports manifest source/path, Vertex location, compact per-credential status, run/output roots, temporary-root flags, and one concise next action.
- Credential validation now distinguishes local structure from real authentication: scoped service-account construction plus bounded OAuth refresh is required for `verified`.
- OAuth/network transport failures are `unverified`; known malformed/rejected credentials are `invalid`. Known invalid entries take precedence in aggregate status.
- Runtime-equivalent credential construction uses the Vertex cloud-platform scope and project quota binding.
- Privacy hardening removes client-email fallback display names and never returns private keys, access tokens, refresh payloads, or raw service-account JSON.
- OAuth transport caps each token-endpoint request at 3 seconds.
- Added dedicated setup regressions for missing/malformed manifests, missing/invalid credential JSON, verified refresh, network-unverified behavior, mixed invalid/unverified status, timeout bounding, and secret non-disclosure.
- Added MCP regressions proving exactly one new tool, exact empty input schema, description <= 64 characters, serialized tool definition <= 512 bytes, and normal `ready:false` structured results for setup failures.
- Focused setup/MCP tests pass; full suite is 113/113 green. Compileall and `git diff --check` pass.
- Real MCP 2.0 stdio smoke exposes exactly five tools and returns a normal structured missing-manifest setup result without inference.
- The feature worktree has no local `.venv`; validation reused the previously verified repository venv by absolute interpreter path while executing from this worktree, so local source remained first on `sys.path`.
- Frozen plan remains unchanged at SHA-256 `0EC868FE7D4243683DFA715ECFD41E717411AE6694ABE4993E0E2594E2F4F7D8`.

Next checkpoint: Phase 2 credential naming and setup internals. Introduce precise Vertex credential names while retaining compatibility aliases and unchanged manifest precedence.

## 2026-08-22 - Phase 2 reorientation checkpoint

- Re-read the frozen plan, full append-only log, current `keys.py`, and Phase 1 setup diagnostic before resuming.
- Phase 2 is intentionally narrow: canonicalize internal Vertex credential naming and centralize setup/runtime manifest resolution without changing credential behavior, manifest precedence, quota rotation, or public MCP schemas.
- Current production modules still consume legacy names such as `get_key_count`, `ApiKeyLease`, and `DEFAULT_KEY_COOLDOWN_SECONDS`; these will move to precise canonical names while legacy symbols remain compatibility aliases.
- Found a maintainability drift risk: `setup_check.py` independently reproduces manifest-source precedence. Phase 2 will make setup inspection consume the same resolver source used by runtime loading.
- Frozen plan hash before Phase 2 edits remains `0EC868FE7D4243683DFA715ECFD41E717411AE6694ABE4993E0E2594E2F4F7D8`.

## 2026-08-22 - Phase 2 credential naming checkpoint

- Introduced canonical internal names: `get_vertex_credential_rotator`, `get_next_vertex_credential_lease`, `get_vertex_credential_count`, and `get_vertex_quota_slot_count`.
- Retained `get_key_rotator`, `get_next_api_key_lease`, `get_key_count`, and related legacy names as compatibility aliases; no public MCP contract changed.
- Added canonical `DEFAULT_VERTEX_CREDENTIAL_COOLDOWN_SECONDS` while retaining `DEFAULT_KEY_COOLDOWN_SECONDS` as a compatibility alias.
- Centralized manifest source resolution through `resolve_vertex_manifest_info()` in `keys.py`; `setup_check.py` now consumes the same resolver rather than duplicating environment precedence logic.
- Preserved manifest precedence: `GEMINI_OFFLOAD_VERTEX_CREDENTIALS`, then `VERTEX_AI_CREDENTIALS`, then the default manifest path.
- Initial full test run exposed an expected compatibility issue: setup tests patched `setup_check.DEFAULT_VERTEX_MANIFEST` and legacy env constants directly. Restored those compatibility exports without reintroducing resolver duplication.
- Validation: 113/113 tests pass; compileall passes; `git diff --check` passes.

Next checkpoint: complete remaining Phase 2 cleanup by moving touched runtime consumers to canonical credential names where churn is low, then proceed to Phase 3 internal safety policy.

## 2026-08-22 - Phase 2 credential naming and setup internals complete

- Added canonical credential APIs: `get_vertex_credential_rotator`, `get_next_vertex_credential_lease`, `get_vertex_credential_count`, `get_vertex_quota_slot_count`, and `mark_vertex_credential_cooldown`.
- Added canonical `DEFAULT_VERTEX_CREDENTIAL_COOLDOWN_SECONDS` and rotator methods `credential_count` / `mark_credential_cooldown`.
- Existing `get_key_*`, `ApiKeyLease`, `ApiKeyRotator`, `DEFAULT_KEY_COOLDOWN_SECONDS`, and `mark_key_cooldown` symbols remain compatibility aliases.
- Updated production consumers in `gemini_client.py` and `run_service.py` to use canonical Vertex credential naming.
- Added `resolve_vertex_manifest_info()` as the shared non-loading manifest resolver; setup diagnostics now consume the same precedence source as runtime loading.
- Preserved setup-module compatibility exports used by existing tests while removing duplicated precedence logic.
- Added regressions for manifest environment precedence and canonical/legacy alias parity.
- Focused credential/setup/client tests pass 37/37; full suite passes 115/115. Compileall and `git diff --check` pass.
- Frozen plan remains unchanged at SHA-256 `0EC868FE7D4243683DFA715ECFD41E717411AE6694ABE4993E0E2594E2F4F7D8`.

Next checkpoint: Phase 3 internal safety policy. Verify explicit `OFF` support in current Vertex documentation and installed Google Gen AI SDK, then centralize the internal safety configuration without growing MCP schemas.

## 2026-08-22 - Phase 3 internal safety policy complete

- Current official Gemini/Vertex safety documentation confirms `HarmBlockThreshold.OFF` is supported for the four configurable text harm categories and that Gemini 2.5/3 models default these additional filters off.
- Installed `google-genai` 2.19.0 constructs and serializes `SafetySetting(..., threshold=OFF)` through `GenerateContentConfig.safety_settings` as expected.
- Added one internal `_default_safety_settings()` path covering dangerous content, harassment, hate speech, and sexually explicit content.
- Every currently supported model receives the same explicit `OFF` settings through `_call_api`; the policy is not user-configurable.
- Added regression coverage across every supported model and explicit checks that `call_gemini` exposes no safety field.
- A live inference probe was not used because current official Vertex documentation plus installed-SDK object serialization fully establishes the request contract without credential, quota, or billable inference dependence.
- Full suite passes 116/116. Compileall, `git diff --check`, and direct public-tool-schema inspection pass.
- Frozen plan remains unchanged at SHA-256 `0EC868FE7D4243683DFA715ECFD41E717411AE6694ABE4993E0E2594E2F4F7D8`.

Next checkpoint: Phase 4 curated model capability registry and capability-driven preflight validation.

## 2026-08-22 - Phase 4 capability validation checkpoint complete

- Connected model registry capability data to request validation instead of leaving it as discovery-only metadata.
- Added shared `validate_request_capabilities()` preflight path covering safety policy, thought summaries, Google Search, JSON schema, input modalities, and media resolution compatibility.
- Kept product defaults unchanged; capability support and workload policy remain separate concepts.
- Added model-aware media resolution checks and input modality checks before API calls.
- Exposed model capability data through `list_gemini_models` while preserving legacy `models` and `model_characteristics` fields.
- Promoted `normalize_model_sequence` as canonical naming while keeping the old internal alias.
- Validation: focused registry/client/server tests pass (26/26); full suite passes (119/119); compileall and `git diff --check` pass.

Phase 4 registry foundation is now complete. Remaining work follows the frozen plan: current model research/refresh with Vertex contract evidence, then documentation/skill/installer synchronization.

## 2026-08-22 - Phase 5 model refresh investigation started

- Began model refresh with registry-first policy: runtime discovery is evidence only and does not automatically authorize new models.
- Verified current local Vertex credential setup is usable: setup diagnostic returned `ready=true`, `status=verified`, `location=global`.
- Vertex `models.get()` metadata probe succeeded for:
  - `gemini-3.7-flash`
  - `gemini-3.6-flash`
  - `gemini-3.5-flash`
  - `gemini-3.1-pro-preview`
  - `gemini-3-flash-preview`
- Metadata probe confirms model identifiers exist in the current Vertex environment, but does not replace capability contract validation.
- Began extending registry release-stage vocabulary with `deprecated` to support safe removal/migration states without deleting historical entries immediately.
- No new model has been added to the supported allowlist yet. Candidate additions require capability verification before registry promotion.

## 2026-08-22 - Phase 5 bounded Vertex contract probes authorized by implementation plan

- Candidate models: `gemini-3.7-flash` and `gemini-3.6-flash`.
- Both are documented GA models and resolve through this project's verified Vertex service-account credentials at location `global` via `models.get()`.
- Live billable scope is intentionally tiny: at most four short GenerateContent calls per candidate, covering (1) text + thought summary + JSON schema + explicit safety OFF, (2) inline image + per-part media resolution, (3) inline PDF + per-part media resolution, and (4) Google Search grounding.
- Prompts and media are synthetic/minimal; no user content is sent. Stop probing a feature after a decisive failure.
- These probes establish endpoint compatibility for the public gemini-offload contract only; they are not quality benchmarks.

## 2026-08-22 - Phase 1 implementation started

- Phase 0 baseline gate passed; started compact setup diagnostic implementation.
- Added initial `mcp_server/setup_check.py` as a read-only diagnostic boundary.
- Connected the new `check_gemini_setup` tool surface in the MCP server using an
  empty input schema and compact output contract.
- Existing server branch already contained some model-capability work from the
  current baseline; avoid duplicating that path until the model registry phase.
- Phase 1 remains incomplete: credential validation depth, tests, schema-size
  regression, secret redaction review, and full validation gates are still pending.

- Ran bounded Vertex GenerateContent probes for candidate models `gemini-3.7-flash` and `gemini-3.6-flash` using the verified local credential.
- Results: both models passed text + structured JSON output + thought summary + explicit safety OFF; both passed minimal PDF high-resolution input.
- Image probe returned INVALID_ARGUMENT `Provided image is not valid`; this is treated as invalid probe fixture, not a model capability failure, because the generated PNG sample was not accepted by the API.
- No model registry mutation has been made yet. Existing allowlist/defaults remain unchanged pending the product decision of replacing/deprecating preview Flash entries.

## 2026-08-22 - Phase 5 candidate contract probe continuation

- Previous image probe failure was not treated as a model failure because the fixture itself was rejected as invalid image input.
- Replaced the image fixture with a valid generated PNG and reran candidate probes.
- `gemini-3.7-flash`: valid image high resolution succeeded; Google Search grounding succeeded with grounding metadata.
- `gemini-3.6-flash`: valid image high resolution succeeded; Google Search grounding succeeded with grounding metadata.
- Combined with earlier probes: both candidates pass text+JSON schema+thought summary+safety OFF and PDF high checks.
- Current evidence is sufficient to classify both models as capability candidates for registry addition. Default model/fallback ordering and removal of existing models remain a separate product decision and are not changed yet.

## 2026-08-22 - Phase 5 refreshed Flash registry and regression recovery

- Added verified stable registry entries for `gemini-3.7-flash` and `gemini-3.6-flash`; no default or fallback policy was changed.
- Corrected `gemini-3.5-flash` release stage from preview to stable based on current official model status.
- `gemini-3.7-flash` records `low/medium/high` thinking levels; `gemini-3.6-flash` records `minimal/low/medium/high`.
- Both new entries are marked Vertex-location supported because the configured `global` Vertex endpoint was verified by metadata and bounded live contract probes.
- Added optional `replacement_model` metadata to the registry representation for future deprecation guidance, but did not assign a replacement to `gemini-3-flash-preview`: current official migration material is not sufficiently unambiguous to hard-code one yet.
- Added registry regressions for refreshed Flash release stage, thinking levels, Vertex availability, Search/JSON support, image/PDF input, and media-resolution support.
- During the full-suite gate, seven setup-diagnostic tests exposed that `setup_check.py` in the active worktree had regressed to an earlier shallow `configured => verified` implementation. Restored the intended Phase 1 behavior: per-file JSON validation, scoped service-account construction, bounded OAuth refresh, verified/invalid/unverified aggregation, sanitized names/errors, and shared manifest precedence.
- Setup + registry focused tests are 13/13 green; full suite is now 120/120 green. Compileall and `git diff --check` pass.
- Temporary Phase 5 probe scripts were removed after their evidence was recorded.
- Frozen plan remains unchanged at SHA-256 `0EC868FE7D4243683DFA715ECFD41E717411AE6694ABE4993E0E2594E2F4F7D8`.

## 2026-08-22 - Phase 5 model policy finalized

- Product decision: `gemini-3.7-flash` is the default model; `gemini-3.1-pro-preview` remains the quality-first option.
- `gemini-3.6-flash` and `gemini-3.5-flash` are retained only as explicit 429 rate-limit fallback choices, in that order, and are not recommended for normal operation.
- Removed `gemini-3-flash-preview` from the curated registry and added a direct removed-model error pointing callers to `gemini-3.7-flash`.
- Added machine-readable `selection_role` metadata: `default`, `quality`, and `rate_limit_fallback`.
- Rate-limit error guidance now advertises only the fallback-role models instead of every supported model.
- Updated README and root/plugin workflow skill guidance to match the finalized model policy; root/plugin skill files remain byte-identical.
- Focused policy tests passed 29/29. Full suite passed 122/122; `compileall` and `git diff --check` passed; skill parity passed.
- No commit, push, or main-worktree mutation was performed.

## 2026-08-22 - Phase 6 installer, docs, and skill complete

- Added installer `RunDir` with persistent Windows default `%LOCALAPPDATA%/gemini-offload/runs` and emitted `GEMINI_OFFLOAD_RUN_DIR` in the generated MCP config; the core server's temp fallback remains unchanged for manual setups.
- Installer smoke with an isolated LocalAppData value exited successfully and emitted the expected persistent run-root path.
- `check_gemini_setup` now reports `run_root_temporary` and recommends configuring a durable run root when credentials are ready but no run root is configured.
- Fixed the MCP setup handler to call `check_gemini_setup` via `anyio.to_thread.run_sync`, keeping bounded OAuth refresh off the event loop.
- Restored a real google-auth-compatible HTTP response adapter for OAuth refresh; network failures and HTTP 429/5xx are treated as transport-unverified rather than invalid credentials.
- A real service-account OAuth refresh using the configured default manifest succeeded after the adapter restoration (`ready=true`, `status=verified`, one credential, global location).
- README, English/Korean architecture notes, and workflow skill now document setup-check use, curated model policy, internal safety OFF wording, persistent run-root behavior, generic filesystem companion use, and the artifact-retrieval roadmap.
- Root and bundled workflow skills were re-mirrored and remain byte-identical.

## 2026-08-22 - Final validation audit before release decision

- Final full test suite passed 125/125; `compileall`, fatal Ruff selectors (`E9,F63,F7,F82`), Bandit medium/high scan, and `git diff --check` all passed.
- Final isolated PEP 517 wheel build succeeded as `gemini_offload_mcp-0.2.0-py3-none-any.whl` (validation artifact removed afterward).
- Real stdio smoke succeeded on installed MCP 2.0 and on an isolated MCP 1.29.0 environment: both exposed exactly five tools, returned `gemini-3.7-flash` first with the old Flash preview absent, and returned a normal structured missing-manifest setup result without inference.
- Product-source scan found zero personal machine-path hits and zero real credential/private-key markers. The only local-path match in the complete changed-file set is one historical worktree path in an earlier append-only log entry; it is intentionally left unchanged to preserve the log invariant. The `ya29.SUPER-SECRET-TOKEN` string in setup tests is an explicit fake leak-detection sentinel.
- Root/plugin workflow skill parity is byte-identical. Package/server/plugin version surfaces remain mutually consistent at `0.2.0` pending the release-version decision.
- Frozen plan SHA-256 remains `0EC868FE7D4243683DFA715ECFD41E717411AE6694ABE4993E0E2594E2F4F7D8`.
- Temporary MCP environments, smoke scripts, test logs, wheel output, and bytecode caches were removed from the worktree before status review.
- No commit, push, or main-worktree mutation has been performed.

## 2026-08-22 - Release candidate 0.3.0
- Bumped package, MCP server, and Codex plugin versions from 0.2.0 to 0.3.0 after completing the setup/model-registry feature set.
- Updated current architecture headings to identify the implemented runtime as 0.3.0 while preserving historical 0.2.0 notes where they describe the earlier refactor.
- Final full suite after the version bump: 125/125 tests pass; compileall and git diff --check pass; package/server/plugin version parity tests pass.
- Final isolated PEP 517 wheel build succeeds as `gemini_offload_mcp-0.3.0-py3-none-any.whl` with SHA-256 `CCCC6F82B8918549FE68EE056038424E3C45125F88397C3FBA9BA6C966D3647A`.
- Frozen plan remains unchanged at SHA-256 `0EC868FE7D4243683DFA715ECFD41E717411AE6694ABE4993E0E2594E2F4F7D8`.
- Release candidate is ready for feature-branch commit and push; no main-worktree mutation has been performed.
