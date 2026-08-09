"""Regression tests for non-interactive startup checks."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_gui_startup_smoke_skips_first_run_wizard(monkeypatch):
    import blindpilot_app

    events: list[object] = []

    class FakeApp:
        def MainLoop(self) -> None:
            events.append("main-loop")

    class FakeFrame:
        def __init__(self, *, initial_cwd: str) -> None:
            events.append(("frame", initial_cwd))

        def Show(self) -> None:
            events.append("show")

        def Raise(self) -> None:
            events.append("raise")

        def Close(self) -> None:
            events.append("close")

    def fail_if_wizard_opens(*_args, **_kwargs):
        raise AssertionError("the first-run wizard opened during a GUI smoke test")

    monkeypatch.setattr(blindpilot_app.sys, "argv", ["blind_pilot.py", "--startup-gui-smoke"])
    monkeypatch.setattr(blindpilot_app, "_load_config", lambda: {})
    monkeypatch.setattr(blindpilot_app, "SetupWizard", fail_if_wizard_opens)
    monkeypatch.setattr(blindpilot_app, "MainFrame", FakeFrame)
    monkeypatch.setattr(blindpilot_app, "_bring_to_front", lambda: events.append("front"))
    monkeypatch.setattr(blindpilot_app.wx, "App", lambda _redirect: FakeApp())

    def call_later(delay: int, callback) -> None:
        events.append(("later", delay))
        callback()

    monkeypatch.setattr(blindpilot_app.wx, "CallLater", call_later)

    assert blindpilot_app.main() == 0
    assert ("later", 1500) in events
    assert "close" in events
    assert "main-loop" in events
