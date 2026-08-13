# Changelog

Readable release history for BlindPilot. When adjacent releases were part of the
same fix stream, they are combined with a version range such as
`v0.3.6-v0.3.7`.

## v0.3.12 - 2026-08-12

- Install updates on an installed copy, which since 0.3.10 ended in the installer stopping and reporting nothing but "code 5". BlindPilot handed its own library folder to every program it started, and to everything those started in turn, so an agent CLI or a tool it left running went on holding BlindPilot's libraries open for hours. The installer found its files in use, asked those programs to close, and — with its message boxes suppressed, as a silent update requires — silently answered Abort and rolled the update back.
- Keep the packaged library folder off the environment BlindPilot hands its children, so nothing outside the application loads out of the install folder in the first place.
- Notice a program that has one of our libraries loaded, not only one that runs from the install folder, and close it before the installer has to. The old check looked at where a process runs from and so found none of the programs that were actually holding files.
- Let the installer close what refuses to close, so one stubborn program can no longer abort an update.
- Check that every file can be opened before starting the installer, as the portable update already did.
- Say what went wrong when an installed update fails, rather than reading out the installer's exit code, and keep the installer's own log beside the reason.
- Stand aside when a second BlindPilot window starts an update at the same time, instead of two installers racing over one folder.

## v0.3.11 - 2026-08-12

- Stay in the responses when Down is pressed on the newest row, in the list and in the read-only edit field alike, rather than dropping focus into the prompt. Tab is the way to the prompt.
- Keep the row you are reading when streamed output rebuilds the list, which on Windows clears the selection.
- Apply backend output in small batches that yield to keyboard and screen-reader events, so a long, chatty job no longer leaves arrow keys unanswered, and redraw the list once per batch.
- Keep FreeBuff on DeepSeek V4 Pro. FreeBuff dated the model's name ("DeepSeek V4 Pro 08/13") without dating its documentation, so BlindPilot dropped Pro from the model list and fell back to FreeBuff's own setting, which FreeBuff rewrites to Flash after every turn.
- Re-apply the chosen FreeBuff model before replacing a terminal mid-message, since the terminal being replaced rewrites that setting as it exits.

## v0.3.10 - 2026-08-10

- Install updates at all. No update since 0.3.0 has ever been applied: the helper that does the work was started in a way that made Windows PowerShell exit without running it, and report success while doing so.
- Replace the contents of the installed folder rather than renaming the folder, so a shortcut, a sync client, or the application's own working directory cannot block an update.
- Wait for everything running out of the installed folder, not just the one process that asked for the update, and confirm each file can be opened before replacing it.
- Retry a move that leaves files behind, which is what a virus scanner reading a freshly written file causes.
- Put the previous version back, by copy rather than by move, if anything goes wrong part way through, and keep the backup.
- Say why an update failed. An update finishes after BlindPilot has closed, so the reason is written down and read out at the next start, with the path to a log.
- Update an installed copy into its existing location, and restart it even when the installer reports a problem.
- Never restart BlindPilot with the installed folder as its working directory, which is what stopped the following update from replacing it.
- Delete abandoned update downloads at startup; a failed update used to leave tens of megabytes behind every time.

## v0.3.9 - 2026-08-10

- Reopen a past conversation from any backend and carry on with it, from File, Recent Conversations (Ctrl+H).
- Title every past conversation by the message that started it, so two in the same folder can be told apart when read aloud.
- Filter the conversation list as you type, and widen it to every folder or every backend.
- Rebuild a reopened conversation into the same navigable rows a live one produces, and name its tab after its first message.
- Leave out the context each CLI writes into its own transcript, so a plugin listing or an environment block never becomes a title.
- Compact a conversation in place from File, Compact Conversation (Ctrl+Shift+K), through Claude Code's own command and through Codex's app-server request.
- Start a fresh conversation in the current tab from File, Start New Conversation (Ctrl+Shift+N).
- Say plainly that FreeBuff cannot compact, and grey the command out, instead of failing silently.
- Correct the FreeBuff slash-command list to the commands FreeBuff actually has.

## v0.3.8 - 2026-08-09

- Read a FreeBuff answer off its own screen as it is written, a finished sentence at a time, instead of waiting out the whole reply in silence.
- Read out from the saved chat anything that scrolled away before it could be spoken, so a long answer is heard in full and never twice.
- Keep a FreeBuff terminal waiting so a message reaches it in well under a second rather than several.
- Remember FreeBuff's model catalogue between runs instead of reading it out of the installed application on the way to sending.

## v0.3.7 - 2026-08-09

- Claim BlindPilot's console at startup, hidden and parked off screen, so creating a terminal has none left to put on screen.

## v0.3.6 - 2026-08-09

- Hide the console a pseudo-terminal attaches to BlindPilot, for the whole run and its shutdown, so a FreeBuff turn puts nothing on screen.
- Update an installed copy by running the new installer silently, instead of replacing the program directory and deleting its own uninstaller.
- Offer a desktop shortcut in the installer, and add File, Create Desktop Shortcut for copies that were unpacked rather than installed.
- Read reasoning as ordinary text, without "Thinking" in front of it.

## v0.3.5 - 2026-08-09

- Ship the console host FreeBuff needs, so FreeBuff runs in the packaged application at all.
- Stop stray command windows appearing, which was Windows falling back to a real terminal when the console host was missing.
- Report a terminal that closes before FreeBuff is ready, instead of waiting for output that can never arrive.
- Leave the backend's own reasoning out of the activity by default, with an Options setting to put it back.

## v0.3.4 - 2026-08-09

- Run Claude Code without a terminal window, which had been appearing for the whole turn and taking focus from the screen reader.
- Run every backend helper without one too: sign-in checks, model and version queries, and the Codex app server.
- Add a Stop button beside Steer, and File, Stop Task (Ctrl+period), enabled only while a task is running.
- Keep what a stopped task already produced as that turn's response, and report "Stopped" rather than an error.
- Show a FreeBuff session's second and later answers, which had been replaced by the previous turn's answer.
- Keep FreeBuff on DeepSeek 4 Pro, which FreeBuff had been silently downgrading by rewriting its own settings after every turn.
- Drop the interruption marker FreeBuff leaves behind when a session closes, instead of reading it out as part of the answer.
- Add a finished answer to the list on completion when it never arrived as live activity, for every backend.

## v0.3.3 - 2026-08-09

- Force the main window through the normal shutdown path after an update is verified, so the application releases its files before they are replaced.
- Stage a complete new installation, wait for the old process to exit, swap directories, then launch and check the new version.
- Restore and reopen the previous version if replacement or startup fails, instead of leaving a partial installation.
- Remove obsolete files from older builds as part of the full-directory swap.
- Report installer startup failures through the accessible update error dialog.

## v0.3.2 - 2026-08-09

- Keep the selected FreeBuff model by navigating its runtime picker to it, rather than accepting the Flash model it recommends.

## v0.3.1 - 2026-08-09

- Report agent and tool activity, plus a heartbeat every thirty seconds, so a long FreeBuff task no longer appears frozen.
- Read FreeBuff's structured live chat state for accurate reasoning and answers, keeping terminal parsing as a fallback.
- Detect FreeBuff completion from its authoritative per-chat log rather than from ambiguous terminal redraws.
- Save a new FreeBuff session id immediately, so interrupted work can be resumed.
- Stop mistaking advertisements and terminal tool cards for the assistant's answer.
- Stop narrating text twice when switching from terminal fallback to structured data.

## v0.3.0 - 2026-08-09

- Claude Code, Codex, and FreeBuff backends, with matching conversation features across all three.
- Runtime model discovery for every backend, with FreeBuff preferring DeepSeek 4 Pro.
- Automatic screen-reader narration after a message is submitted.
- Silent-until-response mode, which stays quiet until the complete answer is ready.
- Secure GitHub release updater with SHA-256 verification.
- Lazy model discovery, avoiding a large CPU spike at startup.
