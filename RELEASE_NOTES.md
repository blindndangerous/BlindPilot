# BlindPilot 0.5.1

BlindPilot is an accessible desktop reader for AI coding agents. It is based on Claude
Code Reader and remains available under the MIT License, with credit to the original
project throughout the application and documentation.

## Clean-machine backend setup

Every backend can now be installed from the accessible setup wizard on a computer that
starts with none of them. Claude Code keeps its native installer. Codex, FreeBuff, and
opencode get a verified, per-user Node.js LTS automatically when npm is absent, are
installed into a writable per-user folder, added to PATH, and launched once to prove the
installation works. This first launch also makes FreeBuff's npm wrapper download its
native binary before the wizard advances.

FreeBuff sign-in now opens the browser URL that its CLI prints. Its current off-peak
DeepSeek V4 Pro entry remains available in BlindPilot's model picker, and only a complete,
parseable FreeBuff device credential is reported as signed in.
