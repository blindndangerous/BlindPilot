"""Backend-neutral behavior and provider adapter regression tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

import agent_backends
from agent_backends import (
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    BACKEND_FREEBUFF,
    CodexWorker,
    FreebuffWorker,
    backend_label,
    codex_model_options,
    freebuff_model_options,
    normalize_backend,
    set_freebuff_model,
    worker_class,
)


def _callbacks() -> dict:
    return {
        "on_session": lambda _value: None,
        "on_started": lambda: None,
        "on_activity": lambda _kind, _value: None,
        "on_complete": lambda _value: None,
        "on_failed": lambda _value: None,
        "on_done": lambda: None,
    }


def test_backend_names_are_normalized_and_claude_is_the_fallback():
    assert normalize_backend(None) == BACKEND_CLAUDE
    assert normalize_backend("CODEX") == BACKEND_CODEX
    assert normalize_backend("Free Buff") == BACKEND_FREEBUFF
    assert normalize_backend("unknown") == BACKEND_CLAUDE
    assert backend_label(BACKEND_FREEBUFF) == "FreeBuff"


def test_worker_class_selects_each_adapter():
    class Claude:
        pass

    assert worker_class(BACKEND_CLAUDE, Claude) is Claude
    assert worker_class(BACKEND_CODEX, Claude) is CodexWorker
    assert worker_class(BACKEND_FREEBUFF, Claude) is FreebuffWorker


def test_codex_catalog_includes_all_reported_reasoning_levels(monkeypatch, tmp_path):
    payload = {
        "models": [
            {
                "slug": "gpt-test",
                "supported_reasoning_levels": [
                    {"effort": "low"},
                    {"effort": "max"},
                    {"effort": "ultra"},
                ],
            }
        ]
    }
    monkeypatch.setattr(agent_backends, "find_backend_cli", lambda _backend: "codex")
    monkeypatch.setattr(
        agent_backends.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=json.dumps(payload)),
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))

    models, efforts, current_model, current_effort, error = codex_model_options()

    assert models == ["gpt-test"]
    assert efforts == ["low", "max", "ultra"]
    assert current_model == ""
    assert current_effort == ""
    assert error == ""


def test_codex_permission_modes_translate_to_native_sandboxes():
    assert CodexWorker._policy("plan") == (
        "never",
        {"type": "readOnly", "networkAccess": False},
    )


def test_codex_app_server_prefers_packaged_windows_binary(monkeypatch, tmp_path):
    wrapper = tmp_path / "npm" / "codex.cmd"
    native = (
        tmp_path
        / "npm"
        / "node_modules"
        / "@openai"
        / "codex"
        / "node_modules"
        / "@openai"
        / "codex-win32-x64"
        / "vendor"
        / "target"
        / "bin"
        / "codex.exe"
    )
    native.parent.mkdir(parents=True)
    native.touch()
    monkeypatch.setattr(agent_backends.platform, "system", lambda: "Windows")

    assert agent_backends._codex_app_server_binary(str(wrapper)) == str(native)
    assert CodexWorker._policy("acceptEdits")[0] == "on-request"
    assert CodexWorker._policy("bypassPermissions") == (
        "never",
        {"type": "dangerFullAccess"},
    )


def test_codex_stream_deltas_become_one_accessible_activity_row():
    events = []
    callbacks = _callbacks()
    callbacks["on_activity"] = lambda kind, value: events.append((kind, value))
    worker = CodexWorker("test", None, ".", "default", **callbacks)
    worker._assistant_delta_seen.add("message-1")
    worker._assistant_streams["message-1"] = ["Blind", "Pilot", " ready."]
    worker._assistant_parts.extend(["Blind", "Pilot", " ready."])

    worker._item_completed({"id": "message-1", "type": "agentMessage", "text": "BlindPilot ready."})

    assert events == [("assistant", "BlindPilot ready.")]
    assert "".join(worker._assistant_parts) == "BlindPilot ready."


def test_freebuff_catalog_is_discovered_at_runtime_and_pro_is_default(monkeypatch, tmp_path):
    wrapper = tmp_path / "npm" / "freebuff.cmd"
    readme = wrapper.parent / "node_modules" / "freebuff" / "README.md"
    executable = tmp_path / ".config" / "manicode" / "freebuff.exe"
    readme.parent.mkdir(parents=True)
    executable.parent.mkdir(parents=True)
    wrapper.touch()
    readme.write_text("DeepSeek V4 Pro\nGPT Next\n", encoding="utf-8")
    executable.write_text(
        'pro="deepseek/deepseek-v4-pro",next="openai/gpt-next";'
        'a={id:pro,displayName:"DeepSeek V4 Pro",availability:"always"};'
        'b={id:next,displayName:"GPT Next",availability:"always"};',
        encoding="latin-1",
    )
    monkeypatch.setattr(agent_backends.Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(agent_backends.platform, "system", lambda: "Windows")
    monkeypatch.setattr(agent_backends, "find_backend_cli", lambda _backend: str(wrapper))
    agent_backends.invalidate_backend_cache()

    models, efforts, current, current_effort, error = freebuff_model_options()

    assert models == ["deepseek/deepseek-v4-pro", "openai/gpt-next"]
    assert efforts == []
    assert current == "deepseek/deepseek-v4-pro"
    assert current_effort == ""
    assert error == ""

    set_freebuff_model("openai/gpt-next")
    models, _efforts, current, _current_effort, _error = freebuff_model_options()
    assert current == "openai/gpt-next"
    settings = json.loads(
        (tmp_path / ".config" / "manicode" / "settings.json").read_text(encoding="utf-8")
    )
    assert settings["freebuffModel"] == "openai/gpt-next"


def test_freebuff_screen_parser_returns_reasoning_and_clean_answer():
    worker = FreebuffWorker(
        "Reply with exactly: BlindPilot FreeBuff adapter ready.",
        None,
        ".",
        "default",
        **_callbacks(),
    )
    screen = """
Reply with exactly: BlindPilot FreeBuff adapter ready.
• Thinking
  The user requested an exact reply.
BlindPilot FreeBuff adapter ready.
⎘ • 6s • △▽
DeepSeek V3 unlimited ✕ End session
│ Store memory and run governed workloads. Ad │
│ Learn More mongodb.com │
│ Start Monetizing  trygravity.ai │
│ Get API Access  baseten.co │
│ deduplication cuts backup storage costs by 50%. │
│ ▸ basher ● running │
│ command output that is not part of the answer │
│ ▸ basher completed ✓ │
Enter a coding task or / for commands
"""

    thinking, answer = worker._freebuff_sections(screen)

    assert thinking == "The user requested an exact reply."
    assert answer == "BlindPilot FreeBuff adapter ready."


def test_freebuff_live_delta_ignores_terminal_redraw_replacements():
    events = []
    callbacks = _callbacks()
    callbacks["on_activity"] = lambda kind, value: events.append((kind, value))
    worker = FreebuffWorker(
        "test",
        None,
        ".",
        "default",
        **callbacks,
    )

    assert worker._emit_stable_delta("assistant", "Old answer", "New answer") == "New answer"
    assert events == []
    assert (
        worker._emit_stable_delta("assistant", "New answer", "New answer continued")
        == "New answer continued"
    )
    assert events == [("assistant", "continued")]


def test_freebuff_chat_discovery_searches_all_project_buckets(monkeypatch, tmp_path):
    project_root = tmp_path / ".config" / "manicode" / "projects"
    chat = project_root / "different-git-root" / "chats" / "session-id"
    chat.mkdir(parents=True)
    monkeypatch.setattr(agent_backends.Path, "home", classmethod(lambda cls: tmp_path))

    found = agent_backends._freebuff_chat_dirs(str(tmp_path / "workspace"))

    assert "session-id" in found
    assert found["session-id"] == chat.stat().st_mtime
    assert agent_backends._freebuff_chat_path(str(tmp_path / "workspace"), "session-id") == chat


def test_freebuff_structured_chat_reports_progress_and_authoritative_completion(
    monkeypatch, tmp_path
):
    chat = tmp_path / ".config" / "manicode" / "projects" / "project" / "chats" / "session-id"
    chat.mkdir(parents=True)
    monkeypatch.setattr(agent_backends.Path, "home", classmethod(lambda cls: tmp_path))
    messages = [
        {"variant": "user", "content": "Do the work"},
        {
            "variant": "ai",
            "blocks": [
                {"type": "text", "textType": "reasoning", "content": "Inspecting config"},
                {
                    "type": "agent",
                    "agentId": "tool-1",
                    "agentName": "basher",
                    "status": "complete",
                },
                {"type": "text", "textType": "text", "content": "Configuration updated."},
            ],
        },
    ]
    (chat / "chat-messages.json").write_text(json.dumps(messages), encoding="utf-8")
    log = chat / "log.jsonl"
    log.write_text('{"msg":"old Main prompt finished"}\n', encoding="utf-8")
    offset = log.stat().st_size
    with log.open("a", encoding="utf-8") as handle:
        handle.write('{"msg":"Main prompt finished"}\n')

    thinking, answer, agents = agent_backends._freebuff_chat_snapshot(chat)

    assert thinking == "Inspecting config"
    assert answer == "Configuration updated."
    assert agents == [("tool-1", "basher", "complete")]
    assert agent_backends._freebuff_run_status(chat, offset) == "complete"
    assert agent_backends._freebuff_run_status(chat, log.stat().st_size) == ""


def test_freebuff_structured_chat_reports_interruption(tmp_path):
    chat = tmp_path / "chat"
    chat.mkdir()
    (chat / "log.jsonl").write_text(
        '{"msg":"Agent run cancelled by user (abort error)"}\n', encoding="utf-8"
    )

    assert agent_backends._freebuff_run_status(chat) == "cancelled"
