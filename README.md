# BlindPilot

BlindPilot is an accessible desktop frontend for Claude Code, OpenAI Codex,
and FreeBuff. It uses native wxPython controls so screen readers can navigate
prompts, responses, live activity, settings, and multiple conversations without
having to interpret a terminal interface.

> BlindPilot is based on the original
> **[Claude Code Reader](https://github.com/doubletaponair/claude-code-reader)**
> by [doubletaponair](https://github.com/doubletaponair). We gratefully credit
> its authors and contributors for the foundational accessibility-first design.
> BlindPilot preserves that work and extends it with a persistent, pluggable
> backend system. See [CREDITS.md](CREDITS.md).

BlindPilot is released under the [MIT License](LICENSE).

## Updates

BlindPilot checks the public GitHub Releases feed shortly after startup. It only
offers versions newer than the running application and never installs without
confirmation. Packaged builds download the matching Windows or macOS archive,
verify its published SHA-256 digest, install after BlindPilot exits, and restart
the application. Use **Help → Check for Updates** to check immediately.

## Backend selection

Open **File → Backend** and choose **Claude Code**, **Codex**, or **FreeBuff**.
Claude Code is selected on a fresh installation. Your choice is saved globally and
used for all subsequent turns and launches until you change it again. If you
switch while a tab already has a conversation, BlindPilot starts a new
conversation with the new backend on that tab's next send.

Open **File → Manage Backends** at any time to run the accessible installer,
updater, and sign-in flow for the selected provider. The same provider picker is part of
first-run setup, so Claude Code is no longer required before Codex or FreeBuff
can be used.

| Backend | Integration | Sessions | Model control | Permission modes |
|---|---|---:|---:|---:|
| Claude | Streaming JSON CLI | Yes | Yes | Yes |
| Codex | Official app-server JSONL protocol | Yes | Yes, including installed reasoning levels | Yes |
| FreeBuff | Pseudo-terminal adapter | Yes | Yes; DeepSeek V4 Pro by default | Managed by FreeBuff |

FreeBuff currently provides no JSON or headless API. BlindPilot therefore runs
its terminal UI in a hidden pseudo-terminal, extracts reasoning and answer
updates for the accessible response list, and captures FreeBuff's saved chat ID
for continuation. Terminal redraws and advertisements are filtered before text
is announced. Its permission picker is disabled because the FreeBuff CLI does
not expose equivalent permission controls.

Model catalogs are discovered from each installed backend at runtime instead of
being permanently compiled into BlindPilot. Use **/model** for the recent list,
or **/models** to discard caches and force a fresh provider query. FreeBuff's
catalog is read from its installed picker, and `deepseek/deepseek-v4-pro` is the
default until you choose another available FreeBuff model.

## Features

- Native, screen-reader-friendly controls on Windows and macOS.
- One navigable row per heading, paragraph, list item, quote, code block,
  thought, tool action, or tool result.
- Optional live narration through NVDA, JAWS, VoiceOver, or another available
  accessibility output.
- Multiple project sessions with independent backend conversations.
- Steering messages while a backend is working.
- File and clipboard-image attachments represented as explicit prompt paths.
- Search, response jumping, whole-response copy, code copy, and code saving.
- Provider-aware model, reasoning-effort, permission, and slash-command controls.
- Earcons for sent, working, and received states.

Live rows and automatic narration are enabled by default. The Options menu can
turn narration off, use a read-only text field for easier screen-reader review,
or select **Silent until the response mode** to remain quiet until an answer is complete.

## Install and run

Python 3.10 or newer is recommended.

```powershell
python -m pip install -r requirements.txt
python blind_pilot.py
```

Install and authenticate at least one backend:

```powershell
# Claude Code
claude --version
claude /login

# OpenAI Codex
npm install -g @openai/codex
codex login

# FreeBuff
npm install -g freebuff
freebuff login
```

BlindPilot's first-run and **Manage Backends** wizards can locate, install,
update, add to PATH, and authenticate Claude Code, Codex, or FreeBuff. A missing selected
CLI is reported as an actionable error without affecting the other installed
backends.

Configuration is stored in `%APPDATA%\BlindPilot\config.json` on Windows and
`~/.config/blindpilot/config.json` elsewhere. Existing Claude Code Reader
configuration is imported once from its legacy location and is never modified.

## Keyboard workflow

- **Ctrl+L**: focus the prompt.
- **Ctrl+T**: open a session.
- **Ctrl+W**: close the current session.
- **Ctrl+F**: search responses.
- **Ctrl+R**: jump to the latest response.
- **Ctrl+/**: open provider-aware slash commands.
- **Ctrl+Shift+A**: attach files.
- **Ctrl+Shift+M**: cycle common permission modes.
- **Ctrl+Shift+[ / ]**: previous or next session.
- **Ctrl+1 through Ctrl+9**: jump directly to a session.
- **Up** from the prompt's first line: move to the newest response row.
- **Down** from the newest response row: return to the prompt.

On macOS, wxPython maps the same accelerator definitions to Command where
appropriate.

## Development

```powershell
python -m pytest -q
python -m py_compile blind_pilot.py blindpilot_app.py claude_reader.py agent_backends.py markdown_rows.py
```

The stable entry point is `blind_pilot.py`, and the implementation lives in
`blindpilot_app.py`. `claude_reader.py` is a compatibility alias for scripts and
integrations written for the original application.

The release build uses PyInstaller's one-directory layout so the verified
updater can replace the application safely after it exits. Local example:

```powershell
python -m pip install -r requirements-build.txt
pyinstaller --onedir --windowed --name BlindPilot --add-data "EarCons;EarCons" blind_pilot.py
```

Pushing a `v*` tag runs [.github/workflows/release.yml](.github/workflows/release.yml),
tests startup and the full suite, and publishes Windows x64 plus Intel and Apple
Silicon macOS archives with SHA-256 files.

The original build document is retained as
[`original-claude-code-reader-spec.html`](original-claude-code-reader-spec.html)
for historical attribution. It describes the original Claude-only scope, not
the current multi-backend architecture.

## License and credit

Copyright (c) 2026 doubletaponair and BlindPilot contributors.

Licensed under the MIT License. BlindPilot's name and later multi-backend work
do not erase its origin: the original
[Claude Code Reader](https://github.com/doubletaponair/claude-code-reader) by
[doubletaponair](https://github.com/doubletaponair) is credited in the README,
About dialog, source headers, historical specification, and
[CREDITS.md](CREDITS.md).
