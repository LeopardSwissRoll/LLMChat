# LLMChat

Discord 채널에서 **Claude Code / OpenAI Codex CLI 를 페르소나로 굴리는 PTY 브리지**.

LLM CLI 를 API 로 호출하는 대신, 실제 터미널 프로세스(PTY)로 띄우고 가상 화면(pyte)을 읽어 Discord 로 중계한다. 한 채널에 여러 페르소나가 각자의 이름·아바타(웹훅)로 살고, 각 페르소나는 채널마다 독립된 CLI 세션을 가진다. 세션은 재시작 후에도 `--resume` 으로 이어진다.

- Python 3.11+ · Windows (pywinpty)
- 약 16,000줄 · 외부 의존: `discord.py`, `pywinpty`, `pyte`, `aiohttp`, `pygments`
- 설계 리뷰: [docs/REVIEW.md](docs/REVIEW.md)

## 무엇을 하나

1. **역할 멘션 → 페르소나 세션.** `@아리스 이 파일 고쳐줘` 라고 하면 그 채널의 "아리스" 세션(Claude 또는 Codex)이 응답한다. 세션은 `(persona, provider, channel, workspace)` 단위로 lazy 생성된다.
2. **파이프 체인.** 응답 메시지에 답장하거나 `@메시지ID` 로 참조하면, 그 메시지의 내용이 다음 턴 입력에 주입된다. 아직 처리 중인 메시지를 참조하면 완료를 기다린다. 앞 턴이 실패하면 뒤 턴도 즉시 실패한다.
3. **CLI 제어를 Discord UI 로.** 상태 패널(버튼·드롭다운)과 17개 슬래시 커맨드로 모델·effort·권한 모드·컨텍스트 압축·중단·provider 전환을 한다. Claude 의 권한 요청 프롬프트는 버튼으로 뜬다.
4. **파일 탐색기.** 스레드 안에서 작업 폴더를 탐색·다운로드·업로드·삭제한다.
5. **웹 뷰어 (선택).** 긴 응답은 Discord OAuth 로 보호된 웹 페이지로, 살아있는 PTY 세션은 xterm.js 로 실시간 열람.
6. **운영 모니터.** `--monitor` 로 콘솔에서 세션 목록·화면 tail 을 보고, 세션을 잠가 Discord 입력을 막을 수 있다.

## 저장소 구성

```text
LLMChat/
├── v2/                    본체 (12,300줄)
│   ├── main.py            Discord 봇 진입점 (--monitor)
│   ├── cli.py             로컬 테스트: raw passthrough / parsed REPL / --probe-startup
│   ├── bridge/
│   │   ├── core/          BridgeCore · TurnExecutor · SessionRegistry · ChannelManager
│   │   │                  ContextBuilder · LogStore · PromptComposer · WebPublisher
│   │   ├── providers/     ProviderAdapter ABC + ClaudeAdapter / CodexAdapter
│   │   ├── pty/           PtySession (winpty + pyte) · screen_parser
│   │   ├── transport/     TransportAdapter / OutputSink + DiscordTransport · WebhookTracker
│   │   ├── streamer.py    Discord/Webhook 스트리밍 (edit-in-place)
│   │   ├── renderer.py    Markdown → Discord 정규화, 1900자 분할
│   │   └── web_template.py
│   ├── prompts/           페르소나 프롬프트 (규약 문서 + 예시만 포함)
│   └── tests/test_pipe_chain.py
├── ExportV2/              v2 의 최소 축소판 (1,500줄): settings.json · 역할 멘션만 · 패널/커맨드 없음
├── Export/                PTY + pyte + Win32 입력만 뽑아낸 독립 터미널 엔진 (Terminalist 와 공유)
├── fakeTerm.py / .md      초기 실험과 시행착오 기록
├── test_hangul.py         PTY 한글 출력 실험
└── test_ime.py            ReadConsoleInputW vs msvcrt IME 실험
```

## 아키텍처 — 한 턴의 흐름

```text
Discord message
  │
  ▼
DiscordTransport.on_message
  ├─ 모든 사람 메시지를 LogStore + ChannelManager 스트림에 기록 (passive logging)
  ├─ _target_reasons: 역할 멘션 > 웹훅 메시지에 답장 > @bot 멘션
  └─ 웹훅 "processing..." 을 먼저 보내고 그 ID 를 WebhookTracker 에 등록
  │
  ▼
BridgeCore.handle_message  ──▶  per-SessionKey asyncio.Queue(16)
  │                              (턴과 제어 명령이 같은 큐를 통과)
  ▼
_queue_worker
  ├─ 답장/@ID 파이프 해석 — 대상 로그 파일이 생길 때까지 대기 (최대 30분)
  ├─ SessionRegistry.get_or_create — 설정 변경 감지 시 재시작, --resume 실패 시 fresh
  └─ TurnExecutor.execute
       ├─ ContextBuilder: 최근 대화를 입력 길이에 반비례하는 예산으로 앞에 붙임
       ├─ PtySession.send_message → 화면 리셋 → 입력
       ├─ 스트리밍 루프: 2초마다 화면 파싱 → "streaming..." 미리보기 갱신
       ├─ wait_response: PTY 출력이 idle_seconds 동안 멈추면 완료
       ├─ detect_interaction: 권한 프롬프트면 버튼 → 키 입력 → 다음 라운드 (최대 5)
       ├─ extract_response → tool 블록 접기 → finalize (processing 메시지를 제자리 편집)
       └─ LogStore 저장 → 스트림 추가 → resume id 갱신 → 파이프 대기자 깨움
```

## 핵심 개념

**SessionKey** — `(persona_id, provider_id, channel_id, workspace)` 의 sha256 앞 16자리. 같은 페르소나라도 채널마다 다른 CLI 프로세스이고, 같은 채널에서 provider 를 바꾸면 이전 provider 세션은 정지된다.

**페르소나 레벨** — `PROMPT.md` 를 `## @@N:` 헤더로 자르는 5단계 강도. LV1(core)은 `@@1` 만, LV5(masquerade)는 전문. 레벨에 따라 CLI 에 `--add-dir` 로 노출되는 폴더도 달라진다 (LV1 은 `users/` 로그를 못 본다). 자세한 규약은 [v2/prompts/README.md](v2/prompts/README.md).

**파일시스템이 통합 버스** — 모든 메시지와 응답은 `.bridge/{server}/{channel}/{message_id}.txt` 한 형식으로 저장된다. 이 파일 하나가 ① 파이프 체인의 대기 대상 ② 재시작 시 컨텍스트 복원 원본 ③ CLI 가 `--add-dir` 로 직접 읽는 대화 기록 ④ 웹 뷰어 원본 역할을 동시에 한다.

**edit-in-place** — "processing..." 메시지를 지우고 새로 보내지 않고 **같은 메시지를 편집**해서 응답으로 바꾼다. 메시지 ID 가 유지되므로 응답이 나오기 전에도 그 메시지에 답장(파이프)을 걸 수 있다.

**웹훅 정체성** — 봇 하나가 페르소나별 이름·아바타로 웹훅 발송한다. 웹훅 메시지는 Discord 답장 체인에서 작성자 정보를 잃기 때문에 `WebhookTracker` 가 `(channel, message_id) → persona_id` 를 별도로 기억한다.

## 실행

### 요구사항

- Windows, Python 3.11+
- `claude` 또는 `codex` CLI 가 PATH 에 있을 것 (Codex 는 VS Code 확장 번들 경로도 자동 탐색)
- Discord 봇: `MESSAGE CONTENT` 인텐트, 채널 `MANAGE_WEBHOOKS`, 역할 생성 권한

### v2

```powershell
pip install -r v2/requirements.txt
copy v2\.env.example v2\.env      # DISCORD_BOT_TOKEN, PERSONAS 채우기
mkdir v2\prompts\<persona_dir>    # PROMPT.md 작성 (v2/prompts/example 참고)

python v2/main.py                 # 봇 실행
python v2/main.py --monitor       # + 콘솔 세션 모니터
```

로컬에서 봇 없이 파이프라인만 돌려볼 때:

```powershell
python v2/cli.py                        # raw: 터미널에 CLI TUI 를 그대로 통과
python v2/cli.py --parsed               # parsed: BridgeCore 를 거치는 REPL
python v2/cli.py --parsed "안녕" --dump  # one-shot + 화면 덤프
python v2/cli.py --probe-startup --persona <id> --channel-id 1   # 시작 시퀀스 대화형 탐사
```

### ExportV2

```powershell
copy ExportV2\settings.example.json ExportV2\settings.json   # bot_token, servers 채우기
python -m ExportV2
```

`.env` 대신 `settings.json` 하나, 역할 멘션만 트리거, 패널·슬래시 커맨드 없음. v2 의 `PromptComposer` 와 provider 어댑터를 그대로 재사용하되 세션 관리는 `PersonaSession` 한 클래스로 줄였다.

## 슬래시 커맨드 (v2)

| 커맨드 | 동작 |
|---|---|
| `/init` | 작업 폴더 초기화 + 상태 패널 표시 (폴더/채널 없으면 생성) |
| `/status` | 상태·제어 패널 |
| `/persona` | 페르소나 레벨(LV1~5) 변경 |
| `/provider` | claude ↔ codex 전환 |
| `/model` `/effort` `/permission` | 활성 provider 의 모델 / effort / 권한 모드 |
| `/compact` `/clear` | 컨텍스트 압축 / 대화 초기화 |
| `/interrupt` | 현재 응답 중단 |
| `/reset` | 이 채널의 PTY 세션 재시작 |
| `/instruct` | 세션에 추가 지시 전송 (모달) |
| `/access` | 커맨드 접근 public ↔ protagonist |
| `/browse` | 파일 탐색기 스레드 열기 |
| `/a` | 파일을 참조해 LLM 에 메시지 전송 |
| `/get` `/put` | 작업 폴더 파일 다운로드 / 업로드 |

## 저장소에 포함되지 않은 것

- **비밀·런타임 상태**: `.env`, `.kiss`, `ExportV2/settings.json`, `.bridge/`, `.data/`
- **페르소나 프롬프트 폴더** (`v2/prompts/<persona>/`, `Persona/`): 캐릭터 설정용으로 수집한 외부 텍스트가 섞여 있어 제외. 규약 문서와 예시 페르소나만 포함.

## 관련 프로젝트

- [Terminalist](https://github.com/LeopardSwissRoll/Terminalist) — 같은 PTY/pyte 계층 위에 만든 tmux 스타일 멀티플렉서. `Export/` 는 두 저장소가 공유하는 추출본이다. Terminalist 가 "사람이 보는 화면"이라면 LLMChat 은 "Discord 가 보는 화면"이다.
