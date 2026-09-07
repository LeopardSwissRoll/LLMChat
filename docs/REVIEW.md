# LLMChat 설계 리뷰

전체 소스(v2 12,286줄 · ExportV2 1,525줄 · Export 1,633줄 · 실험 684줄)를 읽고 쓴 리뷰다. 기능 나열보다 **왜 그렇게 만들었는지, 그 결정이 어디서 값을 하고 어디서 비용을 내는지**에 집중한다. 인용은 `파일::함수` 형식이다.

---

## 1. 형태

```text
                 v2/bridge
 ┌──────────────┬──────────────┬──────────────┬──────────────┐
 │ transport    │ core         │ providers    │ pty          │
 │  3,542줄     │  3,956줄     │  1,243줄     │  672줄       │
 │ discord 3340 │ __init__ 854 │ claude  538  │ handler 533  │
 │ webhook   92 │ web_pub  762 │ codex   523  │ parser  132  │
 │ base      76 │ channel  721 │ base    173  │              │
 │ cli       31 │ turn     634 │              │              │
 │              │ registry 450 │              │              │
 │              │ context  210 │              │              │
 │              │ log      184 │              │              │
 │              │ prompt   141 │              │              │
 └──────────────┴──────────────┴──────────────┴──────────────┘
   + streamer 301 · renderer 178 · web_template 556 · config 478 · models 125
   + main.py 345 (monitor) · cli.py 637 (probe)
```

한 눈에 보이는 불균형 하나: `transport/discord_transport.py` 가 전체의 27% 다. 이건 4절에서 다룬다.

---

## 2. 한 턴의 생애

설계를 평가하려면 먼저 데이터가 어떻게 흐르는지 정확히 알아야 한다.

1. `DiscordTransport.on_message` — 봇이 아닌 **모든** 메시지를 `BridgeCore.log_channel_message` 로 기록한다. 멘션 여부와 무관하다. 이게 나중에 컨텍스트가 된다.
2. `_target_reasons` — 역할 멘션 → 웹훅 메시지 답장 → `@bot` 순으로 대상 페르소나를 결정한다. 여러 역할을 멘션하면 여러 턴이 생긴다.
3. `_handle_message` — **큐에 넣기 전에** 웹훅으로 `processing...` 을 보내고 그 메시지 ID 를 `WebhookTracker` 에 등록한다. 순서가 중요하다. 아직 처리도 시작 안 한 메시지에 다른 사용자가 답장을 걸 수 있어야 하기 때문이다.
4. `BridgeCore.handle_message` → `SessionKey` 별 `asyncio.Queue(maxsize=16)`. 큐가 없으면 워커 태스크와 함께 만든다.
5. `_queue_worker` — 답장 참조와 `@MSG_ID` 토큰을 `_resolve_pipe` 로 해석한다. 대상 로그 파일이 없으면 채널별 `asyncio.Event` 를 기다린다(최대 30분, 10초마다 재확인).
6. `_get_session_op_lock` 아래에서 `SessionRegistry.get_or_create` — 살아있는 세션이 있어도 `_desired_signature` 가 바뀌었으면 죽이고 다시 띄운다.
7. `TurnExecutor.execute` — 컨텍스트 조립 → 전송 → 스트리밍 루프 → 유휴 대기 → 인터랙션 감지 → 추출 → finalize → 로그.
8. `_notify_turn_complete` — 이 채널의 파이프 대기자를 깨운다.

이 흐름에서 눈여겨볼 점은 **3번이 4번보다 먼저**라는 것과, **5번이 세션 락 바깥**에 있다는 것이다. 파이프 대기가 세션을 잡고 있으면 A 가 B 를 기다리고 B 가 A 의 락을 기다리는 교착이 생긴다. 코드는 그걸 피하도록 짜여 있다.

---

## 3. 설계 결정 리뷰

### 3.1 양쪽 끝을 모두 추상화했다

```text
TransportAdapter / OutputSink  ◀──  BridgeCore  ──▶  ProviderAdapter
   (Discord, CLI)                                    (Claude, Codex)
```

`bridge/core/__init__.py` 는 `discord` 를 import 하지 않는다. `transport/base.py::OutputSink` 는 `Protocol` 이고, `cli_transport.py::CliOutputSink` 31줄이 그 계약을 만족한다. 그래서 `cli.py --parsed` 로 Discord 없이 전체 파이프라인을 돌릴 수 있다.

반대쪽 `providers/base.py::ProviderAdapter` 는 훅이 27개다. 많아 보이지만 각각이 실제 차이를 표현한다:

| 훅 | Claude | Codex |
|---|---|---|
| `build_spawn_command` | `--append-system-prompt-file`, `--permission-mode` | 인자 대신 `CODEX_HOME/config.toml` 생성 |
| `build_spawn_env` | 없음 | `CODEX_HOME` 격리 |
| `control_requires_restart("model")` | `False` (`/model` 명령) | `True` (config 재생성) |
| `build_control_inputs("permission")` | `\x1b[Z` × n (shift+tab 순환) | 빈 리스트 (재시작 필요) |
| `resolve_resume_id` | 화면의 `session: UUID` | 화면 → `session_index.jsonl` → 세션 파일 mtime |

**평가**: 좋은 추상화의 기준은 "두 번째 구현이 첫 번째를 뒤틀지 않았는가"다. Codex 는 Claude 와 제어 방식이 근본적으로 다른데(명령 vs 설정 파일) ABC 에 구멍을 내지 않고 들어갔다. `control_requires_restart` 하나가 그 차이를 흡수한다.

**대가**: 훅 27개 중 추상 메서드는 9개, 기본 구현이 있는 것이 18개다. 어떤 훅이 필수이고 어떤 게 선택인지 문서화가 없어 새 provider 를 붙일 때 어디까지 구현해야 하는지 코드를 다 읽어야 안다.

### 3.2 턴과 제어 명령이 같은 큐를 지난다

```python
# core/__init__.py
@dataclass class _TurnQueueJob:    msg, sink
@dataclass class _RuntimeQueueJob: label, action, future
```

`/model`, `/compact`, `reset` 같은 제어 명령도 `_enqueue_runtime_job` 으로 **턴과 같은 큐**에 들어간다. 워커가 둘 다 `_get_session_op_lock` 아래에서 실행한다.

왜 중요한가: PTY 는 한 번에 한 입력만 받는다. 턴이 돌고 있는데 `/compact` 가 끼어들면 CLI 는 그걸 사용자 메시지 중간 텍스트로 받는다. 큐 하나로 직렬화하니 이 문제가 구조적으로 사라진다. `future` 로 결과를 돌려주므로 호출자는 await 만 하면 된다.

`PtySession.run_internal_command` 는 이걸 트랜잭션으로 만든다 — 보내고, 유휴 대기하고, 필요하면 프롬프트 복귀까지 기다리고, **화면을 비운 뒤** 반환한다. 다음 턴이 `/compact` 의 출력 찌꺼기를 응답으로 오인하지 않는다.

### 3.3 edit-in-place 가 파이프 체인을 가능하게 한다

`streamer.py::WebhookStreamer.finalize` 의 주석이 이유를 정확히 말한다:

> By editing instead of delete+create, the message ID stays the same. This is critical for pipe chains — replies to "processing..." resolve correctly because the log is saved under the same ID.

여기서 세 가지가 맞물린다:

1. `processing...` 을 **먼저** 보내서 ID 를 확보한다 (`_handle_message`).
2. 응답이 오면 그 메시지를 **편집**한다. ID 불변.
3. 로그 파일명이 그 ID 다 (`LogStore.save_response`).

그래서 사용자가 `processing...` 에 답장을 걸면, `_resolve_reply_pipe` 가 같은 ID 의 로그 파일을 기다리고, 파일이 생기는 순간 내용을 주입한다. **Discord 메시지 ID 가 곧 퓨처(future)의 핸들**이 된 셈이다.

실패 전파도 같은 메커니즘이다. `_write_failure_log` 가 `[실패]` 마커로 시작하는 파일을 같은 이름으로 쓰면, 대기자는 `PipeSourceFailed` 를 받고 30분 타임아웃 대신 즉시 실패한다. `tests/test_pipe_chain.py` 가 이 세 성질(ID 보존 · 파이프 해석 · 실패 전파)을 검증한다.

**대가**: 웹훅 메시지는 Discord 의 `message_reference` 를 지원하지 않는다. 그래서 답장 UI 로 페르소나를 특정할 수 없고 `WebhookTracker` 가 필요해졌다(3.10).

### 3.4 파일시스템이 통합 버스다

`.bridge/{server}/{channel}/{message_id}.txt` — 첫 줄이 헤더, 나머지가 본문.

```text
[홍길동 @123456]                       ← 사용자 메시지
@[123456] [aris|claude @987654]        ← 봇 응답: 트리거 ID, 페르소나|provider, 봇 ID
```

이 한 형식이 네 소비자를 먹인다:

| 소비자 | 어떻게 |
|---|---|
| 파이프 체인 | `_resolve_pipe` 가 파일 존재를 폴링 |
| 재시작 복원 | `ChannelManager.hydrate_from_logs` 가 헤더를 정규식으로 파싱해 `ChannelMessage` 재구성 |
| LLM 자체 | `--add-dir` 로 채널 폴더가 노출됨. 모델이 `ls` 하고 읽는다 |
| 웹 뷰어 | `WebPublisher` 가 같은 파일을 HTML 로 렌더 |

**평가**: DB 를 안 쓴 게 아니라 **DB 가 필요 없게 만들었다**. Discord snowflake ID 는 시간순이라 파일명 정렬이 곧 시간순이고(`hydrate_from_logs` 가 이걸 쓴다), CLI 가 `--add-dir` 로 읽을 수 있으려면 어차피 파일이어야 한다. 이 제약을 받아들이니 상태 복원과 LLM 컨텍스트 공유가 같은 코드로 해결됐다.

`_turns/{message_id}.json` 은 별도 디버그 채널이다 — 실제 입력 텍스트, 응답, 상태, 재전송 여부까지 턴마다 남긴다. 화면 스크래핑이 어긋났을 때 "모델이 뭘 받았고 뭘 뱉었나"를 사후에 볼 수 있다.

### 3.5 설정 변경 감지

```python
# core/session_registry.py::_desired_signature
return (mode, model, effort, permission, fast)
```

세션을 만들 때 이 튜플을 `_applied_states[key]` 에 기억하고, 다음 `get_or_create` 에서 현재 상태와 비교한다. 다르면 살아있는 세션이라도 죽이고 다시 띄운다. 사용자가 패널에서 effort 를 바꾸면 다음 메시지에서 조용히 반영된다.

**이게 없으면**: 상태는 바뀌었는데 프로세스는 옛 인자로 떠 있는 불일치가 생긴다. 튜플 비교 한 줄이 "설정과 프로세스의 동기화"라는 클래스의 버그를 통째로 막는다.

### 3.6 페르소나 레벨 = 문서 절단

```python
# core/prompt_composer.py
SECTION_RE = re.compile(r'^## @@(\d+):', re.MULTILINE)
```

`PROMPT.md` 하나를 `@@N` 헤더로 자른다. LV1 은 `@@1` 만, LV4 는 `@@5` 앞까지, LV5 는 전문. 페르소나 강도를 파일 5개가 아니라 **한 파일의 절단 위치**로 표현했다.

`SessionRegistry.mode_dirs` 가 여기에 접근 제어를 얹는다 — LV1 은 `channel` 과 `persona` 폴더만, LV2 이상은 `users` 폴더(사용자별 로그)까지 `--add-dir` 로 받는다. 즉 **약한 페르소나는 개인 기록을 못 본다**. 프롬프트 강도와 정보 접근 권한이 같은 숫자로 묶였다.

### 3.7 Codex 를 위한 격리 홈

Codex CLI 는 시스템 프롬프트를 인자로 못 받는다. `CodexAdapter._prepare_runtime_home` 은 채널마다 `CODEX_HOME` 을 만들고 거기에 `config.toml` 을 생성한다:

- `developer_instructions` 에 조립된 프롬프트를 JSON 문자열로
- `approval_policy` / `sandbox_mode` 를 권한 모드에서 매핑
- `[projects."<workspace>"] trust_level = "trusted"` — 작업 폴더 신뢰 등록
- `auth.json` 은 사용자 홈에서 복사

두 가지 세부가 실전 경험을 보여준다:

`_sanitize_cli_args` — 사용자가 `.env` 에 `--model` 이나 `-c model=...` 을 넣어도 **벗겨낸다**. config.toml 이 관리하는 키와 인자가 충돌하면 어느 쪽이 이기는지 예측 불가능하므로 한 곳에서만 지정하게 강제한다.

`_workspace_trust_variants` — Windows 경로를 슬래시/역슬래시, 대문자/소문자 드라이브, `normcase` 변형까지 전부 `[projects]` 에 등록한다. Codex 가 어떤 형식으로 정규화하는지 확신할 수 없으니 전부 넣는다. 우아하진 않지만 "신뢰 프롬프트가 안 뜬다"는 결과를 보장한다.

### 3.8 컨텍스트 예산이 입력 길이에 반비례한다

```python
# core/context_builder.py::_calc_budget
≤500자 입력 → 3000자 컨텍스트
≥2000자 입력 → 500자 컨텍스트  (사이는 선형 보간)
```

짧은 질문일수록 대화 맥락이 중요하고, 긴 입력(코드 붙여넣기)일수록 맥락은 노이즈다. 그리고 `last_seen_msg_id` — 이 봇이 지난 턴에서 이미 본 메시지는 다시 안 넣는다. CLI 세션이 자체 히스토리를 갖고 있으므로 중복이다. 두 규칙 다 **토큰을 아끼는 게 아니라 응답 품질을 위한** 것이고, 주석이 그 의도를 밝힌다.

### 3.9 유휴 기반 완료 — 그리고 그 한계를 기록한 것

`PtySession.wait_response` 는 프롬프트 복귀가 아니라 **"idle_seconds 동안 출력이 없으면 끝"** 으로 완료를 판정한다. provider 마다 프롬프트 모양이 다르니 이게 더 일반적이다.

문제는 `TurnExecutor.execute` 의 이 주석에 있다:

> Observed: ~3.3s gap between input echo and actual response, which exceeds the default IDLE_SECONDS (3.0).

입력 에코 직후 API 응답이 오기까지의 공백이 유휴 임계값보다 길어서 **빈 응답으로 조기 종료**된다. 대응은 `idle_seconds + 2.0` 을 더 기다리고, 그래도 비면 한 번 재전송. 휴리스틱 위에 휴리스틱이다.

그래도 이걸 높게 사는 이유는 **관측치를 숫자로 코드에 남겼다**는 것이다. "가끔 빈 응답이 온다" 가 아니라 "3.3초 vs 3.0초" 다. 다음 사람이 임계값을 조정할 근거가 있다. 4.8에서 이 방식 자체의 취약성을 다룬다.

### 3.10 웹훅 정체성과 그 뒷수습

봇 하나로 여러 페르소나를 운영하려면 웹훅이 유일한 방법이다(이름·아바타 커스텀). 하지만 웹훅 메시지는 답장 체인에서 작성자를 잃는다. `WebhookTracker` 는 `(channel_id, message_id) → persona_id` 를 채널당 200개 LRU 로 기억하고 JSON 으로 영속화한다.

`_target_reasons` 의 2순위가 이걸 쓴다 — 사용자가 "아리스"의 응답에 답장하면 역할 멘션 없이도 아리스가 받는다. 작은 클래스지만 이게 없으면 다중 페르소나 UX 가 성립하지 않는다.

### 3.11 라이브 터미널 — 이미 구현된 "Remote"

`WebPublisher._handle_terminal_ws` 는 살아있는 PTY 세션에 `add_data_listener` 를 걸고 원시 출력을 WebSocket 으로 흘린다. 클라이언트는 xterm.js. 접속 시 `get_display_lines()` 스냅샷을 먼저 보내고, 상태 전이(`ready → busy`)도 별도 이벤트로 밀어준다. Discord OAuth 로 길드 멤버만 열람.

읽기 전용이지만(`_receiver` 가 입력을 버린다) **브라우저에서 PTY 화면을 실시간으로 보는 경로**가 이미 있다. Terminalist 가 로드맵에만 적어둔 Remote 기능의 절반이 여기 있다.

### 3.12 운영 도구가 제품 안에 있다

- `main.py --monitor`: 콘솔에서 세션 목록·화면 tail 을 보고, `l` 로 세션을 잠근다. 잠긴 세션은 Discord 입력을 거부한다(`_locked_sessions`). 운영자가 CLI 를 직접 조작하는 동안 사용자 메시지가 끼어드는 걸 막는다.
- `cli.py --probe-startup`: 세션 하나를 띄우고 `status / dump / raw / repr / send / auto` 로 시작 시퀀스를 한 단계씩 관찰한다. 신뢰 프롬프트 감지가 실패할 때 어떤 바이트가 왔는지 보는 도구.
- `LogStore.save_turn_debug`: 턴마다 입력·출력·상태 JSON.
- `_dump_startup_failure` (ExportV2): 시작 타임아웃 시 화면 30줄 + 원시 2000자를 로그에.

화면 스크래핑은 언젠가 반드시 깨진다. 이 프로젝트는 **깨졌을 때 볼 수 있는 것**을 만드는 데 코드를 썼다.

---

## 4. 문제점

심각도 순. 각 항목은 근거가 되는 코드를 가리킨다.

### 4.1 [높음] 화면 스크래핑이 CLI 버전과 결합돼 있다

`providers/claude.py` 의 `_CHROME_PATTERNS` 는 19개, `_PENDING_PATTERNS` 5개, 마스코트 아트 정규식, 상태바 감지, `●` 불릿 휴리스틱(`_strip_context_echo` 는 "짧은 ● + 빈 줄 + 다른 ●" 이면 첫 ● 를 thinking 으로 간주). 주석에 `claude 2.x` 라고 표시된 패턴이 여러 개다 — CLI 가 UI 를 바꿀 때마다 패턴이 추가됐다는 뜻이다.

```python
re.compile(r"how is claude doing this session"),   # 세션 피드백 설문
re.compile(r"^\s*✻\s"),                             # thinking 시간 표시
re.compile(r"\(ctrl\+o to expand\)"),               # 도구 사용 요약
```

이건 유지보수 부채가 아니라 **아키텍처 결정의 청구서**다. TUI 를 스크래핑하기로 한 순간 정해진 비용이다. 대안은 `claude -p --output-format stream-json` — 구조화된 이벤트 스트림이라 파싱 대상이 텍스트가 아니라 JSON 이 된다. 다만 그러면 권한 프롬프트·`/compact` 같은 대화형 제어를 잃으므로, **화면은 PTY 로 두고 응답 추출만 stream-json 으로** 분리하는 하이브리드가 현실적이다.

`ClaudeAdapter._normalize_effort` 가 `xhigh → high` 로 낮추는 것도 같은 부류다. 작성 시점의 CLI 제약이었겠지만 현재 CLI 는 `xhigh` 를 받는다. 이런 "그때는 맞았던" 보정이 코드에 남아 조용히 품질을 깎는다.

### 4.2 [높음] 기본값이 권한 우회 + 공개 채널이다

```python
# config.py::_provider_from_env
default_permission = "bypass"          # claude → --permission-mode bypassPermissions
default_args = '["--dangerously-bypass-approvals-and-sandbox", ...]'   # codex
# codex.py::_PERMISSION_PRESETS
"bypass": ("never", "danger-full-access")
```

그리고 `DEFAULT_COMMAND_ACCESS=public`. 조합하면: **역할을 멘션할 수 있는 누구나 호스트에서 파일 쓰기·셸 실행이 가능한 에이전트에게 지시할 수 있다.** 파일 탐색기와 `/put` 은 작업 폴더에 임의 파일을 올릴 수도 있다.

접근 제어 자체는 있다 — `/access` 로 protagonist 전용으로 바꿀 수 있고, `_check_access_admin` 이 있다. 문제는 **기본값**이다. 개인 서버에서 혼자 쓴다면 괜찮지만, 공개 저장소의 README 를 보고 따라 하는 사람은 기본값으로 시작한다. 기본을 `default` 권한 + `protagonist` 접근으로 뒤집고, bypass 는 명시적 opt-in 이어야 한다.

부수적으로 `--dangerously-bypass-approvals-and-sandbox` 는 `_sanitize_cli_args` 가 항상 제거하므로 **죽은 기본값**이다. 실제 권한은 config.toml 의 `approval_policy` 가 결정한다. 혼란만 준다.

### 4.3 [중간] `discord_transport.py` 3,340줄

한 파일에 있는 것: 출력 싱크, 상태 패널(뷰·버튼·셀렉트·임베드 빌더), 권한 선택 뷰, 지시 모달, 파일 탐색기(상태·스캔·뷰·버튼·메뉴·폴더/파일 액션·삭제 확인·복사 모달·새 폴더 모달), 트랜스포트 본체(이벤트·라우팅·웹훅·헬스), 슬래시 커맨드 14개, 파일 커맨드 3개.

이건 "Discord 와 닿는 모든 것"이라는 기준으로 묶인 건데, 그 기준이 너무 넓다. 최소한:

```text
transport/discord/
├── transport.py      DiscordTransport (이벤트 · 라우팅 · 웹훅)
├── sink.py           DiscordOutputSink
├── ui/panel.py       ControlPanelView + embed
├── ui/explorer.py    FileExplorerView 일가
├── ui/prompts.py     ChoiceView · InstructModal
└── commands/         session.py · files.py
```

`interaction.client._transport` 로 뷰가 트랜스포트를 역참조하는 패턴도 분리를 어렵게 만든다. 뷰에 필요한 건 `BridgeCore` 와 `ChannelManager` 뿐인데 트랜스포트 전체를 잡고 있다.

### 4.4 [중간] 추출 로직이 세 곳에 있다

| 파일 | 내용 |
|---|---|
| `pty/screen_parser.py` | `extract_response_text`, `CHROME_PATTERNS` 13개 |
| `providers/claude.py` | `_extract_response_text`, `_CHROME_PATTERNS` 19개, `_strip_context_echo`, `_strip_tool_blocks` |
| `core/turn_executor.py` | `_strip_context_echo`, `_collapse_tool_blocks`, `_TOOL_MARKER_RE`, `_detect_prompt_raw` |

`screen_parser.py` 는 어댑터 도입 전의 옛 버전으로 보인다. `pty/__init__.py` 가 re-export 하지만 그 심볼을 쓰는 곳은 없다 — 모든 소비자는 `pty.handler` 만 import 한다. `_strip_context_echo` 는 `claude.py` 와 `turn_executor.py` 에 **거의 같은 70줄**이 두 벌 있다. `_TOOL_MARKER_RE` 는 세 파일에 세 번 정의됐다. 한쪽을 고치면 다른 쪽이 어긋난다 — 이미 `_CHROME_PATTERNS` 가 13개 vs 19개로 어긋나 있다.

### 4.5 [중간] 순수 함수가 많은데 유닛 테스트가 없다

테스트는 `tests/test_pipe_chain.py` 하나이고 실제 Claude CLI 를 두 개 띄워야 돈다. 그런데 이 코드베이스에는 **입출력이 문자열인 순수 함수**가 유독 많다:

- `renderer.py::split_text_for_discord` — 코드 펜스를 넘나드는 1900자 분할. 펜스 스택 로직은 경계 케이스가 많다.
- `turn_executor.py::_collapse_tool_blocks` — Claude/Codex 두 포맷.
- `context_builder.py::_calc_budget`, `_resolve_message_links`
- `prompt_composer.py::_extract_before_section`, `_inject_protagonist`
- `channel_manager.py::_parse_log_file` — 헤더 정규식 두 개.
- `codex.py::_sanitize_cli_args`, `_workspace_trust_variants`

전부 pytest 로 1초 안에 도는 것들이다. 4.1 의 정규식들이 CLI 업데이트로 깨질 때 **어느 패턴이 깨졌는지** 알려줄 테스트가 없다는 게 진짜 비용이다. 실제 화면 덤프를 픽스처로 저장해두면 회귀 테스트가 된다 — `_turns/*.json` 과 `cli.py --dump` 가 이미 그 덤프를 만들고 있다.

`test_pipe_chain.py::MockSink.finalize(self, text)` 는 `OutputSink.finalize(text, meta)` 와 시그니처가 다르다. 테스트가 자기 목을 직접 호출하니 통과하지만, `TurnExecutor` 를 거치지 않으므로 실제 계약을 검증하지 않는다.

### 4.6 [낮음] 파사드가 샌다

`BridgeCore` 는 파사드인데 트랜스포트가 `self._core.channel_mgr._channels`, `cm._save_state(...)`, `self._core._registry` 를 직접 만진다. `core/__init__.py` 자신도 `self._log_store._root` 를 쓴다. `ExportV2/session.py` 는 `adapter._prepare_runtime_home`, `adapter._sanitize_cli_args` 를 `pyright: ignore` 를 달고 호출한다.

밑줄이 붙은 이름을 밖에서 쓴다는 건 그 이름이 실은 공개 API 라는 뜻이다. 밑줄을 떼거나, 진짜 감출 거면 공개 메서드를 만들어야 한다. ExportV2 의 경우는 명확하다 — `_prepare_runtime_home` 은 두 프로젝트가 쓰는 공개 API 다.

### 4.7 [낮음] 예외를 삼키는 곳

```python
# core/__init__.py::_hydrate_all_channels
except (ValueError, Exception):   # ValueError 는 Exception 의 부분집합
    continue
```

`webhook_tracker.py::_save` 는 실패를 `LOGGER.debug` 로 내린다 — 영속화 실패가 디버그 로그면 운영 중엔 절대 안 보인다. `config.py::_load_default_state_file` 은 JSON 파싱 실패 시 조용히 `{}` 를 반환해서, 오타 하나로 모든 기본값이 사라져도 알 길이 없다.

### 4.8 [낮음] 유휴 판정의 구조적 취약성

3.9의 연장이다. 유휴 기반 완료는 **응답 중간에 idle_seconds 이상의 침묵이 생기면** 잘린다. 도구 실행이 길거나 모델이 오래 생각하면 그렇다. `has_pending_output` 이 "pondering", "ruminating" 같은 단어로 이를 보완하지만 이것도 텍스트 매칭이다. `ExportV2/session.py::_response_looks_incomplete` 에는 `"gpt-5.4 xhigh"` 가 하드코딩돼 있다 — 모델명이 바뀌면 조용히 무력화된다.

근본 해결은 4.1 과 같다. 완료 신호를 화면이 아니라 구조화된 출력에서 받아야 한다.

---

## 5. v2 → ExportV2 가 드러낸 것

ExportV2 는 v2 를 **12,286줄 → 1,525줄**로 줄인 재작성이다. 뺀 것: `.env`(→ `settings.json`), 슬래시 커맨드, 패널, 파일 탐색기, 파이프 체인, 컨텍스트 주입, 웹 퍼블리셔, 다중 provider 동시 운영. 남긴 것: 역할 멘션 → 세션 → 응답.

축소 자체보다 흥미로운 건 **축소하면서 발견한 개선**이다. v2 에 없고 ExportV2 에만 있는 것:

| ExportV2 | 무엇 |
|---|---|
| `_start_once` settle 대기 | `--resume` 직후 CLI 가 이전 대화를 재생하는 동안 기다린다. 안 기다리면 재생된 옛 대화가 첫 응답에 섞인다 |
| `_dismiss_leftover_prompts` | 이전 턴에 뜬 세션 설문("How is Claude doing?")을 `Esc` 로 닫는다. 안 닫으면 다음 응답을 오염시킨다 |
| `_build_turn_input` (Codex) | 한국어 입력을 PTY 로 직접 보내면 Windows 에서 깨지므로, **JSON 파일로 쓰고 "이 파일을 읽어서 답하라"** 고 지시한다. `ensure_ascii=True` 로 유니코드 이스케이프 |
| `_resolve_claude_resume_id_from_disk` | 화면에서 세션 ID 를 못 찾으면 `~/.claude/projects/<sanitized-path>/*.jsonl` 의 mtime 으로 찾는다 |
| `_sync_codex_runtime_home` | `auth.json` 을 mtime 비교로 갱신 (v2 는 없을 때만 복사) |

이 다섯 개는 전부 **v2 로 실제 운영하다 겪은 문제**의 해법이다. 그리고 전부 v2 에 역이식되지 않았다. 두 코드베이스가 갈라진 상태다. ExportV2 가 v2 의 `PromptComposer`, `ClaudeAdapter`, `CodexAdapter`, `_collapse_tool_blocks` 를 import 해서 쓰는 것과 대조된다 — 어댑터는 공유하는데 세션 관리 교훈은 공유 안 됐다.

권장: 위 다섯 개를 v2 의 `SessionRegistry._spawn_session` / `TurnExecutor` 로 옮기고, ExportV2 는 "설정이 단순한 v2" 로 남기거나 정리한다.

---

## 6. Terminalist 와의 관계

같은 사람이 같은 계층(pywinpty + pyte + Win32 콘솔 입력) 위에 만든 두 프로젝트다. `Export/` 는 두 저장소에 바이트 단위로 동일하게 들어 있다.

| | Terminalist | LLMChat |
|---|---|---|
| 화면의 소비자 | 사람 (VT100 렌더) | Discord (텍스트 추출) |
| 세션 상태머신 | `LLMSession` (STARTING/BUSY/READY) | `PtySession` (STARTING/READY/BUSY/DEAD) |
| 완료 판정 | 프롬프트 정규식 | 유휴 시간 |
| 응답 추출 | `extract_response` TODO 스텁 | 어댑터 500줄 |
| 권한 프롬프트 감지 | `detect_interaction` → `None` 스텁 | `detect_interaction` + Discord 버튼 |
| 원격 | 로드맵 | WebSocket + xterm.js (읽기 전용) |

Terminalist 의 `LLMSession` ABC 에 비어 있는 훅들이 LLMChat 의 `ProviderAdapter` 에는 채워져 있다. 반대로 Terminalist 의 `PreservingScreen`(pyte 축소 버그 우회), TES 이벤트 스트림, 렌더 스케줄링은 LLMChat 에 없다. 두 프로젝트를 합치면 한 세션 계층 위에 "사람용 화면"과 "봇용 추출"이 같이 올라간다 — Terminalist 의 Flow 로드맵이 정확히 그 그림이다.

---

## 7. 권장 순서

1. **기본값 뒤집기** (4.2) — 한 줄 변경, 가장 큰 위험 제거.
2. **추출 로직 통합** (4.4) — `screen_parser.py` 삭제, `_strip_context_echo` 한 벌로, 정규식 상수를 `providers/claude.py` 한 곳에.
3. **화면 덤프 픽스처 테스트** (4.5) — `_turns/*.json` 에서 실제 입력·출력 쌍을 골라 `extract_response` 회귀 테스트로. CLI 업데이트 때 무엇이 깨졌는지 즉시 안다.
4. **ExportV2 교훈 역이식** (5) — settle 대기, 설문 닫기, 디스크 resume 폴백.
5. **transport 분해** (4.3) — 기능 변경 없이 파일만 나눈다.
6. **stream-json 하이브리드** (4.1) — 가장 크고, 위 1~5 가 끝난 뒤.

---

## 부록: 코드에서 읽히는 작업 방식

리뷰 대상은 코드지만, 코드가 말해주는 것이 하나 더 있다. 이 저장소에는 **관측치가 숫자로 적힌 주석**이 유독 많다 — "3.3초 vs 3.0초", "채널당 200개", "20초 신뢰 프롬프트 대기", "5초 steer 쿨다운". 그리고 `--probe-startup`, `--dump`, `_turns/`, `_dump_startup_failure` 처럼 **관측하기 위한 도구**가 제품 안에 들어 있다.

화면 스크래핑은 처음부터 깨질 것을 알고 선택한 방식이다. 그 선택을 한 사람은 깨졌을 때 볼 수 있는 것을 같이 만들었다. 그게 이 코드베이스의 가장 일관된 특징이다.
