# BlindPilot 0.3.14

BlindPilot is an accessible desktop reader for Claude Code, Codex, and FreeBuff. It is
based on Claude Code Reader and remains available under the MIT License, with credit to
the original project throughout the application and documentation.

## Nothing you can see

This release changes nothing about what BlindPilot does. If 0.3.13 is working, there is
no reason to hurry.

A type checker was run over the three modules that ship, and the thirty-two things it
objected to were settled. Most were objects the code passed around without naming,
because they honestly come from more than one place — the worker for whichever backend
you picked, the terminal FreeBuff runs under, which is winpty's on Windows and pexpect's
elsewhere. Each now states what is asked of it, which is both shorter than the comment
explaining it would have been and still true when a fourth backend arrives.

Two were worth the trouble on their own. Neither could happen as the code stood; both
depended on an argument the next reader would have had to reconstruct.

- The macOS announcement that VoiceOver speaks read six names that exist only on macOS.
  Nothing on Windows or Linux reached them, but nothing there said so either. It now
  loads them where it uses them, and a failed announcement no longer travels any further
  than the announcement.
- The sign-in helper killed a process in a handler that could be entered before that
  process existed. Only the wait can time out, so it never was — the timeout is now
  caught where the process is known to be there.

The upgrade fix from 0.3.13 is unchanged and still the reason to be on a recent version:
running the setup program by hand no longer stops on a file it cannot replace.
