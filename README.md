# LLMChat

Discord 채널에서 **Claude Code / OpenAI Codex CLI 를 페르소나로 굴리는 최소 PTY 브리지**.

LLM CLI 를 API 로 호출하는 대신 실제 터미널 프로세스(PTY)로 띄우고, 가상 화면(pyte)을 읽어 Discord 로 중계한다. 페르소나 역할을 멘션하면 그 채널 전용 CLI 세션이 응답하고, 응답은 페르소나 이름·아바타를 단 웹훅으로 나간다. 세션은 재시작 후에도 `--resume` 으로 이어진다.

- Python 3.11+ · Windows (pywinpty)
- 약 3,400줄 · 의존: `discord.py`, `pywinpty`, `pyte`
- 이 저장소는 원래 12,000줄짜리 브리지(`v2`)를 **쓸 수 있는 가장 작은 단위**로 줄인 것이다. 전체 버전과 그 설계 리뷰는 [docs/REVIEW.md](docs/REVIEW.md) 와 git 이력(`ab71988`)에 있다.

## 동작

1. 설정된 서버에서 페르소나 **역할 멘션**이 오면 `(channel, persona)` 별 PTY 세션을 lazy 생성한다.
2. 입력은 `[표시이름:uid] : 메시지` 한 줄로 축약해 CLI 에 보낸다. (Codex 는 한국어 입력이 PTY 에서 깨지므로 JSON 파일로 쓰고 읽게 한다.)
3. PTY 출력이 `idle_seconds` 동안 멈추면 응답 완료로 보고, provider 어댑터가 화면에서 응답 텍스트를 추출한다. 도구 호출 블록은 한 줄로 접는다.
4. 응답은 웹훅으로 전송하고 `.bridge/{server}/{channel}/{message_id}.txt` 에 남긴다. 이 폴더는 CLI 에 `--add-dir` 로 열려 있어 **모델이 대화 기록을 직접 읽을 수 있다.**
5. CLI 세션 ID 는 `.data/{server}/state.json` 에 저장되고, 채널에 로그가 있으면 다음 시작 때 `--resume` 한다.

없는 것: 슬래시 커맨드, 상태 패널, 파일 탐색기, 파이프 체인, 웹 뷰어, 다중 provider 동시 운영. 전부 `v2` 에 있었고 의도적으로 뺐다.

## 구성

```text
ExportV2/
├── __main__.py          python -m ExportV2
├── config.py            settings.json → AppSettings
├── models.py            설정·상태·어댑터 계약 타입
├── discord_backend.py   discord.Client: 역할 생성, 멘션 라우팅, 웹훅 발송
├── session.py           PersonaSession: 스폰 · resume · 턴 실행 · 응답 추출
├── prompt_composer.py   PROMPT.md 조립 (@@N 섹션, @레퍼런스)
├── textutil.py          도구 블록 접기, 빈 줄 압축
├── logs.py              .data / .bridge 파일 레이아웃
├── providers/           ProviderAdapter + ClaudeAdapter / CodexAdapter (화면 파싱, 스폰 인자)
├── vt/                  VirtualTerminal: winpty + pyte, PreservingScreen (리사이즈 보존)
├── prompts/             페르소나 규약 문서 + 예시
└── tests/               pytest (VirtualTerminal, PreservingScreen, textutil, PromptComposer)
```

## 실행

요구사항: Windows, Python 3.11+, `claude` 또는 `codex` CLI 가 PATH 에 있을 것 (Codex 는 VS Code 확장 번들도 자동 탐색). Discord 봇에 `MESSAGE CONTENT` 인텐트, 채널 웹훅 관리, 역할 생성 권한.

```powershell
pip install -r requirements.txt
copy ExportV2\settings.example.json ExportV2\settings.json   # bot_token, servers 채우기
python -m ExportV2
python -m ExportV2 --settings path\to\settings.json --log-level DEBUG
```

### settings.json

| 키 | 의미 |
|---|---|
| `bot_token` | Discord 봇 토큰 |
| `data_root` / `bridge_root` | 상태 · 대화 로그 위치 (settings 파일 기준 상대 경로) |
| `prompts_root` | 페르소나 프롬프트 루트. 생략 시 `ExportV2/prompts` |
| `llm.provider` | `claude` \| `codex` — 배포당 하나 |
| `llm.model` / `effort` / `permission` / `fast` | 스폰 인자. `permission` 은 `default` \| `plan` \| `bypass` |
| `llm.idle_seconds` | 이 시간 동안 출력이 없으면 응답 완료 |
| `servers.<guild_id>.workspace` | CLI 작업 폴더 |
| `servers.<guild_id>.personas.<id>.prompt_dir` | `prompts_root/<prompt_dir>/PROMPT.md` |

> `permission: "bypass"` 는 CLI 의 모든 도구 사용을 승인 없이 허용한다. 역할을 멘션할 수 있는 누구나 그 권한으로 호스트에 지시할 수 있으므로, 신뢰할 수 있는 서버에서만 켜라. 예시 설정의 기본값은 `default` 다.

### 페르소나

`prompts_root/<dir>/PROMPT.md` 를 `## @@N:` 섹션 규약으로 쓴다. [ExportV2/prompts/README.md](ExportV2/prompts/README.md) 와 `example/` 참고.

### 테스트

```powershell
python -m pytest -q
```

## 저장소에 포함되지 않은 것

- 비밀·런타임 상태: `settings.json`, `.bridge/`, `.data/`
- 페르소나 프롬프트 폴더: 캐릭터 설정용으로 수집한 외부 텍스트가 섞여 있어 제외. 규약 문서와 예시만 포함.
- 전체 브리지(`v2`), 독립 터미널 엔진(`Export`), 초기 실험 파일: git 이력 `ab71988` 에 있다.

## 관련 프로젝트

- [Terminalist](https://github.com/LeopardSwissRoll/Terminalist) — 같은 PTY/pyte 계층 위에 만든 tmux 스타일 멀티플렉서. `ExportV2/vt/` 는 Terminalist 의 `Export/vt` 와 같은 코드다.
