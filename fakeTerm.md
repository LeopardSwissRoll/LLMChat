# fakeTerm — PTY 가상 터미널 시행착오 기록

Windows에서 PTY 프로세스를 래핑하여 "진짜 터미널처럼" 보이게 만들 때 겪은 문제들.

---

## 1. ANSI 이스케이프가 깨져 보임

**증상**: PTY 출력에 `[32m`, `[0m` 같은 텍스트가 그대로 출력됨. 색상/커서 이동 안 됨.

**원인**: Windows 콘솔은 기본적으로 ANSI/VT 이스케이프 시퀀스를 해석 안 함.

**해결**: `SetConsoleMode`로 `ENABLE_VIRTUAL_TERMINAL_PROCESSING` (0x0004) 플래그 활성화.
```python
kernel32.SetConsoleMode(h_out, mode.value | 0x0004)
```
이거 없으면 PTY의 모든 TUI 출력이 의미 없는 문자열 쓰레기.

---

## 2. 키보드 입력이 한 글자씩 안 읽힘

**증상**: `input()`으로 읽으면 Enter 칠 때까지 한 줄을 모아서 줌. CLI의 TUI (탭 완성, 화살표 네비게이션) 불가.

**원인**: Python `input()`은 canonical mode (줄 버퍼링). PTY passthrough에는 character-at-a-time이 필요.

**해결**: Windows에서는 `msvcrt.getwch()` — 한 글자씩 즉시 반환, 에코 없음.
Unix라면 `tty.setraw(sys.stdin)` 후 `sys.stdin.read(1)`.

---

## 3. 방향키/Home/End가 안 먹힘

**증상**: 방향키 누르면 `[A` 같은 문자가 입력됨.

**원인**: Windows `msvcrt.getwch()`는 특수 키를 **2바이트 시퀀스**로 반환:
- 첫 호출: `\x00` 또는 `\xe0` (prefix)
- 둘째 호출: 실제 키 코드 (`H`=Up, `P`=Down, `M`=Right, `K`=Left)

PTY 프로세스는 ANSI 이스케이프 시퀀스 (`\x1b[A` = Up)를 기대함.

**해결**: prefix 감지 → 둘째 바이트 읽기 → ANSI 시퀀스로 변환:
```python
if ch in ("\x00", "\xe0"):
    key = msvcrt.getwch()
    ansi = {"H": "\x1b[A", "P": "\x1b[B", ...}.get(key, "")
    if ansi:
        proc.write(ansi)
```

---

## 4. DA 응답 쓰레기가 stdin에 섞임

**증상**: 시작 직후 입력하면 이상한 문자열(`[?61;6;7;21;22;23;24;28;32;42c`)이 먼저 입력됨.

**원인**: Claude CLI가 시작 시 Device Attributes 쿼리 (`\x1b[c`)를 보냄. 터미널이 응답으로 DA 문자열을 stdin에 넣음. 이걸 PTY에 다시 전달하면 의미 없는 입력이 됨.

**해결**: 시작 후 2초간 stdin 버퍼를 비움 (drain):
```python
drain_end = time.monotonic() + 2.0
while time.monotonic() < drain_end:
    if msvcrt.kbhit():
        msvcrt.getwch()  # 버림
    time.sleep(0.02)
```
2초는 경험적 값. TUI 렌더링 + DA 왕복에 충분한 시간.

---

## 5. Ctrl+C가 프로세스를 즉사시킴

**증상**: Ctrl+C 누르면 Python 프로세스 자체가 죽음. PTY 내부 CLI에 Ctrl+C를 보내고 싶었는데.

**원인**: Python의 기본 SIGINT 핸들러가 `KeyboardInterrupt` 발생. PTY 프로세스가 아니라 래퍼가 죽음.

**해결**: SIGINT 핸들러를 커스텀으로 교체:
- 1번 누름: PTY에 `\x03` (Ctrl+C) 전달
- 1초 내 2번 누름: 래퍼 자체 종료
```python
def on_sigint(_s, _f):
    if now - last_sigint[0] < 1.0:
        stop.set()  # 래퍼 종료
        return
    proc.write("\x03")  # PTY에 전달
```

---

## 6. 터미널 리사이즈가 반영 안 됨

**증상**: 창 크기 바꿔도 PTY 내부 CLI는 원래 크기로 출력. 줄이 잘리거나 줄바꿈 위치가 틀림.

**원인**: PTY는 spawn 시점의 크기를 기억. 터미널 크기 변경을 PTY에 알려줘야 함.

**해결**: reader thread에서 주기적으로 `os.get_terminal_size()` 확인, 바뀌었으면 `proc.setwinsize()` 호출:
```python
new_size = _terminal_size()
if new_size != last_size:
    last_size = new_size
    proc.setwinsize(*new_size)
```

---

## 7. pyte 사용 시 — screen.display에서 IndexError

**증상**: `list(screen.display)` 호출 시 간헐적 `IndexError: string index out of range`.

**원인**: pyte의 `render()` 함수가 빈 char 데이터에 `char[0]` 접근. 한글/wide character 처리 중 버퍼가 깨지면 발생. pyte 라이브러리 버그.

**해결**: 모든 `screen.display` 접근을 try/except로 감싸기:
```python
try:
    display = list(self.screen.display)
except (IndexError, KeyError):
    display = []
```
다음 프레임에서 자연 복구됨. 한 프레임 스킵해도 체감 차이 없음.

---

## 8. PTY 프로세스 종료 시 정리

**증상**: 래퍼 종료해도 PTY 프로세스가 좀비로 남음.

**해결**: 종료 순서:
1. `/exit` 명령 전송 (CLI의 정상 종료 경로)
2. 1초 대기
3. 아직 살아있으면 `proc.terminate()` (강제 종료)

```python
if proc.isalive():
    proc.write("/exit\r")
    time.sleep(1)
    if proc.isalive():
        proc.terminate()
```

---

## 핵심 교훈

1. **Windows PTY는 Unix와 완전히 다르다** — `msvcrt`/`ctypes`/`winpty` 조합이 필수. Unix의 `pty.fork()` + `termios`와는 다른 세계.
2. **DA 쿼리 drain은 경험적 해결** — 깔끔한 방법이 없음. 시작 후 2초 drain이 실전에서 가장 안정적.
3. **pyte는 화면 상태 관리에 유용하지만 버그가 있다** — wide character 처리에서 크래시. 방어 코드 필수.
4. **Ctrl+C 이중 처리는 UX 필수** — 1번은 CLI에 전달, 2번은 래퍼 탈출. 이게 없으면 CLI에서 빠져나올 수 없거나, 반대로 CLI에 Ctrl+C를 보낼 수 없음.
