# BlindPilot 0.6.2

BlindPilot is an accessible desktop reader for AI coding agents. It is based on Claude
Code Reader and remains available under the MIT License, with credit to the original
project throughout the application and documentation.

## Sign In gets you signed in

Sign In did not work. On Claude Code it could not have: BlindPilot ran `claude /login`,
and `/login` is a slash command typed inside a session, not a command line. Run as one it
opens the terminal interface — with no console to draw in and no keyboard reaching it, it
sat there until it timed out five minutes later, having opened nothing and said nothing.

The other backends failed more quietly. Codex prints the address to sign in on, and
BlindPilot threw its entire output away, so anything it had to say about what was
happening or what had gone wrong was lost. Only FreeBuff was handled, and only because it
had been special-cased.

All of them now run through one sign-in that treats them the same way. It reads the CLI's
output as it arrives, finds the address to sign in on, speaks it, and makes sure it
reaches your default browser: the CLI opens the page itself where it can, BlindPilot opens
it where the CLI will not, and a new **Open Sign-in Page** button opens it again for when
the browser never appeared or was closed by accident. Every line the CLI says on the way
is spoken, colour codes and stray bytes stripped out, so "Waiting for login…" and "Login
failed: Request failed with status code 400" both arrive instead of silence.

Two things had to be got right for Claude Code in particular. Its prompt for the code the
sign-in page hands back — `Paste code here if prompted > ` — is written with no newline
after it, so anything reading the output a line at a time never sees the prompt it is
being asked to answer. It is now read a character at a time, and the prompt opens a box to
paste the code into, which is passed straight to the CLI. That box is a way in rather than
a wall: the same page usually finishes the sign-in on its own, and when it does the box
closes by itself. And Codex announces its own callback server — `http://localhost:1455` —
*before* it prints the page to visit, so the first address in its output is never the one
to open. Loopback addresses are no longer mistaken for the sign-in page.

Whether it worked is no longer taken on trust. When the CLI stops, BlindPilot asks it
whether you are signed in, so a sign-in that succeeded without a tidy exit code is
recognised and one that failed is not reported as success. Closing the wizard, pressing
Escape, or switching backends stops a sign-in that is still running instead of leaving the
process and its half-open browser behind.

opencode is signed in through Connect a Provider, which was already working; it now says
so out loud when a browser could not be opened, and gives you the address to open yourself
rather than asking you to finish in a browser that never came up.

Fifteen tests drive the new sign-in against transcripts taken verbatim from all three
CLIs — the missing newline, the loopback address, the code written back to the CLI, the
failure repeated word for word, the CLI that never finishes, and the one that asks for a
code forever.
