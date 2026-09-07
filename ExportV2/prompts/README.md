# prompts/

페르소나 프롬프트 자산 디렉터리. **실제 페르소나 폴더는 저장소에 포함되지 않는다** — 캐릭터 설정용으로 수집한 외부 텍스트가 섞여 있어 공개 배포 대상이 아니다. 이 문서는 `PromptComposer` 가 기대하는 레이아웃을 설명하고, `example/` 은 그 규약을 따르는 최소 예시다.

기본 위치는 이 폴더(`ExportV2/prompts/`)이고, `settings.json` 의 `prompts_root` 로 다른 곳을 가리킬 수 있다 (settings 파일 기준 상대 경로 허용).

## 레이아웃

```text
prompts/
├── bot_global.md          # 모든 페르소나 공통 서두 (선택)
├── providers/
│   ├── claude.md          # provider 별 추가 지시 (선택)
│   └── codex.md
└── <persona_dir>/
    ├── PROMPT.md          # 필수. @@N 섹션 규약을 따른다
    └── ...                # 참고 자료. @레퍼런스 마커로 경로가 주입되고 --add-dir 로 노출된다
```

`settings.json` → `servers.<guild>.personas.<id>.prompt_dir` 값이 `<persona_dir>` 이 된다.

## PROMPT.md 섹션 규약

`PromptComposer` 는 `## @@N:` 헤더로 문서를 잘라 **페르소나 강도(LV1~5)** 를 만든다.

| 모드 | 이름 | 포함 섹션 |
|---|---|---|
| 1 | core | `@@1` 만 |
| 2 | soft | `@@1` ~ `@@3` |
| 3 | medium | `@@1` ~ `@@4` |
| 4 | hard | `@@1` ~ `@@5` |
| 5 | masquerade | 문서 전체 |

`@@1` 은 항상 들어가는 정체성 핵심이고, 번호가 커질수록 말투·관계 보정처럼 "강도가 높은" 지시를 둔다.

> ExportV2 는 항상 **LV5 (전문)** 로 조립하고 protagonist 를 주입하지 않는다 (`PersonaSession._compose_prompt`). 레벨 절단과 `@주인공` 은 컴포저가 지원하는 기능이며 테스트로 검증되지만, 현재 런타임 경로에서는 쓰이지 않는다.

추가 마커:

- `@레퍼런스` — 한 줄로 단독 등장하면 `추가 레퍼런스 폴더: <persona_dir 절대경로>` 로 치환된다. CLI 가 `--add-dir` 로 그 폴더를 읽을 수 있다.
- `@주인공` — protagonist 가 주어지면 Discord 멘션으로 치환되고, `@@1` 섹션 끝에 `주인공 = <@id>` 한 줄이 삽입된다.

## 예시

`example/PROMPT.md` 를 복사해서 폴더 이름을 바꾸고 `prompt_dir` 에 지정하면 바로 쓸 수 있다.
