# BlindPilot 0.3.3

BlindPilot is an accessible desktop reader for Claude Code, Codex, and FreeBuff. It is
based on Claude Code Reader and remains available under the MIT License, with credit to
the original project throughout the application and documentation.

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
