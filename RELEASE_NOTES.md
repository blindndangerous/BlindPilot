# BlindPilot 0.3.4

BlindPilot is an accessible desktop reader for Claude Code, Codex, and FreeBuff. It is
based on Claude Code Reader and remains available under the MIT License, with credit to
the original project throughout the application and documentation.

## No console windows

- Claude Code now runs without a terminal window. It was appearing on screen for the
  whole turn and taking focus away from the screen reader.
- The same applies to every backend helper process: sign-in checks, model and version
  queries, and the Codex app server.

## Stopping a task

- A Stop button sits next to Steer, enabled only while a task is running, and File, Stop
  Task (Ctrl+period) does the same from anywhere in the session.
- Stopping keeps whatever the task already produced, records it as that turn's response,
  and reports "Stopped" rather than an error.

## FreeBuff responses

- A second and later message in a FreeBuff session now shows its own answer. The previous
  turn's answer was being narrated again in its place, and the new one never reached the
  list or the read-only text field.
- FreeBuff answers are narrated once, from its saved chat file, instead of from the file
  and the hidden terminal at the same time.
- The interruption marker FreeBuff leaves behind when a session closes is no longer read
  out as part of the answer.
- A finished answer that never arrived as live activity is added to the list on
  completion, for every backend.

## FreeBuff stays on DeepSeek 4 Pro

- FreeBuff rewrites its own settings to its recommended Flash model once a turn has run,
  which silently downgraded every following turn. BlindPilot now keeps its own record of
  the selected model and uses that, so Pro remains the default.
- A model chosen with /model still overrides it and now survives the same reset.

## Included from 0.3.3

## Updater reliability

- BlindPilot now forces its main window through the normal shutdown path after an update
  is verified, so the installed application releases its files before replacement.
- The Windows updater stages a complete new installation, waits for the old process to
  exit, swaps directories, and then launches and checks the new version.
- If shutdown stalls, the helper uses a bounded forced-close fallback without replacing
  files while the old process is still running.
- Failed replacement or startup restores and reopens the previous version instead of
  leaving a partial installation.
- Obsolete files from older PyInstaller builds are removed by the full-directory swap.
- Installer startup failures are now reported through BlindPilot's accessible update
  error dialog.

## Included from 0.3.2

- Long-running FreeBuff tasks now report agent/tool activity and a progress heartbeat
  every 30 seconds instead of appearing frozen.
- BlindPilot reads FreeBuff's structured live chat state for accurate reasoning and
  response narration, while retaining terminal parsing as a fallback.
- FreeBuff completion is detected from its authoritative per-chat log rather than from
  ambiguous terminal redraws.
- New FreeBuff session IDs are saved immediately, allowing interrupted work to be
  resumed.
- Ads and terminal tool cards are no longer misidentified as assistant responses.
- Switching from terminal fallback to structured data no longer narrates text twice.
- BlindPilot now navigates FreeBuff's runtime model picker to the requested model instead
  of accepting its highlighted Flash recommendation; DeepSeek 4 Pro remains selected.

## Included from 0.3.0

- Claude Code, Codex, and FreeBuff backends with matching conversation features.
- Runtime model discovery for every backend; FreeBuff prefers DeepSeek 4 Pro.
- Automatic NVDA reading after submitting a message.
- Silent until the response mode keeps activity quiet until the complete answer is ready.
- Secure GitHub release updater with SHA-256 verification.
- Lazy model discovery avoids the large CPU spike during application startup.

## Downloads

- Windows x64 setup: `BlindPilot-Setup-x64.exe`
- Windows x64: `BlindPilot-Windows-x64.zip`
- macOS Apple Silicon: `BlindPilot-macOS-arm64.zip`
- macOS Intel: `BlindPilot-macOS-x64.zip`

The macOS builds are ad-hoc signed but not Apple-notarized. On first launch, macOS may
require approval in System Settings under Privacy & Security.
