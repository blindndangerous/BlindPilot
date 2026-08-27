"""Backend-neutral behavior and provider adapter regression tests."""

from __future__ import annotations

import io
import json
import os
import platform
import threading
import time
from types import SimpleNamespace

import agent_backends
from agent_backends import (
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    BACKEND_FREEBUFF,
    BACKEND_OPENCODE,
    CodexWorker,
    FreebuffWorker,
    OpencodeWorker,
    backend_auth_ok,
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


def test_freebuff_auth_requires_a_complete_parseable_credential(monkeypatch, tmp_path):
    credential = tmp_path / ".config" / "manicode" / "credentials.json"
    credential.parent.mkdir(parents=True)
    monkeypatch.setattr(agent_backends.Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(agent_backends, "find_backend_cli", lambda _backend: "freebuff")

    credential.write_text("not json", encoding="utf-8")
    assert not backend_auth_ok(BACKEND_FREEBUFF)
    credential.write_text(json.dumps({"default": {"authToken": "token"}}), encoding="utf-8")
    assert not backend_auth_ok(BACKEND_FREEBUFF)
    credential.write_text(
        json.dumps(
            {
                "default": {
                    "authToken": "token",
                    "fingerprintId": "id",
                    "fingerprintHash": "hash",
                }
            }
        ),
        encoding="utf-8",
    )
    assert backend_auth_ok(BACKEND_FREEBUFF)


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


def test_freebuff_catalog_keeps_the_current_off_peak_pro_model(monkeypatch, tmp_path):
    """Freebuff 0.0.152 changed Pro from always to off_peak_only."""
    wrapper = tmp_path / "npm" / "freebuff.cmd"
    readme = wrapper.parent / "node_modules" / "freebuff" / "README.md"
    executable = tmp_path / ".config" / "manicode" / "freebuff.exe"
    readme.parent.mkdir(parents=True)
    executable.parent.mkdir(parents=True)
    wrapper.touch()
    readme.write_text("DeepSeek V4 Pro\nDeepSeek V4 Flash\n", encoding="utf-8")
    executable.write_text(
        'pro="deepseek/deepseek-v4-pro",flash="deepseek/deepseek-v4-flash";'
        'a={id:pro,displayName:"DeepSeek V4 Pro",availability:"off_peak_only"};'
        'b={id:flash,displayName:"DeepSeek V4 Flash",availability:"always"};',
        encoding="latin-1",
    )
    monkeypatch.setattr(agent_backends.Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(agent_backends.platform, "system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    monkeypatch.setattr(agent_backends, "find_backend_cli", lambda _backend: str(wrapper))
    agent_backends.invalidate_backend_cache()

    models, _efforts, current, _current_effort, error = freebuff_model_options()

    assert models == ["deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-flash"]
    assert current == "deepseek/deepseek-v4-pro"
    assert error == ""


def test_freebuff_catalog_keeps_a_model_the_readme_names_without_its_date(monkeypatch, tmp_path):
    """A release date on the display name must not hide the model.

    FreeBuff renamed "DeepSeek V4 Pro" to "DeepSeek V4 Pro 08/13" in the binary
    and left the README undated. Reading the two as different models dropped Pro
    out of the catalog, which quietly handed every conversation to Flash.
    """
    wrapper = tmp_path / "npm" / "freebuff.cmd"
    readme = wrapper.parent / "node_modules" / "freebuff" / "README.md"
    executable = tmp_path / ".config" / "manicode" / "freebuff.exe"
    readme.parent.mkdir(parents=True)
    executable.parent.mkdir(parents=True)
    wrapper.touch()
    readme.write_text("DeepSeek V4 Flash 07/31, DeepSeek V4 Pro, and GPT Next.\n", encoding="utf-8")
    executable.write_text(
        'pro="deepseek/deepseek-v4-pro",flash="deepseek/deepseek-v4-flash",'
        'next="openai/gpt-next";'
        'a={id:flash,displayName:"DeepSeek V4 Flash 07/31",availability:"always"};'
        'b={id:pro,displayName:"DeepSeek V4 Pro 08/13",availability:"always"};'
        'c={id:next,displayName:"GPT Next",availability:"always"};',
        encoding="latin-1",
    )
    monkeypatch.setattr(agent_backends.Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(agent_backends.platform, "system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    monkeypatch.setattr(agent_backends, "find_backend_cli", lambda _backend: str(wrapper))
    agent_backends.invalidate_backend_cache()

    models, _efforts, current, _current_effort, error = freebuff_model_options()

    assert models[0] == "deepseek/deepseek-v4-pro"
    assert "deepseek/deepseek-v4-flash" in models
    assert current == "deepseek/deepseek-v4-pro"
    assert error == ""


def test_freebuff_falls_back_to_pro_rather_than_freebuffs_own_flash_setting(monkeypatch, tmp_path):
    """With no BlindPilot record, Pro wins over whatever FreeBuff left behind."""
    wrapper = tmp_path / "npm" / "freebuff.cmd"
    readme = wrapper.parent / "node_modules" / "freebuff" / "README.md"
    executable = tmp_path / ".config" / "manicode" / "freebuff.exe"
    readme.parent.mkdir(parents=True)
    executable.parent.mkdir(parents=True)
    wrapper.touch()
    readme.write_text("DeepSeek V4 Flash 07/31 and DeepSeek V4 Pro.\n", encoding="utf-8")
    executable.write_text(
        'pro="deepseek/deepseek-v4-pro",flash="deepseek/deepseek-v4-flash";'
        'a={id:flash,displayName:"DeepSeek V4 Flash 07/31",availability:"always"};'
        'b={id:pro,displayName:"DeepSeek V4 Pro 08/13",availability:"always"};',
        encoding="latin-1",
    )
    settings = executable.parent / "settings.json"
    settings.write_text(json.dumps({"freebuffModel": "deepseek/deepseek-v4-flash"}), "utf-8")
    monkeypatch.setattr(agent_backends.Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(agent_backends.platform, "system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    monkeypatch.setattr(agent_backends, "find_backend_cli", lambda _backend: str(wrapper))
    agent_backends.invalidate_backend_cache()

    _models, _efforts, current, _current_effort, _error = freebuff_model_options()

    assert current == "deepseek/deepseek-v4-pro"


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


def test_freebuff_reads_the_answer_as_the_terminal_paints_it():
    """The chat file is only saved once the reply is finished.

    Everything read out while a turn is running therefore comes off the screen,
    one finished sentence at a time, and each frame must add to the reading
    rather than repeat it.
    """
    events = []
    callbacks = _callbacks()
    callbacks["on_activity"] = lambda kind, value: events.append((kind, value))
    worker = FreebuffWorker("test", None, ".", "default", **callbacks)

    spoken = worker._emit_screen_delta("assistant", "", "The sky is blue. Sunlight is")
    assert events == [("assistant", "The sky is blue.")]
    assert spoken == "The sky is blue."

    # The rest of that sentence is still half-written, so nothing new is read.
    spoken = worker._emit_screen_delta("assistant", spoken, "The sky is blue. Sunlight is made")
    assert len(events) == 1

    spoken = worker._emit_screen_delta(
        "assistant", spoken, "The sky is blue. Sunlight is made of many colors. And"
    )
    assert events[-1] == ("assistant", "Sunlight is made of many colors.")

    # The terminal repaints from nothing between frames; that is not an answer.
    assert worker._emit_screen_delta("assistant", spoken, "The sky is") == spoken
    assert len(events) == 2

    # The turn ends holding a sentence that never finished. It is still the
    # answer, so it is released whole.
    worker._emit_screen_delta(
        "assistant",
        spoken,
        "The sky is blue. Sunlight is made of many colors. And blue scatters most",
        whole=True,
    )
    assert events[-1] == ("assistant", "And blue scatters most")


def test_freebuff_rejoins_lines_the_terminal_wrapped():
    """A wrapped line is not the end of a sentence, and must not read as one."""
    wrapped = (
        "Sunlight is made of many colors, and blue scatters more than the\n"
        "others do.\n"
        "That is the whole of it."
    )

    assert agent_backends._unwrap_screen_text(wrapped) == (
        "Sunlight is made of many colors, and blue scatters more than the others do.\n"
        "That is the whole of it."
    )


def test_freebuff_keeps_reading_where_it_left_off_after_the_screen_scrolls():
    """Once the answer is taller than the terminal, the top scrolls away."""
    assert agent_backends._append_delta("one two three", "one two three four") == "four"
    assert agent_backends._append_delta("one two three", "two three four") == "four"
    assert agent_backends._append_delta("one two three", "one two three") == ""
    assert agent_backends._append_delta("", "one two") == "one two"
    # The terminal revises where it broke a line as the text below it grows.
    # The same words rewrapped are not new words.
    assert agent_backends._append_delta("Tips: 1.", "Tips:\n1.") == ""
    assert agent_backends._append_delta("Tips: 1.", "Tips:\n1. Be clear.") == "Be clear."


def test_freebuff_finishes_reading_an_answer_that_scrolled_out_of_view():
    """The saved chat is the whole answer; the screen only ever held part.

    What was read came off the terminal, without the Markdown the answer was
    written in, so the two are compared by their letters and digits alone.
    """
    answer = "**Rain** falls. It waters the fields. Then it stops."

    assert agent_backends._unspoken_tail("Rain falls. It waters the fields.", answer) == (
        "Then it stops."
    )
    assert agent_backends._unspoken_tail(answer, answer) == ""
    assert agent_backends._unspoken_tail("", answer) == answer


def test_freebuff_prewarmed_terminal_is_only_taken_for_the_message_it_fits():
    """A terminal started ahead of time is bound to one conversation.

    FreeBuff is told which chat to resume and which model to use when it starts,
    so a waiting terminal cannot serve a different one. It also keeps the record
    of which chats existed before it started, because it creates the chat for a
    new conversation as it starts, and that record is what identifies it.
    """
    killed: list[str] = []
    holding = {
        "key": (os.path.abspath("work"), "chat-1", "deepseek/deepseek-v4-pro"),
        "pty": "terminal",
        "read": lambda _timeout: "",
        "ended": threading.Event(),
        "before": {"chat-0": 1.0},
        "expires": time.monotonic() + 60,
    }
    agent_backends._freebuff_prewarm = holding
    try:
        assert (
            agent_backends._take_freebuff_prewarm("work", "chat-2", "deepseek/deepseek-v4-pro")
            is None
        )
        assert agent_backends._freebuff_prewarm is None
        assert holding["ended"].is_set()

        holding["ended"] = threading.Event()
        agent_backends._freebuff_prewarm = holding
        taken = agent_backends._take_freebuff_prewarm("work", "chat-1", "deepseek/deepseek-v4-pro")
        assert taken is not None
        assert taken["pty"] == "terminal"
        assert taken["before"] == {"chat-0": 1.0}
        assert not taken["ended"].is_set()
        assert agent_backends._freebuff_prewarm is None
    finally:
        agent_backends._freebuff_prewarm = None
    assert killed == []


def test_freebuff_prewarmed_terminal_is_dropped_once_it_is_too_old():
    holding = {
        "key": (os.path.abspath("work"), "", "deepseek/deepseek-v4-pro"),
        "pty": "terminal",
        "read": lambda _timeout: "",
        "ended": threading.Event(),
        "before": {},
        "expires": time.monotonic() - 1,
    }
    agent_backends._freebuff_prewarm = holding
    try:
        assert (
            agent_backends._take_freebuff_prewarm("work", None, "deepseek/deepseek-v4-pro") is None
        )
        assert holding["ended"].is_set()
    finally:
        agent_backends._freebuff_prewarm = None


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


# ----- Compaction -----


class _ScriptedCodexServer:
    """A stand-in app server that answers whatever the worker actually asks.

    Its stdout is a generator, so each line is produced only when the worker
    reads it — by which point the request that line replies to has already been
    written. That is what lets the script assert on real ordering rather than
    on a fixed transcript.
    """

    def __init__(self, thread_id: str = "thread-1") -> None:
        self.sent: list[dict] = []
        self.stdin = self
        self.stderr = None
        self.stdout = self._script()

    def write(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def flush(self) -> None:
        pass

    def poll(self):
        return None

    def kill(self) -> None:
        pass

    def methods(self) -> list[str]:
        return [message.get("method", "") for message in self.sent]

    def _last(self, method: str) -> dict:
        for message in reversed(self.sent):
            if message.get("method") == method:
                return message
        raise AssertionError(f"the worker never sent {method}: {self.methods()}")

    def _script(self):
        resume = self._last("thread/resume")
        yield json.dumps({"id": resume["id"], "result": {"thread": {"id": "thread-1"}}})
        compact = self._last("thread/compact/start")
        self.compact_params = compact.get("params")
        yield json.dumps({"id": compact["id"], "result": {}})
        yield json.dumps({"method": "turn/started", "params": {"turn": {"id": "turn-1"}}})
        item = {"type": "contextCompaction", "id": "compaction-1"}
        yield json.dumps({"method": "item/started", "params": {"item": item}})
        yield json.dumps({"method": "item/completed", "params": {"item": item}})
        yield json.dumps(
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-1", "status": "completed"}},
            }
        )


def test_codex_compaction_is_a_request_of_its_own_not_a_message(monkeypatch):
    """Codex has no /compact message, so sending one as text would do nothing."""
    server = _ScriptedCodexServer()
    monkeypatch.setattr(agent_backends, "find_backend_cli", lambda _backend: "codex")
    monkeypatch.setattr(agent_backends.subprocess, "Popen", lambda *_a, **_k: server)
    completed: list[str] = []
    failures: list[str] = []
    callbacks = _callbacks()
    callbacks["on_complete"] = completed.append
    callbacks["on_failed"] = failures.append
    worker = CodexWorker("/compact", "thread-1", ".", "default", compact=True, **callbacks)

    worker._do_run()

    assert not failures
    assert "thread/compact/start" in server.methods()
    assert server.compact_params == {"threadId": "thread-1"}
    # The prompt text is never sent: a compaction turn is not a message.
    assert "turn/start" not in server.methods()
    # A compaction turn produces no answer, so it has to say so itself.
    assert completed == ["Conversation compacted."]


def test_codex_will_not_compact_a_conversation_that_has_not_started(monkeypatch):
    monkeypatch.setattr(agent_backends, "find_backend_cli", lambda _backend: "codex")
    failures: list[str] = []
    callbacks = _callbacks()
    callbacks["on_failed"] = failures.append
    worker = CodexWorker("/compact", None, ".", "default", compact=True, **callbacks)

    worker._do_run()

    assert failures == ["There is no Codex conversation to compact yet"]


def test_only_the_backends_with_a_compaction_command_offer_one():
    from agent_backends import BACKENDS, compaction_request

    assert BACKENDS[BACKEND_CLAUDE].supports_compaction is True
    assert BACKENDS[BACKEND_CODEX].supports_compaction is True
    # FreeBuff's CLI has no compaction command at all.
    assert BACKENDS[BACKEND_FREEBUFF].supports_compaction is False

    # Claude Code acts on "/compact" as an ordinary message; Codex needs its
    # worker told, because the text alone would mean nothing to it.
    assert compaction_request(BACKEND_CLAUDE) == ("/compact", {})
    assert compaction_request(BACKEND_CODEX) == ("/compact", {"compact": True})
    assert compaction_request(BACKEND_FREEBUFF) is None


class _ScriptedOpencodeServer:
    """An opencode server that answers from a script instead of over a socket.

    It records every request the worker makes, and plays back a canned event
    stream — which is all the worker actually reads, so a whole turn can be
    replayed without a provider, a network, or a subprocess.
    """

    def __init__(self, events: list[dict], session_id: str = "ses_test") -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.session_id = session_id
        self.events = events
        self.closed = False
        self.fail: set[str] = set()

    def paths(self) -> list[str]:
        return [path for _method, path, _body in self.calls]

    def body(self, needle: str) -> object:
        for _method, path, body in self.calls:
            if needle in path:
                return body
        return None

    def request(self, method, path, params=None, body=None, timeout=None):
        self.calls.append((method, path, body))
        if any(marker in path for marker in self.fail):
            raise OSError(f"no route for {path}")
        if method == "POST" and path == "/session":
            return {"id": self.session_id}
        return {}

    def open(self, method, path, params=None, body=None, timeout=None):
        self.calls.append((method, path, body))
        server = self

        class Stream:
            def __iter__(self):
                for event in server.events:
                    yield ("data: " + json.dumps(event)).encode("utf-8")

            def close(self):
                server.closed = True

        return Stream()


def _wait_for(condition, timeout: float = 5.0) -> None:
    """Give a worker's own thread a moment to get where the test is looking."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError("the worker never got there")


def _opencode_event(kind: str, session_id: str = "ses_test", **properties) -> dict:
    return {"type": kind, "properties": {"sessionID": session_id, **properties}}


def _opencode_turn(events: list[dict], monkeypatch, **kwargs) -> tuple[dict, object]:
    """Run one OpencodeWorker turn against a scripted server."""
    server = _ScriptedOpencodeServer(events)
    monkeypatch.setattr(agent_backends, "opencode_server", lambda: server)
    monkeypatch.setattr(agent_backends, "opencode_default_model", lambda *_a, **_k: "")
    monkeypatch.setattr(agent_backends, "opencode_model_efforts", lambda *_a, **_k: [])
    known = kwargs.pop("commands", [])
    monkeypatch.setattr(agent_backends, "opencode_commands", lambda *_a, **_k: known)
    seen: dict = {"activity": [], "complete": [], "failed": [], "session": []}
    callbacks = _callbacks()
    callbacks["on_activity"] = lambda kind, value: seen["activity"].append((kind, value))
    callbacks["on_complete"] = seen["complete"].append
    callbacks["on_failed"] = seen["failed"].append
    callbacks["on_session"] = seen["session"].append
    worker = OpencodeWorker(
        kwargs.pop("prompt", "hello"),
        kwargs.pop("session_id", None),
        kwargs.pop("cwd", "/work"),
        kwargs.pop("permission_mode", "default"),
        **kwargs,
        **callbacks,
    )
    worker._do_run()
    return seen, server


def test_opencode_is_one_of_the_backends():
    assert normalize_backend("OpenCode") == BACKEND_OPENCODE
    assert normalize_backend("opencode-ai") == BACKEND_OPENCODE
    assert backend_label(BACKEND_OPENCODE) == "opencode"
    assert BACKEND_OPENCODE in agent_backends.BACKEND_IDS


def test_opencode_worker_is_the_adapter_for_opencode():
    class Claude:
        pass

    assert worker_class(BACKEND_OPENCODE, Claude) is OpencodeWorker


def test_opencode_permission_modes_translate_to_rule_sets_and_agents(monkeypatch):
    """Plan mode has to reach opencode as something it enforces, not as prose."""
    events = [_opencode_event("session.idle")]

    _seen, server = _opencode_turn(events, monkeypatch, permission_mode="plan")
    created = server.body("/session")
    assert created["agent"] == "plan"
    assert {"permission": "edit", "pattern": "*", "action": "deny"} in created["permission"]

    _seen, server = _opencode_turn(events, monkeypatch, permission_mode="bypassPermissions")
    assert server.body("/session")["permission"] == [
        {"permission": "*", "pattern": "*", "action": "allow"}
    ]

    # "Default" means whatever opencode itself is configured to do, so it must
    # not quietly install rules of BlindPilot's own.
    _seen, server = _opencode_turn(events, monkeypatch, permission_mode="default")
    assert "permission" not in server.body("/session")


def test_opencode_stream_becomes_accessible_rows_and_one_answer(monkeypatch):
    events = [
        _opencode_event("message.updated", info={"id": "msg_1", "role": "assistant"}),
        _opencode_event(
            "message.part.updated",
            part={"id": "prt_1", "messageID": "msg_1", "type": "reasoning", "text": ""},
        ),
        _opencode_event(
            "message.part.updated",
            part={
                "id": "prt_1",
                "messageID": "msg_1",
                "type": "reasoning",
                "text": "Working it out.",
            },
        ),
        _opencode_event(
            "message.part.updated",
            part={
                "id": "prt_2",
                "messageID": "msg_1",
                "type": "tool",
                "tool": "bash",
                "state": {"status": "running", "input": {"command": "wc -l app.py"}},
            },
        ),
        _opencode_event(
            "message.part.updated",
            part={
                "id": "prt_2",
                "messageID": "msg_1",
                "type": "tool",
                "tool": "bash",
                "state": {"status": "completed", "output": "42"},
            },
        ),
        _opencode_event(
            "message.part.updated",
            part={"id": "prt_3", "messageID": "msg_1", "type": "text", "text": "First half."},
        ),
        _opencode_event(
            "message.part.updated",
            part={"id": "prt_4", "messageID": "msg_1", "type": "text", "text": "Second half."},
        ),
        _opencode_event("session.idle"),
    ]

    seen, server = _opencode_turn(events, monkeypatch)

    assert seen["session"] == ["ses_test"]
    assert seen["activity"] == [
        ("thinking", "Working it out."),
        ("tool", "Running: wc -l app.py"),
        ("result", "42"),
        ("assistant", "First half."),
        ("assistant", "Second half."),
    ]
    # A part is opened empty and streamed before it is repeated in full, so the
    # empty opening must not be read out as an answer of its own.
    # Two text parts are two paragraphs, not one run-together sentence.
    assert seen["complete"] == ["First half.\n\nSecond half."]
    assert not seen["failed"]
    assert server.closed is False  # only Stop closes the stream early


def test_opencode_only_reads_its_own_conversation(monkeypatch):
    """One event stream carries every session, subagents and titles included."""
    events = [
        _opencode_event("message.updated", info={"id": "msg_1", "role": "assistant"}),
        _opencode_event(
            "message.part.updated",
            session_id="ses_someone_else",
            part={"id": "prt_9", "messageID": "msg_1", "type": "text", "text": "Not ours."},
        ),
        _opencode_event("session.idle", session_id="ses_someone_else"),
        _opencode_event(
            "message.part.updated",
            part={"id": "prt_1", "messageID": "msg_1", "type": "text", "text": "Ours."},
        ),
        _opencode_event("session.idle"),
    ]

    seen, _server = _opencode_turn(events, monkeypatch)

    assert seen["complete"] == ["Ours."]


def test_opencode_never_reads_the_users_own_message_back_as_an_answer(monkeypatch):
    events = [
        _opencode_event("message.updated", info={"id": "msg_user", "role": "user"}),
        _opencode_event(
            "message.part.updated",
            part={"id": "prt_0", "messageID": "msg_user", "type": "text", "text": "hello"},
        ),
        _opencode_event("message.updated", info={"id": "msg_1", "role": "assistant"}),
        _opencode_event(
            "message.part.updated",
            part={"id": "prt_1", "messageID": "msg_1", "type": "text", "text": "Hi."},
        ),
        _opencode_event("session.idle"),
    ]

    seen, _server = _opencode_turn(events, monkeypatch)

    assert seen["activity"] == [("assistant", "Hi.")]
    assert seen["complete"] == ["Hi."]


def test_opencode_declines_a_mid_turn_question_instead_of_waiting_forever(monkeypatch):
    """An unanswered question holds the turn open for good."""
    events = [
        _opencode_event(
            "question.asked",
            id="que_1",
            questions=[{"question": "Tabs or spaces?", "header": "Indent", "options": []}],
        ),
        _opencode_event("session.idle"),
    ]

    seen, server = _opencode_turn(events, monkeypatch)

    assert "/question/que_1/reject" in server.paths()
    assert seen["activity"] and "Tabs or spaces?" in seen["activity"][0][1]


def test_opencode_answers_a_permission_request_from_the_chosen_mode(monkeypatch):
    events = [
        _opencode_event("permission.asked", id="per_1", permission="bash"),
        _opencode_event("session.idle"),
    ]

    _seen, server = _opencode_turn(events, monkeypatch, permission_mode="auto")
    assert server.body("/permissions/per_1") == {"response": "once"}

    seen, server = _opencode_turn(events, monkeypatch, permission_mode="acceptEdits")
    # Accept edits accepts edits; a shell command keeps the normal safeguard.
    assert server.body("/permissions/per_1") == {"response": "reject"}
    assert any("Declined bash" in text for _kind, text in seen["activity"])


def test_opencode_says_so_when_it_cannot_answer_a_permission_request(monkeypatch):
    """Silence here reads as thinking, while the turn waits for good."""
    events = [
        _opencode_event("permission.asked", id="per_1", permission="bash"),
        _opencode_event("session.idle"),
    ]
    server = _ScriptedOpencodeServer(events)
    server.fail = {"/permissions/", "/permission/"}
    monkeypatch.setattr(agent_backends, "opencode_server", lambda: server)
    monkeypatch.setattr(agent_backends, "opencode_default_model", lambda *_a, **_k: "")
    activity: list[tuple[str, str]] = []
    callbacks = _callbacks()
    callbacks["on_activity"] = lambda kind, value: activity.append((kind, value))
    OpencodeWorker("hi", None, "/work", "auto", **callbacks)._do_run()

    assert any("Could not answer a permission request" in text for _kind, text in activity)


def test_opencode_error_is_reported_rather_than_left_running(monkeypatch):
    events = [
        _opencode_event(
            "session.error",
            error={"name": "UnknownError", "data": {"message": "5-hour usage limit reached."}},
        ),
    ]

    seen, _server = _opencode_turn(events, monkeypatch)

    assert seen["failed"] == ["5-hour usage limit reached."]
    assert not seen["complete"]


def test_opencode_compaction_is_a_request_of_its_own_not_a_message(monkeypatch):
    events = [_opencode_event("session.compacted")]
    monkeypatch.setattr(agent_backends, "opencode_default_model", lambda *_a, **_k: "")

    seen, server = _opencode_turn(
        events,
        monkeypatch,
        prompt="/compact",
        session_id="ses_test",
        model="opencode/flash",
        compact=True,
    )

    assert "/session/ses_test/summarize" in server.paths()
    # The prompt text is never sent: a compaction is not a message.
    assert not any("prompt_async" in path for path in server.paths())
    assert seen["complete"] == ["Conversation compacted."]


def test_opencode_will_not_compact_a_conversation_that_has_not_started(monkeypatch):
    seen, _server = _opencode_turn([], monkeypatch, prompt="/compact", compact=True)

    assert seen["failed"] == ["There is no opencode conversation to compact yet"]


def test_opencode_sends_a_model_only_when_a_tab_chose_one(monkeypatch):
    events = [_opencode_event("session.idle")]

    _seen, server = _opencode_turn(events, monkeypatch, model="opencode-go/glm-5.3")
    assert server.body("prompt_async")["model"] == {
        "providerID": "opencode-go",
        "modelID": "glm-5.3",
    }

    # No choice means opencode's own default, which is a flag left unsent.
    _seen, server = _opencode_turn(events, monkeypatch)
    assert "model" not in server.body("prompt_async")


def test_opencode_steering_adds_to_the_turn_already_running(monkeypatch):
    server = _ScriptedOpencodeServer([])
    monkeypatch.setattr(agent_backends, "opencode_server", lambda: server)
    worker = OpencodeWorker("hello", "ses_test", "/work", "default", **_callbacks())
    worker._server = server
    worker._session_id = "ses_test"

    assert worker.steer("actually, stop at three") is False  # not started yet
    worker._accepting_input.set()
    assert worker.steer("actually, stop at three") is True
    # Delivered from a thread of its own: this is called from the window's
    # thread, which must not wait on a request.
    _wait_for(lambda: server.body("prompt_async") is not None)
    assert server.body("prompt_async")["parts"] == [
        {"type": "text", "text": "actually, stop at three"}
    ]

    # Once the turn is over there is nothing left to steer, and saying so is
    # what lets the window offer to send the text as the next message instead.
    worker._accepting_input.clear()
    assert worker.steer("too late") is False


def test_opencode_stop_interrupts_the_turn_and_releases_the_reader(monkeypatch):
    server = _ScriptedOpencodeServer([])
    monkeypatch.setattr(agent_backends, "opencode_server", lambda: server)
    worker = OpencodeWorker("hello", "ses_test", "/work", "default", **_callbacks())
    worker._server = server
    worker._stream = server.open("GET", "/event")

    worker.cancel()

    assert "/session/ses_test/abort" in server.paths()
    # Closing the stream is what unblocks the thread reading it.
    assert server.closed is True


def test_opencode_catalog_names_models_by_provider_and_pools_their_efforts(monkeypatch):
    catalog = {
        "providers": [
            {
                "id": "opencode-go",
                "models": {
                    "glm-5.3": {"variants": {"high": {}, "low": {}}},
                    "kimi-k3": {"variants": {"max": {}}},
                },
            }
        ],
        "default": {"opencode-go": "kimi-k3"},
        "model": "",
    }
    monkeypatch.setattr(agent_backends, "_opencode_catalog", lambda *_a, **_k: catalog)

    models, efforts, current, effort, error = agent_backends.opencode_model_options("/work")

    assert models == ["opencode-go/glm-5.3", "opencode-go/kimi-k3"]
    # Pooled across every model, and in the order a person would expect them.
    assert efforts == ["low", "high", "max"]
    assert (current, effort, error) == ("opencode-go/kimi-k3", "", "")
    # Effort is per model, so the picker's pooled list has to be checked
    # against the model it is about to be sent with.
    assert agent_backends.opencode_model_efforts("opencode-go/kimi-k3") == ["max"]


def test_opencode_never_sends_an_effort_the_chosen_model_does_not_offer(monkeypatch):
    monkeypatch.setattr(agent_backends, "opencode_model_efforts", lambda *_a, **_k: ["max"])
    monkeypatch.setattr(agent_backends, "opencode_default_model", lambda *_a, **_k: "")
    server = _ScriptedOpencodeServer([_opencode_event("session.idle")])
    monkeypatch.setattr(agent_backends, "opencode_server", lambda: server)

    OpencodeWorker(
        "hi", None, "/work", "default", model="p/m", effort="low", **_callbacks()
    )._do_run()
    assert "variant" not in server.body("prompt_async")

    server.calls.clear()
    OpencodeWorker(
        "hi", None, "/work", "default", model="p/m", effort="max", **_callbacks()
    )._do_run()
    assert server.body("prompt_async")["variant"] == "max"


def test_opencode_server_is_reached_over_loopback_behind_a_password(monkeypatch):
    """Anything on the machine could drive an unsecured server."""
    started: dict = {}

    class FakeProcess:
        stdout = io.StringIO("opencode server listening on http://127.0.0.1:41234\n")

        def poll(self):
            return None

    def fake_popen(argv, **kwargs):
        started["argv"] = argv
        started["env"] = kwargs.get("env") or {}
        return FakeProcess()

    monkeypatch.setattr(agent_backends.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(agent_backends, "_free_port", lambda: 41234)
    monkeypatch.setattr(agent_backends, "_subprocess_env", lambda _binary: {})

    server = agent_backends.OpencodeServer("opencode")

    assert server.base_url == "http://127.0.0.1:41234"
    assert started["argv"][1:] == ["serve", "--port", "41234", "--hostname", "127.0.0.1"]
    assert started["env"]["OPENCODE_SERVER_PASSWORD"]
    assert server._auth.startswith("Basic ")


def test_opencode_prefers_its_own_executable_over_the_npm_wrapper(monkeypatch, tmp_path):
    wrapper = tmp_path / "npm" / "opencode.cmd"
    native = wrapper.parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
    native.parent.mkdir(parents=True)
    native.touch()
    monkeypatch.setattr(agent_backends.platform, "system", lambda: "Windows")

    # Terminating a .cmd launcher leaves the server it started holding the port.
    assert agent_backends._opencode_server_binary(str(wrapper)) == str(native)


def test_opencode_compaction_is_offered_and_freebuffs_is_not():
    from agent_backends import BACKENDS, compaction_request

    assert BACKENDS[BACKEND_OPENCODE].supports_compaction is True
    assert compaction_request(BACKEND_OPENCODE) == ("/compact", {"compact": True})


def test_opencode_commands_are_run_as_commands_not_typed_at_the_model(monkeypatch):
    """ "/init" sent as a message is five characters, not opencode's command."""
    events = [_opencode_event("session.idle")]

    _seen, server = _opencode_turn(
        events,
        monkeypatch,
        prompt="/init focus on the tests",
        commands=[("init", "guided setup")],
    )

    assert not any("prompt_async" in path for path in server.paths())
    assert server.body("/command") == {
        "command": "init",
        "arguments": "focus on the tests",
    }


def test_opencode_leaves_a_slash_it_does_not_know_as_an_ordinary_message(monkeypatch):
    """A sentence that happens to start with a slash is still a sentence."""
    events = [_opencode_event("session.idle")]

    _seen, server = _opencode_turn(
        events,
        monkeypatch,
        prompt="/usr/bin/env is on the path?",
        commands=[("init", "guided setup")],
    )

    assert server.body("prompt_async")["parts"] == [
        {"type": "text", "text": "/usr/bin/env is on the path?"}
    ]
    assert not any(path.endswith("/command") for path in server.paths())


def test_opencode_reports_a_command_that_will_not_run_exactly_once(monkeypatch):
    """Its command request answers only when the turn ends, so a failure can be
    noticed from either thread — and the user should hear about it once."""
    monkeypatch.setattr(
        agent_backends, "opencode_commands", lambda *_a, **_k: [("init", "guided setup")]
    )
    monkeypatch.setattr(agent_backends, "opencode_default_model", lambda *_a, **_k: "")
    server = _ScriptedOpencodeServer([])
    server.fail = {"/command"}
    monkeypatch.setattr(agent_backends, "opencode_server", lambda: server)
    failures: list[str] = []
    callbacks = _callbacks()
    callbacks["on_failed"] = failures.append
    worker = OpencodeWorker("/init", None, "/work", "default", **callbacks)

    worker._do_run()
    _wait_for(lambda: bool(failures))

    assert len(failures) == 1
    assert "/init" in failures[0]


# ----- Mid-run questions -----
#
# Every one of the four backends can stop a turn to ask a multiple-choice
# question, and each describes it differently. These check the translation both
# ways: the provider's own shape into the one dialog BlindPilot shows, and the
# chosen answers back into whatever that provider takes.


def test_codex_request_user_input_is_answered_by_question_id():
    asked = []
    sent = []
    callbacks = _callbacks()
    worker = CodexWorker(
        "test",
        None,
        ".",
        "default",
        on_question=lambda questions: asked.append(questions) or [["Spaces"]],
        **callbacks,
    )
    worker._send = lambda message: sent.append(message) or True

    worker._answer_user_input(
        7,
        {
            "questions": [
                {
                    "id": "indent",
                    "header": "Indent",
                    "question": "Tabs or spaces?",
                    "isOther": True,
                    "isSecret": False,
                    "options": [
                        {"label": "Tabs", "description": "Tab characters."},
                        {"label": "Spaces", "description": "Space characters."},
                    ],
                }
            ]
        },
    )

    (questions,) = asked
    assert [question.question for question in questions] == ["Tabs or spaces?"]
    assert [option.label for option in questions[0].options] == ["Tabs", "Spaces"]
    assert questions[0].allow_custom is True
    assert sent == [{"id": 7, "result": {"answers": {"indent": {"answers": ["Spaces"]}}}}]


def test_codex_question_nobody_answered_still_ends_the_turn():
    sent = []
    worker = CodexWorker(
        "test", None, ".", "default", on_question=lambda _questions: None, **_callbacks()
    )
    worker._send = lambda message: sent.append(message) or True

    worker._answer_user_input(
        3, {"questions": [{"id": "pick", "question": "Which one?", "options": []}]}
    )

    # An empty answer list is Codex's "the person had nothing to say"; leaving
    # the request unanswered would hold the turn open for good.
    assert sent == [{"id": 3, "result": {"answers": {"pick": {"answers": []}}}}]


def test_opencode_question_is_replied_to_in_the_order_it_was_asked():
    posted = []
    worker = OpencodeWorker(
        "test",
        None,
        ".",
        "default",
        on_question=lambda _questions: [["Rust"], ["Tests", "Docs"]],
        **_callbacks(),
    )
    worker._server = object()
    worker._session_id = "ses_1"
    worker._post = lambda routes, what: posted.append((routes, what)) or True

    worker._answer_question(
        {
            "id": "que_9",
            "sessionID": "ses_1",
            "questions": [
                {
                    "question": "Which language?",
                    "header": "Language",
                    "options": [{"label": "Rust", "description": "Fast."}],
                },
                {
                    "question": "What else?",
                    "header": "Extras",
                    "multiple": True,
                    "options": [
                        {"label": "Tests", "description": "Add tests."},
                        {"label": "Docs", "description": "Add docs."},
                    ],
                },
            ],
        }
    )

    ((routes, what),) = posted
    assert what == "a question"
    assert [path for path, _body in routes] == [
        "/question/que_9/reply",
        "/api/session/ses_1/question/que_9/reply",
    ]
    assert all(body == {"answers": [["Rust"], ["Tests", "Docs"]]} for _path, body in routes)


def test_opencode_question_nobody_answered_is_rejected_not_left_open():
    posted = []
    worker = OpencodeWorker(
        "test", None, ".", "default", on_question=lambda _questions: None, **_callbacks()
    )
    worker._server = object()
    worker._session_id = "ses_1"
    worker._post = lambda routes, what: posted.append(routes) or True

    worker._answer_question(
        {"id": "que_9", "questions": [{"question": "Which?", "options": [{"label": "A"}]}]}
    )

    (routes,) = posted
    assert [path for path, _body in routes] == [
        "/question/que_9/reject",
        "/api/session/ses_1/question/que_9/reject",
    ]


def test_opencode_multiple_flag_is_what_makes_a_question_multi_select():
    (single, several) = agent_backends._opencode_questions(
        [
            {"question": "One?", "options": [{"label": "A"}]},
            {"question": "Many?", "multiple": True, "options": [{"label": "B"}]},
        ]
    )
    assert single.multi_select is False
    assert several.multi_select is True
    # Every provider adds an "Other" of its own, so typing is always offered.
    assert single.allow_custom is True


FREEBUFF_QUESTION_SCREEN = "\n".join(
    [
        "  Freebuff will run commands on your behalf to help you build.",
        "╭──────────────── Some questions for you ────────────────╮",
        "│                                                Close ✕│",
        "│                                                       │",
        "│▼ 1. Tabs or spaces?                                   │",
        "│     ○ Tabs                                            │",
        "│     ○ Spaces                                          │",
        "│     ○ Custom                                          │",
        "│                                                       │",
        "│▶ 2. Which language?                                   │",
        "│   ↳ (click to answer)                                 │",
        "│                                                       │",
        "│╭──────────╮                                           │",
        "││  Submit  │    ↑↓ navigate • Enter select             │",
        "│╰──────────╯                                           │",
        "╰───────────────────────────────────────────────────────╯",
    ]
)


def test_freebuff_reads_the_question_that_is_open_and_counts_the_rest():
    total, index, question = agent_backends._freebuff_open_question(FREEBUFF_QUESTION_SCREEN)

    assert (total, index) == (2, 0)
    assert question is not None
    assert question.question == "Tabs or spaces?"
    # "Custom" is FreeBuff's own entry, not one of the model's answers: the
    # dialog offers typing in its own words instead.
    assert [option.label for option in question.options] == ["Tabs", "Spaces"]
    assert question.multi_select is False


def test_freebuff_checkboxes_mean_the_question_takes_several_answers():
    screen = FREEBUFF_QUESTION_SCREEN.replace("○ Tabs", "☐ Tabs").replace("○ Spaces", "☑ Spaces")
    _total, _index, question = agent_backends._freebuff_open_question(screen)

    assert question is not None
    assert question.multi_select is True


def test_freebuff_answers_a_question_with_the_keys_its_box_understands():
    keys = []
    worker = FreebuffWorker(
        "test", None, ".", "default", on_question=lambda _questions: [["Spaces"]], **_callbacks()
    )
    worker._write = lambda text: keys.append(text) or True

    _total, _index, question = agent_backends._freebuff_open_question(FREEBUFF_QUESTION_SCREEN)
    assert question is not None
    worker._choose(question, ["Spaces"])

    # "Spaces" is the second answer, so one step down and Enter takes it.
    assert keys == [agent_backends._KEY_DOWN, agent_backends._KEY_ENTER]


def test_freebuff_typed_answer_walks_past_the_options_to_its_own_entry():
    keys = []
    worker = FreebuffWorker("test", None, ".", "default", **_callbacks())
    worker._write = lambda text: keys.append(text) or True

    _total, _index, question = agent_backends._freebuff_open_question(FREEBUFF_QUESTION_SCREEN)
    assert question is not None
    worker._choose(question, ["four spaces, always"])

    down = agent_backends._KEY_DOWN
    enter = agent_backends._KEY_ENTER
    assert keys == [down, down, enter, "four spaces, always", enter]


def test_freebuff_a_question_nobody_answered_closes_the_box():
    keys = []
    worker = FreebuffWorker(
        "test", None, ".", "default", on_question=lambda _questions: None, **_callbacks()
    )
    worker._write = lambda text: keys.append(text) or True

    assert worker._answer_questions(lambda _wait: FREEBUFF_QUESTION_SCREEN) is True
    # Escape is how FreeBuff is told the questions were skipped, which is what
    # it reports to the model.
    assert keys == [agent_backends._KEY_ESCAPE]


def test_freebuff_stops_asking_once_the_box_stops_changing():
    asked = []
    worker = FreebuffWorker(
        "test",
        None,
        ".",
        "default",
        on_question=lambda questions: asked.append(questions[0].question) or [["Tabs"]],
        **_callbacks(),
    )
    worker._write = lambda _text: True

    # The screen never moves on, which is what a keystroke that did not land
    # looks like. The same question must not be put up over and over.
    assert worker._answer_questions(lambda _wait: FREEBUFF_QUESTION_SCREEN) is True
    assert asked == ["Tabs or spaces?"]


def test_freebuff_start_screen_is_recognised_however_it_is_worded():
    # The chooser is what stands between a hidden terminal and its composer, so
    # missing it costs the whole message, not just the model. Its wording has
    # moved between FreeBuff releases; all of it has to count.
    picker = agent_backends._FREEBUFF_PICKER_RE
    assert picker.search("  RECOMMENDED  DeepSeek V4 Pro")
    assert picker.search("  Start coding for free   1 day streak")
    assert picker.search("  See all 4 models")
    assert not picker.search("  Enter a coding task or / for commands")
