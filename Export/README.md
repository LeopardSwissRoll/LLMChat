# Export

`Export/` is a standalone mini terminal engine extracted from `terminalist`.

It intentionally keeps only the pieces needed for:

- PTY lifecycle
- pyte-backed virtual terminal state
- Windows console input reading
- user input -> PTY write routing
- optional raw-output passthrough to stdout

It intentionally does **not** include:

- panes / windows / split layout
- compositor / frame diff rendering
- copy mode
- app-level multiplexing

## Layout

- `vt/`
  - PTY backend
  - pyte patch
  - `VirtualTerminal`
- `io/`
  - Win32 console input
  - key translation
  - pure input routing
- `bridge/`
  - thin loop that connects console input and a `VirtualTerminal`
- `tests/`
  - unit tests copied/adapted from `Terminalist`
  - manual integration tests for real console / PTY paths

## Example

```python
from pathlib import Path

from Export.vt.virtual_terminal import VirtualTerminal
from Export.bridge.console_session import ConsoleSession

vt = VirtualTerminal(
    "demo",
    ["powershell.exe"],
    Path.cwd(),
    cols=120,
    rows=30,
)
ConsoleSession(vt).run()
```

## Run directly

```powershell
python -m Export
python -m Export -- cmd.exe
python -m Export --cwd C:\path\to\workspace -- powershell.exe
```

## Test commands

```powershell
python -m pytest -q Export/tests/unit
python Export/tests/integration/test_io.py
python Export/tests/integration/test_console_input_integration.py
```
