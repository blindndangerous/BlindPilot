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


def test_downloaded_update_schedules_before_forced_close(monkeypatch, tmp_path):
    import blindpilot_app

    events = []
    archive = tmp_path / "update.zip"
    archive.write_bytes(b"verified")
    release = blindpilot_app.ReleaseInfo(
        version="9.9.9",
        tag="v9.9.9",
        title="Update",
        notes="",
        page_url="https://github.com/release",
        asset_name="BlindPilot-Windows-x64.zip",
        asset_url="https://github.com/download/update.zip",
        asset_size=archive.stat().st_size,
        sha256="0" * 64,
    )

    class FakeFrame:
        def _announce_setting(self, message):
            events.append(("announce", message))

        def _show_update_error(self, message):
            events.append(("error", message))

        def Close(self, *, force=False):
            events.append(("close", force))

    monkeypatch.setattr(
        blindpilot_app,
        "schedule_install",
        lambda selected: events.append(("schedule", selected)),
    )

    blindpilot_app.MainFrame._on_update_downloaded(FakeFrame(), archive, "", release)

    assert events[0] == ("schedule", archive)
    assert events[-1] == ("close", True)
    assert not [event for event in events if event[0] == "error"]
