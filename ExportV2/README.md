# ExportV2

`ExportV2/` is a minimal Discord backend that keeps only:

- one Discord bot token
- per-channel persona PTY sessions (one session per `(channel, persona)`)
- role mention -> persona webhook reply
- single LLM provider per deployment (Claude or Codex, chosen via `llm.provider`)
- no slash commands
- no panel/UI system
- no channel context replay

## Behavior

- The bot reacts in any channel of a configured server when a persona role is mentioned.
- Each `(channel_id, persona_id)` gets its own PTY session, created lazily on first mention.
- The PTY input for a turn is reduced to:

```text
[display_name:uid] : message text
```

- Persona prompts still come from `v2/prompts/...` through the existing `PromptComposer`.
- Runtime state is stored under `data_root/{server_id}/state.json` (per-channel, per-persona `cli_session_id`).
- Per-session scratch (CODEX_HOME, prompt files) lives under `data_root/{server_id}/{channel_id}/{session_hash}/`.
- Call/response logs live under `bridge_root/{server_id}/{channel_id}/{message_id}.txt` and that directory is passed to the CLI as `--add-dir`.
- If there is a saved `cli_session_id` and the channel log directory has any `.txt` files, the session will resume on restart.

## Run

```powershell
python -m ExportV2
python -m ExportV2 --settings ExportV2/settings.json
```

## Settings

See [settings.example.json](settings.example.json) — copy it to `settings.json` and fill in `bot_token` / `servers`.
