"""Optional GTK bridge used to send non-focus-stealing announcements to Orca."""

from __future__ import annotations

import importlib
import platform


GTK = None
if platform.system() == "Linux":
    try:
        # This module is imported before wx so both libraries share the same
        # GTK runtime. Do not call Gtk.disable_setlocale(): importing Gtk has
        # already initialized it on supported PyGObject versions, and calling
        # that function afterward emits a warning.
        gi = importlib.import_module("gi")
        gi.require_version("Gtk", "3.0")
        GTK = importlib.import_module("gi.repository.Gtk")
    except Exception:
        # Orca installations normally provide PyGObject. Its absence must not
        # prevent BlindPilot from starting; status text remains available.
        GTK = None
