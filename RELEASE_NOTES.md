# BlindPilot 0.4.0

BlindPilot is an accessible desktop reader for AI coding agents. It is based on Claude
Code Reader and remains available under the MIT License, with credit to the original
project throughout the application and documentation.

## opencode is the fourth backend

Pick it from File → Backend, or install and sign into it from the setup wizard, the same
way as the other three. Everything BlindPilot does for Claude Code and Codex, it now does
for opencode: answers read out as they arrive, tool activity and reasoning as navigable
rows, steering a turn while it runs, stopping one, permission modes, compacting a
conversation, and reopening a past one to carry on with it.

It is driven through the headless server opencode's own terminal interface talks to,
rather than a process per turn. BlindPilot starts one, shared by every tab, on the
loopback interface behind a password made up for that run. That is the only surface that
exposes all of this at once, which is why nothing had to be left out.

- **`/model` covers everything opencode can reach**, named `provider/model`, with the
  reasoning variants each model offers. The list is read per directory, because a
  project's own `opencode.json` can pin a model or turn providers off, and a reasoning
  level a model does not offer is never sent with it.
- **`/connect` is opencode's own command, as a dialog.** Every provider it knows, the
  connected ones first, signed in with an API key or through your browser. Providers that
  want an account id or a self-hosted address ask for it in opencode's own words. It is
  also the wizard's sign-in step, because opencode's command-line sign-in is a terminal
  prompt nobody using a screen reader can answer.
- **Its own commands run as commands.** `/init`, `/review`, and anything the project
  defines are offered in the slash picker and handed to opencode to expand. A `/name` it
  does not recognise is left alone, so a sentence that happens to start with a slash is
  still a sentence.
- **Permission modes are rules opencode enforces**, not instructions to a model. Plan mode
  selects opencode's own plan agent and denies edits outright; accept-edits allows edits
  while a shell command keeps the normal safeguard; default leaves opencode's own
  configuration alone. A question opencode stops to ask mid-turn is declined and reported
  — unanswered, it would hold the turn open for good.
- **Past conversations come out of opencode's database**, read-only, titled by their first
  message where opencode has not titled them itself.

## Also in this release

- Steering an opencode turn no longer waits on a request from the window's own thread.
- Every opencode conversation in a directory is offered, not only the ones among its most
  recent few hundred across all directories.
- opencode's sign-in check no longer counts any `*_API_KEY` in your environment as an
  opencode sign-in.

Nothing about Claude Code, Codex, or FreeBuff changes in this release.
