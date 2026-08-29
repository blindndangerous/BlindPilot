# BlindPilot 0.7.0

BlindPilot is an accessible desktop reader for AI coding agents. It is based on Claude
Code Reader and remains available under the MIT License, with credit to the original
project throughout the application and documentation.

## Orca hears BlindPilot on Linux

BlindPilot's live announcements now reach Orca without moving keyboard focus. A bare ATK
object is not part of the application's accessible tree, so Orca correctly ignores it;
BlindPilot now creates a real off-screen GTK accessible and emits announcements through
that object instead. It uses GTK's native C API so wxPython and PyGObject do not both try
to initialize GTK and produce warnings.

The GTK bridge is optional. If the native GTK libraries are unavailable, BlindPilot still
starts normally and leaves the same message in the status bar for the review cursor.

## macOS updates recover instead of disappearing

The old macOS updater closed BlindPilot and attempted one unreported bundle swap. If the
application was running from macOS App Translocation, the folder was not writable, the
archive could not be expanded, Gatekeeper quarantined the replacement, or the relaunch
failed, BlindPilot could simply vanish.

Those failures are now handled deliberately:

- Translocated and unwritable application bundles are rejected while BlindPilot is still
  open, with instructions that can be acted on.
- A detached helper waits for BlindPilot to close and prevents two windows from updating
  the same bundle at once.
- Verified updates have quarantine removed before and after installation so macOS can
  launch them.
- The current application is kept as a backup until the new copy is installed and ready.
  If anything fails, the previous version is restored and reopened.
- Failures are logged and reported on the next launch instead of being lost after the
  window closes. Relaunch falls back to the executable if Launch Services refuses the
  bundle.

## Python 3.13 and release coverage

The opencode backend's event handler was named `_handle`, which collides with a private
attribute Python 3.13 adds to every `threading.Thread`. On that Python version, opencode
events could try to call the runtime's internal handle as a method. The handler now has a
non-conflicting name, with a regression test that reproduces the collision on every
supported Python version.

POSIX login-shell PATH discovery is now tested without depending on the host shell, and
release builds explicitly compile the new Linux accessibility bridge.
