# BlindPilot 0.3.12

BlindPilot is an accessible desktop reader for Claude Code, Codex, and FreeBuff. It is
based on Claude Code Reader and remains available under the MIT License, with credit to
the original project throughout the application and documentation.

## Updates install again on an installed copy

An update on a copy put there by the installer has been failing since 0.3.10, and all it
left behind was a line saying the installer exited with code 5. That number means the
installation was cancelled — by nobody, since a silent update has nobody at the keyboard.

The installer had asked to close the programs holding BlindPilot's files, waited half a
minute, and been refused. It then showed the box that asks whether to abort, retry or
ignore; message boxes are suppressed during a silent update, so the box answered itself
with its default of Abort, and the update was rolled back.

- The programs refusing to close were BlindPilot's own doing. The packaged application
  put its private library folder on its PATH, every program BlindPilot started inherited
  it, and so did everything those started in turn — an agent CLI, the tools it runs, a
  terminal left open. Hours after BlindPilot had closed, unrelated programs were still
  loading its copy of the Visual C++ runtime and holding it open. That folder is now kept
  off the environment handed to child processes, so nothing outside the application loads
  out of the install folder in the first place.
- The updater's own check for programs still using the folder only asked where each
  program was running from. Every program that was actually holding a file was running
  from somewhere else entirely, so the check found none of them and declared the folder
  free. It now also asks which programs have one of our libraries loaded — what the
  installer itself looks at — and closes them before the installer has to.
- The installer is now allowed to close what still refuses to close, so one stubborn
  program can no longer abort an update.
- Every file is confirmed to be openable before the installer starts, which the portable
  update already did and the installed one did not.

## A failed update says what happened

- An update that fails on an installed copy now reports the reason in words — that files
  were in use, that the installer could not start, that the computer needs a restart —
  instead of reading out a number, and the installer's own log is kept beside it for a
  bug report.
- Two BlindPilot windows checking for updates at the same time no longer start two
  installers over the same folder, each guaranteeing the other finds files in use. The
  second one steps aside and lets the first finish.
