# BlindPilot 0.3.0

BlindPilot is an accessible desktop reader for Claude Code, Codex, and FreeBuff. It is
based on Claude Code Reader and remains available under the MIT License, with credit to
the original project throughout the application and documentation.

## Highlights

- Claude Code, Codex, and FreeBuff backends with matching conversation features.
- Runtime model discovery for every backend; FreeBuff defaults to DeepSeek 4 Pro.
- Automatic NVDA reading after submitting a message.
- Silent until the response mode, replacing the former Classic view wording.
- Secure GitHub release updater with SHA-256 verification.
- Lazy model discovery to avoid the large CPU spike during application startup.
- Accessible Windows and macOS builds, including Intel and Apple Silicon Macs.

## Downloads

- Windows x64: `BlindPilot-Windows-x64.zip`
- macOS Apple Silicon: `BlindPilot-macOS-arm64.zip`
- macOS Intel: `BlindPilot-macOS-x64.zip`

The macOS builds are ad-hoc signed but not Apple-notarized. On first launch, macOS may
require approval in System Settings under Privacy & Security.
