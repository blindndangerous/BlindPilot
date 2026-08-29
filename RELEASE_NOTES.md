# BlindPilot 0.6.3

BlindPilot is an accessible desktop reader for AI coding agents. It is based on Claude
Code Reader and remains available under the MIT License, with credit to the original
project throughout the application and documentation.

## Every backend gets a PATH that can reach Node

An application opened from the macOS Dock inherits launchd's PATH — `/usr/bin:/bin:/usr/sbin:/sbin`
— and nothing else. Every provider CLI that npm installs is a `#!/usr/bin/env node` shim,
so a child handed that PATH dies on `env: node: No such file or directory` before it
prints anything a person could act on. FreeBuff's pseudo-terminal was the one spawn site
that passed no environment at all, which is exactly why FreeBuff never ran on a Mac and
every other backend did: theirs already put the CLI's own folder first.

That environment is now built in one place, it carries the PATH a login shell would have
given, and everything that starts a CLI uses it — the sign-in, the auth probes, the
version checks, npm itself. And a terminal that died during start-up no longer reports a
guess: it reports its own last words, which is the only part of it anyone can act on.

## What BlindPilot starts, BlindPilot stops

Half of these CLIs are launchers rather than programs. npm's `codex` and `freebuff` are
Node scripts that run the real agent as a child, so killing the launcher left that child
running — still holding its lock, still waiting on a sign-in nobody was completing.

Children now start in a process group of their own and are stopped as one. The group is
only ever signalled when the child is demonstrably its leader, because a child still
sitting in our own group would have us signal ourselves.

## FreeBuff picks the model you chose

FreeBuff has dropped `deepseek-v4-pro`. A remembered model was offered whether or not the
installed release still had it, so the picker was driven looking for a row that never
appears and the message was lost five seconds later.

Underneath that, the picker only parsed three of its five rows, because FreeBuff's display
names disagree with its ids about where a version letter goes — `mimo/mimo-v2.5` is drawn
"MiMo 2.5". Navigation counts arrow presses as the distance between two positions in that
list, so a row that failed to parse did not cost only its own model: it silently selected
the wrong one for every model below it.

A model the installed release has dropped is no longer offered, and one that goes missing
mid-run falls back audibly rather than costing the turn.

## Sound, and a build that could not speak

The progress earcon watched an event that the next turn cleared while the previous thread
was still inside `wait()`, so one cue became several playing over each other — and a
player that could not play the file at all spun, spawning processes as fast as the machine
allowed. Both are fixed.

The packaged smoke test now fails on macOS if AppKit did not make it into the bundle.
AppKit is how anything is said to VoiceOver, and a build that packaged everything else and
dropped it starts, runs, and is silent.

## Windows

Unchanged by design: the process-group flags are empty there, the login shell is never
asked, and the pywinpty spawn is untouched.
