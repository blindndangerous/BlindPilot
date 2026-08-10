# BlindPilot 0.3.10

BlindPilot is an accessible desktop reader for Claude Code, Codex, and FreeBuff. It is
based on Claude Code Reader and remains available under the MIT License, with credit to
the original project throughout the application and documentation.

## Updates install

- No BlindPilot update has ever installed. The helper that replaces the files was
  started detached from any console, and Windows PowerShell responds to that by exiting
  without running the script it was given — reporting success as it goes. So every
  update downloaded, verified, announced that it was restarting to install, and then
  quietly did nothing. That has been true since 0.3.0.
- **This one has to be installed by hand.** The copy you are running now cannot install
  it, because installing it is the thing that was broken. Every update after this one
  installs on its own.

## An update that fails now says so

- An update finishes after BlindPilot has closed, so a failure had nowhere to be
  reported and was never seen. The reason is now written down as it happens, and read
  out the next time BlindPilot starts, along with where to find the log.
- Abandoned downloads are deleted at startup. A failed update left tens of megabytes
  behind every time it was tried.

## An update no longer has to be lucky

- The contents of the installed folder are replaced rather than the folder renamed, so
  a shortcut, a file sync client, or the application's own working directory can no
  longer block an update.
- BlindPilot waits for everything running out of the installed folder — the agent
  command-line tools it started, and FreeBuff's console host — rather than only the one
  process that asked for the update, and then checks that each file can actually be
  opened before replacing it.
- A move that leaves files behind is retried, which is what a virus scanner reading a
  freshly written file causes.
- If anything goes wrong part way through, the previous version is copied back and its
  backup is kept. BlindPilot is restarted either way.
- BlindPilot is never restarted with the installed folder as its working directory,
  which is what stopped the *following* update from replacing it.

## Included from 0.3.9

## Past conversations come back

- Every backend already stored its conversations and could resume one by id, but there
  was no way to find one again: a conversation existed only while its tab was open.
  File, Recent Conversations (Ctrl+H) now lists them, newest first, each titled by the
  message that started it — which is the only thing that tells two of them apart when
  they are read out.
- Opening one rebuilds it into the same navigable rows a live answer produces, adopts
  the backend's own session id so the next message continues it, and names the tab after
  its first message.
- The list can be filtered as you type, and widened from this folder to every folder, or
  from one backend to all three.
- The context each CLI writes into its own transcript — plugin listings, environment
  blocks, slash-command wrappers — is left out, so a title is what the person typed.

## Compacting, and starting over

- File, Compact Conversation (Ctrl+Shift+K) summarises the conversation so far so the
  backend has room to keep going. Claude Code takes it as its own command; Codex has a
  separate app-server request for it, which BlindPilot now makes.
- FreeBuff's command-line interface has no compaction at all. The command is greyed out
  for it and says so, rather than failing quietly.
- File, Start New Conversation (Ctrl+Shift+N) forgets the current conversation and starts
  a fresh one in the same tab. The old one is still there in Recent Conversations.
- The FreeBuff slash-command list now matches the commands FreeBuff actually has.

## Included from 0.3.8

## FreeBuff answers are read as they are written

- FreeBuff does not save its chat file until a reply has finished, so reading from that
  file meant waiting out the whole answer in silence and then hearing it at once. The
  answer is now read off FreeBuff's own screen, a finished sentence at a time, as it is
  written. Anything that scrolled out of view before it could be read is read out from
  the saved chat when the turn ends, so a long answer is still heard in full and never
  heard twice.

## FreeBuff starts answering straight away

- A FreeBuff terminal takes several seconds to reach the point where it can be given a
  message, and every message waited for that. One is now started ahead of time — when a
  FreeBuff tab opens, and again after each answer for the conversation it belongs to —
  so sending a message reaches FreeBuff in well under a second. Only one is ever held,
  it is dropped after fifteen minutes unused, and a message that finds none simply
  starts its own.
- Choosing a FreeBuff model no longer reads its model catalogue out of the hundred and
  odd megabytes of installed FreeBuff on the way to sending. The catalogue is remembered
  between runs, and is only read again when FreeBuff itself is updated.

## Included from 0.3.7

## Less of the FreeBuff terminal

- BlindPilot claims its console at startup, hidden and parked off screen, so creating a
  terminal has none left to put on screen. A console raised while the terminal is torn
  down is moved off screen as well as hidden, rather than only hidden.

## Included from 0.3.6

## The FreeBuff terminal no longer appears

- Creating a pseudo-terminal attaches a console to BlindPilot itself, and a windowed
  application has no way to ask for that console to be hidden. It is now hidden as it
  appears, for the whole run and its shutdown, so a FreeBuff turn puts nothing on screen.

## Updating works however BlindPilot was installed

- A copy from the setup program now updates by running the new installer silently. The
  previous update replaced the program directory outright, which deleted the uninstaller
  beside it and left Add or Remove Programs pointing at a file that no longer existed.
- An unpacked copy still updates by replacing its own folder, which is right for it.

## Desktop shortcut

- The installer offers a desktop shortcut, and remembers the choice on later updates.
- File, Create Desktop Shortcut makes one at any time, which is how an unpacked copy
  gets one.

## Reasoning lines read as plain text

- Reasoning no longer has "Thinking" in front of it. When it is switched on in Options it
  reads as ordinary text.

## Included from 0.3.5

## FreeBuff works in the installed application

- FreeBuff never ran in the packaged build. It needs a pseudo-terminal, and the build was
  missing the console host that runs a process inside one, so nothing started, nothing was
  reported, and the turn sat silent. That console host is now included.
- This was also a source of stray command windows: with no console host, Windows fell back
  to giving the child a real terminal.
- A terminal that closes before FreeBuff is ready is now reported as an error instead of
  waiting for output that can never arrive.

## Reasoning is no longer read out

- The backend's own thinking is left out of the activity by default, so a turn reads as
  what it did and what it answered.
- Options, "Include the backend's reasoning" turns it back on.

## Included from 0.3.4

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
