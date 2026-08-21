# MCP 아키텍처 리팩터링 노트

이 문서는 `call_gemini`와 `manage_gemini_run`으로 전환한 breaking refactor
이후의 gemini-offload MCP run-oriented 아키텍처를 정리한다. 목적은 요청을
구성하는 축, 축 사이의 영향, 최상위 모드로 올리지 말아야 할 개념을 명확히
하는 것이다.

## 목표

- 풍부한 Gemini 요청을 구성할 자유도를 보존한다.
- MCP 추상화를 컨텍스트 최적화 관점에서 균형 있게 유지한다.
- 반복 OCR 및 batch형 작업에서 숨어 있는 컨텍스트 증가를 피한다.
- blocking 실행과 background 실행이 같은 요청 모델을 쓰게 한다.
- plain text와 JSON schema 출력을 모두 1급 선택지로 유지한다.
- `code_execution`은 미래 설계 주제로만 남기고, 이번 리팩터링에서는
  제외한다.

## 구현된 Durable Runtime (0.2.0)

현재 background runtime의 authoritative state는 run root 아래의 SQLite WAL
데이터베이스다. `runs`, `items`, `artifacts`, `events`, `worker_leases`를
관계형 테이블로 저장하며 schema version과 명시적 transaction 경계를 둔다.
각 run의 `status.json`과 `events.jsonl`은 호환/디버그용 export일 뿐 source of
truth가 아니다.

run 상태(`queued`, `starting`, `running`, `stopping`, `canceling`, `completed`,
`failed`, `stopped`, `canceled`)와 item 상태(`pending`, `running`, `completed`,
`failed`, `stopped`, `canceled`)는 명시적 state machine으로 검증된다. 상태
변경과 대응 event는 가능한 경우 같은 SQLite transaction에서 commit된다.

worker ownership은 단조 증가하는 lease generation과 random token으로 fence된다.
worker의 상태/아티팩트 변경 transaction은 같은 fence를 commit 전에 다시
검증한다. heartbeat가 lease를 갱신하고, forced cancel은 terminal cancel을
commit하기 전에 lease를 revoke하므로 stale worker가 나중에 결과를 publish할
수 없다.

자동 관리되는 background output은 `<run_dir>/outputs/` 아래에 갇히며 item
index 기반 storage key를 쓴다. caller item ID는 opaque metadata이며 파일명이
되지 않는다. 사용자가 명시한 absolute `output.path`는 계속 지원하지만 managed
artifact와 구분해서 기록한다.

`completed` item은 기록된 모든 artifact가 regular file로 남아 있고 byte count와
SHA-256이 일치할 때만 resume에서 skip된다. 누락/변조된 artifact는 item을
pending으로 되돌려 다시 실행하고 recovery event를 남긴다. active lease가 없는
stale `starting`/`running`은 `failed`, `stopping`은 `stopped`, `canceling`은
`canceled`로 복구된다.

구현 책임은 다음과 같이 분리되어 있다.

- `run_store.py`: SQLite state, transition, event, lease/fencing.
- `artifacts.py`: managed path confinement, atomic write, hash/validation.
- `output_policy.py`: text/JSON/image spill과 compact return policy.
- `run_service.py`: request materialization과 run/item orchestration.
- `worker.py` / `run_worker.py`: worker process lifecycle과 background entrypoint.
- `server.py`: MCP schema/handler, compact result wrapping, narrow adapter.
- `gemini_client.py` / `keys.py`: Gemini API와 quota/credential.

`run_worker.py`는 MCP transport layer와 결합되지 않도록 `server.py`가 아니라
`worker.py`를 직접 import한다.

## 핵심 모델

중심 단위는 run이다.

run은 하나 이상의 request item을 실행한다. item이 하나인 run도 정상적인
run이며, 별도의 single 모드는 없다. item이 여러 개면 execution 설정에
따라 병렬 실행될 수 있다.

각 request item은 최종적으로 Gemini request envelope로 구체화된다.

- `system`: system instruction. inline text 또는 path-backed text.
- `contents[]`: 순서가 있는 Gemini-style content entry.
- `contents[].parts[]`: text, text path, local file path, future part type.
- `output`: output contract와 result storage 의도.
- `tools`: Google Search 같은 runtime Gemini tool.

## 최상위 축

### Request Materialization

최종 request item envelope를 어떻게 만들지 정하는 축.

- `explicit`: 각 item이 완성된 request envelope를 직접 제공한다.
- `template`: 재사용 가능한 request template과 각 item의 vars를 결합한다.

이 축은 item 개수와 독립적이다. template run은 item 하나만 가질 수 있다.
이 구조는 pilot call, 실패한 chunk 하나 재시도, 40개 OCR chunk 이후 늦게
발견된 "41번째 chunk" 처리에 유용하다.

### Execution Lifecycle

MCP 호출이 run 완료와 어떤 관계를 맺는지 정하는 축.

- `blocking`: MCP 호출이 모든 item 완료까지 기다린다.
- `background`: MCP 호출이 run을 시작하고 즉시 receipt를 반환한다.

background 실행에는 durable result storage와 run bookkeeping이 필요하다.

### Execution Concurrency

request item을 동시에 몇 개 실행할 수 있는지 정하는 축.

- `max_concurrency`: run에 item이 여러 개 있을 때 적용된다.

item이 하나인 run에서는 effective concurrency가 자연스럽게 1이다.
`item_count`는 입력 모드가 아니며, item 배열 길이에서 파생된다.

### Content Model

Gemini context를 어떻게 표현할지 정하는 축.

canonical model은 `prompt/files/history`를 별도 최상위 분류로 두는 것이
아니라, `system`과 순서 있는 `contents[]`를 사용하는 것이다. prompt text,
text file, local file, prior turn은 모두 content graph의 일부다.

현재 `history` 개념은 preloaded content turns로 이해하는 편이 낫다.
에이전트가 Codex 대화 전체를 Gemini에 복사하도록 유도해서는 안 된다.

### Output Contract

모델에게 어떤 형식의 응답을 요구할지 정하는 축.

- `text`: 기본값. OCR, transcription, cleanup, 비정형 multimodal 작업에
  적합한 유연한 출력.
- `json_schema`: exact fields, validation, tabulation, automation이 중요할
  때 쓰는 엄격한 structured output.

이것은 하나의 축이다. plain text와 JSON schema는 같은 축의 상호 배타적인
값이지, 서로 독립된 두 축이 아니다.

### Runtime Tools

generation 중 사용하는 추가 Gemini tool.

- `google_search`: 현재 범위.
- `code_execution`: 미래 계획만 남긴다. 이번 리팩터링 범위에서는 제외한다.

Google Search는 선택한 model/API 조합이 지원한다면 text 또는 JSON schema
output과 결합 가능해야 한다.

### Result Storage

full result body를 어디에 쓸지 정하는 축.

- explicit output path
- output path template
- automatic run/output directory

Result storage는 inline return policy와 별도다. background execution은
storage에 의존하고, blocking execution은 작은 결과를 inline으로 반환할 수
있다.

### Inline Return Policy

즉시 MCP tool response에 result body를 얼마나 넣을지 정하는 축.

blocking run은 작은 result body를 inline으로 반환하고 큰 result는 spill할
수 있다. background run은 final result body가 아니라 receipt/status
information만 반환해야 한다.

## 파생 개념

아래 개념들은 최상위 request mode가 되어서는 안 된다.

- `single`: `items.length == 1`에서 파생된다.
- `batch`: `items.length > 1`, concurrency, lifecycle에서 파생된다.
- `item_count`: 입력이 아니라 result summary다.
- `manifest`: delivery mode가 아니라 run bookkeeping artifact다.
- `history`: content model detail이며 최상위 축이 아니다.
- `prompt`와 `files`: 최상위 축이 아니라 `contents[].parts[]` 안의 part
  type이다.

## 축 간 영향

이 섹션은 서로 다른 축 사이의 영향만 나열한다. 같은 축 안의 상호 배타성은
의도적으로 제외한다.

### Execution Lifecycle -> Result Storage

lifecycle이 `background`이면 full result body는 반드시 durable path에
저장되어야 한다. 최초 MCP response가 result를 담는 유일한 장소가 되어서는
안 된다.

설계 시사점: background run에는 explicit output path, output path template,
또는 server-managed run output directory가 필요하다.

### Execution Lifecycle -> Inline Return Policy

lifecycle이 `background`이면 최종 `text`나 `response_json`을 start response에
inline으로 반환해서는 안 된다. start response는 receipt, run id, status
path 또는 status handle, 가능하다면 manifest path, read guidance를 담아야
한다.

blocking run은 기존 inline/spill policy를 사용할 수 있다.

### Execution Lifecycle -> Run Bookkeeping

background execution에는 최초 tool call 이후에도 유지되는 run state가
필요하다.

- run id
- status
- item statuses
- output paths
- errors
- cancellation state
- timestamps

설계 시사점: bookkeeping은 user-selected delivery mode가 아니라 background
lifecycle에서 파생되는 server responsibility다.

### Request Materialization -> Result Storage

template materialization은 result path와 자연스럽게 결합된다. 반복 OCR item은
item 변수에서 파생된 output path가 필요한 경우가 많다.

설계 시사점: output path template은 유효한 template field여야 하며, 치환된
값은 absolute path여야 한다.

### Request Materialization -> Content Model

template placeholder는 content text, text path, local file path, system path,
output path에 나타날 수 있다.

설계 시사점: placeholder substitution은 단순하고 예측 가능해야 한다. 예를
들어 string field 안의 `{{name}}`만 허용한다. substitution 이후 모든 path는
absolute path로 검증해야 한다.

### Request Materialization -> Context Size

explicit multi-item run은 큰 request envelope를 반복할 수 있다. template run은
공통 context를 하나의 template에 보관하고 item별 vars만 보낼 수 있다.

설계 시사점: explicit materialization은 heterogeneous work를 위해 유지할 수
있지만, 반복 OCR/chunk workflow에는 template materialization을 선호해야 한다.

### Content Model -> Context Size

큰 inline system instruction, text part, prior turn은 MCP payload와 Gemini
context를 증가시킨다. path-backed part는 반복 MCP payload 크기를 줄이고
재사용 prompt를 감사하기 쉽게 만든다.

설계 시사점: path-backed text는 `system`과 `contents[].parts[]`에서 1급으로
지원해야 한다.

### Runtime Tools -> Result Metadata

Google Search는 normalized grounding field를 추가하여 result metadata를
바꾼다. 하지만 그 자체로 requested output body를 바꾸지는 않는다.

설계 시사점: prompt/schema가 source를 명시적으로 요구하지 않는 한,
grounding은 plain text나 JSON schema contract의 일부가 아니라 result envelope
metadata로 남겨야 한다.

### Runtime Tools -> Output Contract

Google Search는 선택된 model/API가 지원한다면 text 및 JSON schema output과
조합 가능해야 한다.

설계 시사점: 지원하지 않는 조합은 run 시작 전에 reject해야 한다. background
execution에서는 시작 후 실패를 대화적으로 수습하기 어렵기 때문에 특히
중요하다.

### Output Contract -> Result Storage And Inline Return

output contract는 저장 및 inline result field의 형태를 바꾼다.

- text output은 raw text를 저장하고 `text`를 inline할 수 있다.
- JSON schema output은 raw JSON text를 저장하고 parsed `response_json`을
  inline할 수 있다.
- JSON parse failure는 raw text와 `response_json_error`를 보존해야 한다.

설계 시사점: storage와 inline policy는 normalized result envelope 위에서
동작하되, output-contract-specific field name을 존중해야 한다.

### Item Array Length -> Concurrency

run에 item이 하나라면 `max_concurrency`는 실질적인 효과가 없다. item이 여러
개라면 `max_concurrency`가 run 내부 fan-out을 제어한다.

설계 시사점: 별도의 single/set mode는 필요 없다.

### Item Array Length -> Run Bookkeeping

multi-item run에는 blocking이어도 item result index가 필요하다. background
run은 항상 bookkeeping이 필요하다. blocking one-item run은 persisted manifest가
필요하지 않을 수 있다.

설계 시사점: manifest-like artifact는 item count와 lifecycle에서 파생되어야
한다.

### Item Array Length -> Inline Return Policy

blocking multi-item run은 item 각각이 작아도 전체 inline budget을 초과할 수
있다.

설계 시사점: blocking multi-item run에는 aggregate inline budgeting이 여전히
필요하다. background run은 final-body inline return 자체를 피해야 한다.

## 확정된 설계 결정

아래 결정은 첫 Q&A pass에서 나왔다.

### Materialization Shape

`explicit`과 `template`은 하나의 `source.type` 값으로 노출하지 않고, 서로
다른 top-level input shape로 노출한다.

이유: 두 shape는 에이전트에게 서로 다른 사고 방식을 요구한다. explicit
run은 item마다 완성된 request envelope를 제공한다. template run은 하나의
재사용 envelope와 item별 placeholder value를 제공한다. 둘을 하나의
discriminated object 아래에 합치면 차이가 흐려지고 validation도 덜 명확해질
수 있다.

두 shape는 같은 primary tool인 `call_gemini`에서 받아야 한다.

### Lifecycle Parameter

blocking과 background execution은 별도 tool name이 아니라 tool parameter로
선택한다.

return shape는 lifecycle에 따라 달라진다.

- blocking은 completed run result를 반환한다. inline/spill policy가 적용된다.
- background는 hardcoded receipt와 durable path를 반환한다. 예: background
  run이 시작되었고, 결과는 반환된 path 아래에 기록되며, 진행 상황은 Codex
  hooks로 surface되고, 수동 status inspection은 appended registry log를
  확인하라는 안내.

primary tool name은 `call_gemini`다.

### Run Management Tool

status, progress, cancel, stop, resume은 여러 개의 별도 tool이 아니라 하나의
management tool에서 처리해야 한다.

management tool의 이름은 `manage_gemini_run`이어야 한다.

별도 command-object shape보다 `action` parameter를 사용한다. MCP tool schema는
하나의 안정적인 shape와 action enum을 가질 때 에이전트가 더 쉽게 발견하고
사용할 수 있다.

- `list`
- `status`
- `progress`
- `cancel`
- `stop`
- `resume`

이 management tool은 process liveness 질문에 답하거나 실행 중인 process를
제어할 때 runtime state를 직접 확인해야 한다. static registry file은 유용한
history지만, live process state에 대한 authority는 아니다.

`stop`과 `cancel`은 서로 다른 의미여야 한다.

- `stop`: run이 나중에 resume될 수 있도록, 보통 current item 이후에
  cooperative하게 멈추도록 요청한다.
- `cancel`: run을 intentional termination으로 표시하고, 가능한 한 빨리 작업을
  중단한다.
- `resume`: durable plan과 completed-item record가 허용하는 경우, stopped,
  interrupted, unknown, partially complete run의 실행을 시작하거나 다시
  연결한다.

### Live Runtime State

durable lifecycle state는 SQLite에서 온다. OS process inspection은 liveness와
process control을 위한 supporting evidence이며 별도의 state authority가 아니다.
`manage_gemini_run list/status/progress`는 durable store를 조회하고, status/control
응답은 필요할 때 `locator.json`의 PID/create-time/run token을 OS process table과
대조한다.

active worker는 `(run_id, generation, token)` SQLite lease를 소유하고 heartbeat로
expiry를 갱신한다. generation은 단조 증가하므로 run이 reclaim되면 모든 이전
worker가 fence된다. stale worker가 이미 진행 중이던 Gemini 요청을 끝낼 수는
있지만 이후 state나 artifact를 commit할 수는 없다.

### Background Durable Store와 Compatibility Export

authoritative registry는 run root의 `.gemini-offload-runs.sqlite3`이며 SQLite WAL
mode를 사용한다. run/item의 query-critical state, artifact integrity metadata,
event, worker lease를 저장하고 상태 변경과 대응 event를 transaction으로 묶는다.

각 run directory의 `plan.json`, `status.json`, `events.jsonl`, `locator.json`,
`control/`, `outputs/`는 계속 유지된다. 이 중 `status.json`과 `events.jsonl`은
compatibility/audit/debug export이므로 SQLite와 불일치할 때 authority로 사용하면
안 된다. cursorable event 조회는 `manage_gemini_run progress`를 사용한다.

control semantics는 다음과 같다.

- `stop`: `stopping`을 거쳐 `stopped`로 가며 resume 가능하다.
- `cancel`: `canceling`을 거쳐 `canceled`로 간다. forced cancel은 terminal state
  commit 전에 lease를 revoke한다.
- `resume`: 기존 control file을 지우고 `starting`으로 전이한 뒤 새 lease
  generation을 획득하며 non-terminal 또는 artifact-invalid item만 다시 실행한다.

startup recovery는 durable state와 lease를 함께 사용한다. active lease가 없는
stale `starting`/`running`은 `failed`, `stopping`은 `stopped`, `canceling`은
`canceled`로 정리된다.

### Codex Hooks

Hooks는 active background run을 surface해야 하지만 worker가 되어서는 안 된다.
execution과 registry update는 MCP server가 소유한다.

원하는 hook behavior:

- 관련 있을 때 active run progress를 prompt에 주입한다.
- Codex가 이미 한 turn을 처리 중이라면 compact progress 또는 completion
  context로 다음 model step을 steer한다.
- Codex가 idle 상태일 때 run이 완료된다면, Codex가 그 flow를 지원하는 경우
  새 prompt를 trigger한다.
- full result file이나 full registry를 context로 읽지 않는다.

현재 Codex hook docs는 다음 관련 capability를 보여 준다.

- `PostToolUse`는 MCP tool result를 볼 수 있고, tool call 이후 model-visible
  context를 추가할 수 있다.
- `UserPromptSubmit`과 `SessionStart`는 extra developer context를 추가할 수
  있다.
- `Stop`은 continuation prompt를 만들어 Codex가 계속 진행하도록 요청할 수
  있다.
- plugin-bundled hook이 지원된다.
- 현재는 command hook이 지원되는 handler type이다. async command hook은
  parse되지만 skip된다.

docs에는 임의의 background process가 완료되는 정확한 순간에 idle 상태의
session에서 새 assistant response를 시작하는 일반적인 idle-time external
trigger가 보이지 않는다. 따라서 신뢰할 수 있는 설계는 hooks를 context
injection과 continuation point에 사용하고, MCP server와 management tool을
authoritative background run system으로 유지해야 한다.

Reference: <https://developers.openai.com/codex/hooks>

Codex app-server documentation은 turn을 시작하고, active turn을 steer하고,
item을 inject하고, notification을 stream할 수 있는 별도 protocol을 보여 준다.
이것은 custom client에는 유망하지만, 임의의 external worker가 끝났을 때
Codex Desktop for Windows가 새 response를 시작하게 만드는 plugin hook API와
같은 것은 아니다.

References:

- <https://developers.openai.com/codex/hooks>
- <https://developers.openai.com/codex/app-server>

### Placeholder Rules

placeholder scope는 run 단위다. 어떤 run에서 정의된 placeholder도 다른 run에
영향을 주면 안 된다.

syntax 방향:

- placeholder delimiter는 `{{name}}`처럼 brace를 사용한다.
- placeholder name에는 newline이 들어가면 안 된다.
- placeholder name에는 `{` 또는 `}`가 들어가면 안 된다.
- placeholder name에는 Unicode letters and numbers, Korean text, ASCII letters
  and numbers, space, 그리고 다음 conservative punctuation set을 허용한다:
  `_-.()[]@+=,~`
- substitution 이후 path field는 여전히 absolute-path validation을 통과해야
  한다.

punctuation set은 "Windows가 허용하는 모든 것"보다 의도적으로 좁다. Windows
file name에는 reserved character와 reserved name이 있으며, trailing space나
period도 문제가 된다. placeholder name은 literal file name이 아니라
identifier이므로, substituted value가 더 유연하더라도 예측 가능하게 유지해야
한다.

Reference: <https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file>

### Breaking Change

새 run-oriented tool surface는 현재 `gemini_generate`/`gemini_generate_batch`
surface를 대체해야 한다. 이번 리팩터링에서 compatibility tool 유지는 목표가
아니다.

이것은 의도적인 breaking change다.

## Public Shape

`call_gemini`는 explicit request items 또는 template plus per-item vars를
받는다.

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

referenced template은 다음과 같은 request envelope로 materialize될 수 있다.

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

## 구현 후 정리

초기 설계 단계에서 열려 있던 runtime 질문은 0.2.0 구현에서 다음과 같이
정리되었다.

- background 실행은 child worker process를 사용한다.
- worker identity는 PID/create time/run token과 SQLite lease fence로 검증한다.
- hard cancel은 검증된 process tree를 종료하고 lease를 revoke한다.
- JSONL registry 대신 SQLite WAL이 authoritative state store가 되었고 JSONL은
  audit/debug compatibility export로 남았다.
- Codex hook은 optional context-injection helper이며 worker나 state authority가
  아니다.
- 구현 과정의 세부 발견, 타협, 검증 결과는 `IMPLEMENTATION_LOG.md`에 기록한다.

이 문서에서 사용자의 추가 조사를 기다리는 runtime 설계 질문은 없다.
