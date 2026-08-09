"""Backend-neutral behavior and provider adapter regression tests."""

from __future__ import annotations

import json
import platform
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
    # BlindPilot's own config lives under APPDATA on Windows, and selecting a
    # model writes there, so it has to be redirected as well as the home path.
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
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


def test_freebuff_picker_navigation_uses_runtime_model_order():
    visible = """
│   DeepSeek V4 Pro          Deep reasoning │
│   GPT-5.6 Luna             Thinks hard    │
│   MiniMax M3               Fastest        │
│ › DeepSeek V4 Flash 07/31  Recommended    │
"""
    models = [
        "deepseek/deepseek-v4-pro",
        "openai/gpt-5.6-luna",
        "minimax/minimax-m3",
        "deepseek/deepseek-v4-flash",
    ]

    options, focused = agent_backends._freebuff_picker_options(visible, models)

    assert options == models
    assert focused == 3


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
            "id": "ai-1",
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

    answer_id, thinking, answer, agents = agent_backends._freebuff_chat_snapshot(chat)

    assert answer_id == "ai-1"
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


def test_freebuff_resumed_chat_separates_the_new_answer_from_the_previous_one(tmp_path):
    """A resumed turn must not replay the answer it was resumed from.

    FreeBuff rewrites the whole chat file on every save, so the text alone
    cannot say whether an answer is new.  The message id can, and it is what
    the worker uses to decide when this turn's answer has actually started.
    """
    chat = tmp_path / "chat"
    chat.mkdir()
    messages = [
        {"id": "divider-1", "variant": "ai", "blocks": [{"type": "mode-divider"}]},
        {"id": "user-1", "variant": "user", "content": "First"},
        {
            "id": "ai-first",
            "variant": "ai",
            "blocks": [{"type": "text", "textType": "text", "content": "apple"}],
        },
    ]
    (chat / "chat-messages.json").write_text(json.dumps(messages), encoding="utf-8")

    # What the worker records before it sends the resumed prompt.
    assert agent_backends._freebuff_answer_id(chat) == "ai-first"

    # FreeBuff opens the next turn with a divider before writing any reply, and
    # the divider must not be mistaken for the turn's answer.
    messages.append({"id": "divider-2", "variant": "ai", "blocks": [{"type": "mode-divider"}]})
    messages.append({"id": "user-2", "variant": "user", "content": "Second"})
    (chat / "chat-messages.json").write_text(json.dumps(messages), encoding="utf-8")
    assert agent_backends._freebuff_chat_snapshot(chat)[0] == "ai-first"

    messages.append(
        {
            "id": "ai-second",
            "variant": "ai",
            "blocks": [{"type": "text", "textType": "text", "content": "banana"}],
        }
    )
    (chat / "chat-messages.json").write_text(json.dumps(messages), encoding="utf-8")
    answer_id, _thinking, answer, _agents = agent_backends._freebuff_chat_snapshot(chat)

    assert answer_id == "ai-second"
    assert answer == "banana"


def test_freebuff_snapshot_drops_the_interruption_marker_from_kept_text(tmp_path):
    """Closing the hidden terminal stamps the marker onto the text it produced."""
    chat = tmp_path / "chat"
    chat.mkdir()
    messages = [
        {
            "id": "ai-1",
            "variant": "ai",
            "blocks": [
                {
                    "type": "text",
                    "textType": "text",
                    "content": "apple\n\n[response interrupted]",
                }
            ],
        }
    ]
    (chat / "chat-messages.json").write_text(json.dumps(messages), encoding="utf-8")

    assert agent_backends._freebuff_chat_snapshot(chat)[2] == "apple"


def test_backend_processes_are_launched_without_a_console_window():
    """A windowed app has no console to inherit, so children must not get one.

    Without this flag Windows gives every console child a brand new terminal:
    it appears on screen, takes focus from the screen reader, and for the agent
    CLI it stays there for the whole turn.  Elsewhere there is no such flag and
    the keyword has to be absent, not zero, or ``subprocess`` rejects it.
    """
    if platform.system() == "Windows":
        assert agent_backends.CREATE_NO_WINDOW == 0x08000000
        assert agent_backends.no_window_kwargs() == {"creationflags": 0x08000000}
    else:
        assert agent_backends.CREATE_NO_WINDOW == 0
        assert agent_backends.no_window_kwargs() == {}


def test_codex_app_server_is_spawned_with_the_no_window_flag(monkeypatch):
    captured: dict = {}

    def fake_popen(_args, **kwargs):
        captured.update(kwargs)
        raise OSError("stop here; the launch arguments are what matter")

    monkeypatch.setattr(agent_backends, "find_backend_cli", lambda _backend: "codex")
    monkeypatch.setattr(agent_backends.subprocess, "Popen", fake_popen)
    failures: list[str] = []
    callbacks = _callbacks()
    callbacks["on_failed"] = failures.append
    worker = CodexWorker("test", None, ".", "default", **callbacks)

    worker._do_run()

    assert failures and failures[0].startswith("Failed to launch Codex")
    assert captured.get("creationflags", 0) == agent_backends.CREATE_NO_WINDOW


def test_freebuff_keeps_pro_when_freebuff_resets_its_own_setting(monkeypatch, tmp_path):
    """FreeBuff rewrites its settings to the model it recommends after a turn.

    Reading that back as the user's choice downgraded every following turn to
    the recommendation, so BlindPilot keeps its own record and prefers it.
    """
    wrapper = tmp_path / "npm" / "freebuff.cmd"
    readme = wrapper.parent / "node_modules" / "freebuff" / "README.md"
    executable = tmp_path / ".config" / "manicode" / "freebuff.exe"
    settings = executable.parent / "settings.json"
    readme.parent.mkdir(parents=True)
    executable.parent.mkdir(parents=True)
    wrapper.touch()
    readme.write_text("DeepSeek V4 Pro\nDeepSeek V4 Flash\n", encoding="utf-8")
    executable.write_text(
        'pro="deepseek/deepseek-v4-pro",flash="deepseek/deepseek-v4-flash";'
        'a={id:pro,displayName:"DeepSeek V4 Pro",availability:"always"};'
        'b={id:flash,displayName:"DeepSeek V4 Flash",availability:"always"};',
        encoding="latin-1",
    )
    monkeypatch.setattr(agent_backends.Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(agent_backends.platform, "system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    monkeypatch.setattr(agent_backends, "find_backend_cli", lambda _backend: str(wrapper))
    agent_backends.invalidate_backend_cache()

    # FreeBuff has left its own recommendation behind and BlindPilot has no
    # record yet, which is the state after a first run.
    settings.write_text(json.dumps({"freebuffModel": "deepseek/deepseek-v4-flash"}), "utf-8")
    _models, _efforts, current, _effort, _error = freebuff_model_options()
    assert current == "deepseek/deepseek-v4-pro"

    # An explicit choice is recorded by BlindPilot and survives the same reset.
    set_freebuff_model("deepseek/deepseek-v4-flash")
    settings.write_text(json.dumps({"freebuffModel": "deepseek/deepseek-v4-pro"}), "utf-8")
    _models, _efforts, current, _effort, _error = freebuff_model_options()
    assert current == "deepseek/deepseek-v4-flash"


def test_freebuff_reports_a_terminal_that_closes_before_it_is_ready(monkeypatch):
    """A pseudo-terminal that cannot host a process must not read as silence.

    The packaged build shipped without pywinpty's console host, so nothing ever
    started and the worker sat waiting for output that could never arrive.
    """
    failures: list[str] = []
    callbacks = _callbacks()
    callbacks["on_failed"] = failures.append
    worker = FreebuffWorker("do the work", None, ".", "default", **callbacks)

    def fake_spawn(_args):
        worker._stream_ended.set()
        return lambda _timeout: ""

    monkeypatch.setattr(agent_backends, "find_backend_cli", lambda _backend: "freebuff")
    monkeypatch.setattr(
        agent_backends,
        "freebuff_model_options",
        lambda: (["deepseek/deepseek-v4-pro"], [], "deepseek/deepseek-v4-pro", "", ""),
    )
    monkeypatch.setattr(agent_backends, "set_freebuff_model", lambda _model: None)
    monkeypatch.setattr(FreebuffWorker, "_spawn_pty", staticmethod(fake_spawn))

    worker._do_run()

    assert failures and "closed before it was ready" in failures[0]


def test_freebuff_narrates_only_finished_sentences():
    """Half a sentence read aloud is what makes a streamed run sound broken."""
    assert agent_backends._complete_sentences("Maple Ridge is a city. Known for its") == (
        "Maple Ridge is a city."
    )
    assert agent_backends._complete_sentences("The user is asking about") == ""
    assert agent_backends._complete_sentences("First line\nsecond half") == "First line"
    assert agent_backends._complete_sentences('He said "go." Then') == 'He said "go."'
