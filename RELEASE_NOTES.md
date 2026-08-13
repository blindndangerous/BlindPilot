# BlindPilot 0.3.13

BlindPilot is an accessible desktop reader for Claude Code, Codex, and FreeBuff. It is
based on Claude Code Reader and remains available under the MIT License, with credit to
the original project throughout the application and documentation.

## Upgrading by hand works too

0.3.12 fixed the update that BlindPilot installs itself. Running the setup program by
hand still failed, and it failed in the way that is hardest to argue with: a box saying
one of BlindPilot's own files could not be replaced, access denied, over an application
that was closed.

- The two upgrades were never doing the same thing. The updater inside BlindPilot tells
  the installer it may close a program outright; run by hand, the installer only asked,
  waited half a minute, and gave up. What it was asking was a background program with no
  window to close and nobody watching it, which had loaded one of BlindPilot's libraries
  back when 0.3.10 leaked them onto its children's PATH. It never answered, and the only
  choices left were to abort the upgrade or to try again and abort it later.
- The setup program now closes what refuses to close however it was started, so an
  upgrade by hand behaves the same as one BlindPilot installs on its own. Nothing it
  closes is a bystander: it is holding a file the upgrade is about to replace, and it has
  already been asked to let go.

Once 0.3.12 or later is installed, BlindPilot no longer hands its library folder to the
programs it starts, so nothing outside the application should be holding one of these
files in the first place. This is what happens when something still is.
