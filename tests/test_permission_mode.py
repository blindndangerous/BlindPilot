"""Unit tests for where a session's permission mode comes from.

A new tab starts at the mode you last chose in this app; if you have never
chosen one, it falls back to whatever Claude Code itself is configured to use,
and only then to "default".

Run from the project root:

    python -m pytest tests/ -q
    # or, with no pytest installed:
    python tests/test_permission_mode.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import claude_reader  # noqa: E402
from claude_reader import (  # noqa: E402
    _claude_config_permission_mode,
    _default_permission_mode,
    _remember_permission_mode,
)


def _write_settings(path: Path, mode) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"permissions": {"defaultMode": mode}} if mode is not None else {}
    path.write_text(json.dumps(body), encoding="utf-8")


class _Sandbox:
    """Temp user config dir + temp project, with the app's own config stubbed."""

    def __init__(self, saved=None):
        self._saved = saved
        self._tmp = tempfile.TemporaryDirectory()

    def __enter__(self):
        root = Path(self._tmp.name)
        self.user_dir = root / "claude-home"
        self.user_dir.mkdir()
        self.project = root / "project"
        self.project.mkdir()

        self._old_env = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.user_dir)

        # The reader's own config.json is stubbed so tests never touch the
        # real one, and never write to it.
        self._writes = []
        self._old_load = claude_reader._load_config
        self._old_save = claude_reader._save_config
        cfg = {"permission_mode": self._saved} if self._saved else {}
        claude_reader._load_config = lambda: dict(cfg)
        claude_reader._save_config = lambda c: self._writes.append(dict(c))
        return self

    def __exit__(self, *exc):
        claude_reader._load_config = self._old_load
        claude_reader._save_config = self._old_save
        if self._old_env is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self._old_env
        self._tmp.cleanup()
        return False

    @property
    def writes(self):
        return self._writes


def test_user_settings_supply_the_mode():
    with _Sandbox() as box:
        _write_settings(box.user_dir / "settings.json", "acceptEdits")
        assert _claude_config_permission_mode(str(box.project)) == "acceptEdits"
        assert _default_permission_mode(str(box.project)) == "acceptEdits"


def test_project_settings_beat_user_settings():
    with _Sandbox() as box:
        _write_settings(box.user_dir / "settings.json", "acceptEdits")
        _write_settings(box.project / ".claude" / "settings.json", "plan")
        assert _default_permission_mode(str(box.project)) == "plan"


def test_project_local_settings_win():
    with _Sandbox() as box:
        _write_settings(box.user_dir / "settings.json", "acceptEdits")
        _write_settings(box.project / ".claude" / "settings.json", "plan")
        _write_settings(
            box.project / ".claude" / "settings.local.json", "bypassPermissions"
        )
        assert _default_permission_mode(str(box.project)) == "bypassPermissions"


def test_no_settings_anywhere_falls_back_to_default():
    with _Sandbox() as box:
        assert _claude_config_permission_mode(str(box.project)) == ""
        assert _default_permission_mode(str(box.project)) == "default"


def test_settings_without_a_permissions_block_are_ignored():
    with _Sandbox() as box:
        _write_settings(box.user_dir / "settings.json", None)
        assert _default_permission_mode(str(box.project)) == "default"


def test_unparsable_settings_do_not_raise():
    with _Sandbox() as box:
        (box.user_dir / "settings.json").write_text("{not json", encoding="utf-8")
        assert _default_permission_mode(str(box.project)) == "default"


def test_a_mode_claude_code_does_not_offer_is_ignored():
    with _Sandbox() as box:
        _write_settings(box.user_dir / "settings.json", "somethingElse")
        assert _default_permission_mode(str(box.project)) == "default"


def test_a_byte_order_mark_does_not_break_parsing():
    with _Sandbox() as box:
        (box.user_dir / "settings.json").write_text(
            json.dumps({"permissions": {"defaultMode": "plan"}}), encoding="utf-8-sig"
        )
        assert _default_permission_mode(str(box.project)) == "plan"


def test_your_saved_choice_beats_the_claude_code_config():
    with _Sandbox(saved="plan") as box:
        _write_settings(box.user_dir / "settings.json", "acceptEdits")
        assert _default_permission_mode(str(box.project)) == "plan"


def test_a_stale_saved_choice_is_ignored():
    with _Sandbox(saved="noSuchMode") as box:
        _write_settings(box.user_dir / "settings.json", "acceptEdits")
        assert _default_permission_mode(str(box.project)) == "acceptEdits"


def test_changing_the_mode_is_written_to_config():
    with _Sandbox() as box:
        _remember_permission_mode("bypassPermissions")
        assert box.writes == [{"permission_mode": "bypassPermissions"}]


def test_an_unchanged_mode_is_not_rewritten():
    with _Sandbox(saved="plan") as box:
        _remember_permission_mode("plan")
        assert box.writes == []


def test_a_bogus_mode_is_never_saved():
    with _Sandbox() as box:
        _remember_permission_mode("noSuchMode")
        assert box.writes == []


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
