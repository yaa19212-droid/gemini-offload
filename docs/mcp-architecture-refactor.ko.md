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

management tool은 registry log에서 liveness를 추론하지 말고 live runtime
state를 inspect해야 한다.

선호하는 architecture:

- `call_gemini`는 각 background run을 supervised worker process로 시작하거나,
  동등한 live handle을 가진 supervised task로 시작한다.
- run directory는 `run_id`, worker pid, worker start token, control channel
  path, plan path, output directory 같은 durable locator data를 저장한다.
- MCP server가 아직 live worker handle을 소유하고 있다면 `manage_gemini_run`은
  그 handle을 확인한다.
- MCP server가 restart되었다면 `manage_gemini_run`은 locator data를 사용해 OS
  process table을 inspect하고, run token 또는 command line으로 worker identity를
  검증한다.
- heartbeat file 또는 status snapshot은 active, waiting, stale, unknown state를
  구분하는 데 도움을 줄 수 있지만, sole authority가 아니라 supporting
  evidence다.

가능한 live status:

- `running`
- `waiting_rate_limit`
- `stopping`
- `canceling`
- `stopped`
- `completed`
- `failed`
- `unknown`

`unknown`은 durable file상으로는 run이 존재해야 하지만 management tool이 현재
liveness를 증명할 수 없다는 뜻이다. 예시는 missing process, stale heartbeat,
matching worker handle 없이 restart된 MCP server, identity를 검증할 수 없는
worker process다.

### Background Registry

background run에는 disk에 남는 append-friendly registry가 필요하다. registry는
에이전트가 매번 전체 history를 다시 읽지 않고 새로 append된 내용만 확인할
수 있어야 한다.

최소 registry content:

- run id
- lifecycle
- start timestamp
- latest status timestamp
- item ids
- output paths
- manifest path 또는 run directory
- append-only progress events
- timestamp, item id, error type, message를 포함한 error events
- completion events
- cancellation events

registry를 process liveness의 source of truth로 취급해서는 안 된다. process
liveness는 runtime state이며 직접 확인해야 한다. 마찬가지로 cancel, stop,
resume은 runtime control이다. static registry file을 읽거나 수정하는 것만으로는
신뢰성 있게 구현할 수 없다.

registry는 append-only event로 관찰된 liveness check와 management action을
기록할 수 있다. 여기에는 unknown state, rate-limit waiting, cancellation
request, stop request, resume attempt가 포함될 수 있다.

registry는 주로 debugging과 audit artifact로 취급해야 한다. Runtime state는
worker handle, OS process inspection, current worker status에서 와야 한다.

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

## 열린 질문

- registry log는 자신이 live state authority인 척하지 않으면서 observed
  liveness check, unknown state, rate-limit waiting, resume attempt를 어떻게
  표현해야 하는가?
- background run은 기본적으로 in-process supervised task, child worker process,
  detached worker process 중 무엇이어야 하는가?
- worker identity verification과 process-tree cancellation의 정확한 Windows
  implementation은 무엇인가?
- Codex app-server는 Codex Desktop plugin on Windows에서 사용할 수 있는가,
  아니면 separate client integration surface일 뿐인가?
- session이 idle 상태일 때 external background run 완료가 새 response를
  trigger할 수 있게 하는 Codex Desktop API가 hooks/app-server 외에 있는가?
