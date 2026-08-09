"""Unit tests for the live-activity rows: your own message, thinking, tool
steps, and tool results.

Run from the project root:

    python -m pytest tests/ -q
    # or, with no pytest installed:
    python tests/test_live_rows.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_reader import _result_label, _tool_result_text, _tool_use_label  # noqa: E402
from markdown_rows import Row, reassemble, reassemble_all  # noqa: E402


def test_live_narration_is_enabled_for_a_fresh_configuration(monkeypatch):
    import claude_reader

    monkeypatch.setattr(claude_reader, "_load_config", lambda: {})
    settings = claude_reader._Settings()

    assert settings.live_rows is True
    assert settings.speak_live is True


def test_completed_answer_is_narrated_when_no_assistant_activity_was_spoken(
    monkeypatch,
):
    import claude_reader

    spoken = []
    panel = type(
        "PanelStub",
        (),
        {
            "_assistant_narrated_this_turn": False,
            "_session_backend": claude_reader.BACKEND_FREEBUFF,
            "_say": lambda self, text: spoken.append(text) or True,
        },
    )()
    monkeypatch.setattr(claude_reader.SETTINGS, "speak_live", True)

    claude_reader.SessionPanel._narrate_completed_response(panel, "Finished cleanly.")

    assert spoken == ["FreeBuff. Finished cleanly."]
    assert panel._assistant_narrated_this_turn is True


# ----- Tool step narration -----
def test_read_says_the_file_name_only():
    label = _tool_use_label("Read", {"file_path": "/home/me/project/claude_reader.py"})
    assert label == "Reading claude_reader.py"


def test_bash_says_the_command():
    assert _tool_use_label("Bash", {"command": "pytest -q"}) == "Running: pytest -q"


def test_search_says_the_pattern():
    assert _tool_use_label("Glob", {"pattern": "*.py"}) == "Searching for *.py"


def test_unknown_tool_falls_back_to_its_name():
    assert _tool_use_label("Frobnicate", {}) == "Using Frobnicate"


def test_missing_input_never_produces_a_dangling_label():
    # A tool call with no usable parameters still reads as a whole sentence.
    for name in ("Read", "Write", "Edit", "Bash", "Grep", "WebFetch"):
        label = _tool_use_label(name, {})
        assert label and not label.endswith(" ")


def test_multiline_command_is_flattened_for_speech():
    label = _tool_use_label("Bash", {"command": "git add -A\ngit commit -m x"})
    assert "\n" not in label


# ----- Tool results -----
def test_result_text_from_plain_string():
    assert _tool_result_text("  hello\n") == "hello"


def test_result_text_from_typed_blocks_keeps_text_and_notes_images():
    content = [
        {"type": "text", "text": "line one"},
        {"type": "image", "source": {}},
    ]
    assert _tool_result_text(content) == "line one\n[image]"


def test_result_text_ignores_unexpected_shapes():
    assert _tool_result_text(None) == ""
    assert _tool_result_text([{"type": "text"}, "junk"]) == ""


def test_result_label_previews_the_first_line_and_truncates():
    text = "first line\nsecond line"
    assert _result_label(text) == "Result: first line"
    long = "x" * 300
    assert len(_result_label(long)) == len("Result: ") + 100


# ----- Copy whole response -----
def _live_rows():
    return [
        Row(kind="you", label="You: do it", payload="do it", response_number=1),
        Row(kind="header", label="Response 1", payload="Done.", response_number=1),
        Row(kind="thinking", label="Thinking: hmm", payload="hmm", response_number=1),
        Row(kind="tool", label="Reading a.py", payload="Reading a.py", response_number=1),
        Row(kind="result", label="Result: x", payload="x = 1", response_number=1),
        Row(kind="prose", label="Done.", payload="Done.", response_number=1),
    ]


def test_reassemble_copies_every_row_of_the_response_in_list_order():
    assert reassemble(_live_rows(), 1) == (
        "You: do it\n\nThinking: hmm\n\nReading a.py\n\nResult: x = 1\n\nDone."
    )


def test_reassemble_falls_back_to_the_header_when_there_are_no_other_rows():
    # Mid-stream a response can be nothing but its header.
    rows = [Row(kind="header", label="Response 1", payload="Full answer.", response_number=1)]
    assert reassemble(rows, 1) == "Full answer."


def test_reassemble_ignores_other_responses():
    rows = _live_rows() + [
        Row(kind="header", label="Response 2", payload="Second.", response_number=2),
        Row(kind="prose", label="Second.", payload="Second.", response_number=2),
    ]
    assert reassemble(rows, 2) == "Second."


def test_reassemble_all_covers_the_whole_list_with_response_markers():
    rows = _live_rows() + [
        Row(kind="header", label="Response 2", payload="Second.", response_number=2),
        Row(
            kind="code",
            label="Code, Python, 1 line",
            payload="x = 1",
            response_number=2,
            language="Python",
            lang_token="python",
        ),
    ]
    assert reassemble_all(rows) == (
        "You: do it\n\n"
        "Response 1\n\n"
        "Thinking: hmm\n\n"
        "Reading a.py\n\n"
        "Result: x = 1\n\n"
        "Done.\n\n"
        "Response 2\n\n"
        "```python\nx = 1\n```"
    )


# ----- Stream wiring: which events become live activity -----
class _FakeStdin:
    """Captures what the worker writes into the process."""

    def __init__(self):
        self.written: list[str] = []
        self.closed = False

    def write(self, data):
        if self.closed:
            raise ValueError("write to closed pipe")
        self.written.append(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _FakeProc:
    """Stands in for the Claude Code subprocess, replaying canned stdout."""

    def __init__(self, lines):
        import io

        self.stdin = _FakeStdin()
        self.stdout = iter(lines)
        self.stderr = io.StringIO("")
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0


def _run_worker(events, on_activity=None):
    """Drive ClaudeWorker over `events` and collect its activity callbacks."""
    import json
    import subprocess

    import claude_reader

    lines = [json.dumps(e) + "\n" for e in events]
    activity: list[tuple[str, str]] = []
    completed: list[str] = []
    procs: list[_FakeProc] = []

    def fake_popen(*_a, **_k):
        proc = _FakeProc(lines)
        procs.append(proc)
        return proc

    def record(kind, text):
        activity.append((kind, text))
        if on_activity is not None:
            on_activity(procs[0] if procs else None, kind, text)

    real_popen, real_find = subprocess.Popen, claude_reader._find_claude
    subprocess.Popen = fake_popen  # type: ignore[assignment]
    claude_reader._find_claude = lambda: "claude"  # type: ignore[assignment]
    try:
        worker = claude_reader.ClaudeWorker(
            "hi",
            None,
            os.getcwd(),
            "default",
            on_session=lambda _sid: None,
            on_started=lambda: None,
            on_activity=record,
            on_complete=completed.append,
            on_failed=lambda msg: completed.append("FAILED: " + msg),
            on_done=lambda: None,
        )
        worker.run()
    finally:
        subprocess.Popen = real_popen  # type: ignore[assignment]
        claude_reader._find_claude = real_find  # type: ignore[assignment]
    return activity, completed, procs[0]


def test_stream_emits_thinking_tool_result_and_text_in_order():
    activity, completed, proc = _run_worker(
        [
            {"type": "system", "subtype": "init", "session_id": "abc"},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "I should look at the file."},
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"file_path": "a.py"},
                        },
                    ]
                },
            },
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "content": "x = 1"}]},
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "It sets x."}]},
            },
            {"type": "result", "subtype": "success"},
        ]
    )
    assert activity == [
        ("thinking", "I should look at the file."),
        ("tool", "Reading a.py"),
        ("result", "x = 1"),
        ("assistant", "It sets x."),
    ]
    # Thinking and tool chatter stay out of the final answer text.
    assert completed == ["It sets x."]
    # The prompt went in over stdin as a stream-json user message...
    import json as _json

    first = _json.loads(proc.stdin.written[0])
    assert first["type"] == "user"
    assert first["message"]["content"][0]["text"] == "hi"
    # ...and stdin was closed once the turn's result arrived, so the CLI can exit.
    assert proc.stdin.closed


def test_redacted_thinking_still_announces_something():
    activity, _, _ = _run_worker(
        [
            {
                "type": "assistant",
                "message": {"content": [{"type": "redacted_thinking", "data": "..."}]},
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "ok"}]},
            },
            {"type": "result", "subtype": "success"},
        ]
    )
    assert activity[0] == ("thinking", "[redacted thinking]")


# ----- Steering a run that is already going -----
def test_steer_writes_a_second_message_into_the_running_process():
    import json as _json

    sent = {}
    running = {}

    def steer_when_the_tool_runs(kind, _text):
        # Mid-run, exactly when the user would hear "Reading a.py" and type.
        if kind == "tool" and "steered" not in sent:
            sent["steered"] = running["worker"].steer("actually, stop")

    events = [
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}]
            },
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "stopped"}]},
        },
        {"type": "result", "subtype": "success"},
    ]

    import claude_reader
    import subprocess

    lines = [_json.dumps(e) + "\n" for e in events]
    procs = []

    def fake_popen(*_a, **_k):
        proc = _FakeProc(lines)
        procs.append(proc)
        return proc

    real_popen, real_find = subprocess.Popen, claude_reader._find_claude
    subprocess.Popen = fake_popen  # type: ignore[assignment]
    claude_reader._find_claude = lambda: "claude"  # type: ignore[assignment]
    try:
        worker = claude_reader.ClaudeWorker(
            "do a thing",
            None,
            os.getcwd(),
            "default",
            on_session=lambda _sid: None,
            on_started=lambda: None,
            on_activity=steer_when_the_tool_runs,
            on_complete=lambda _t: None,
            on_failed=lambda _m: None,
            on_done=lambda: None,
        )
        running["worker"] = worker
        worker.run()
    finally:
        subprocess.Popen = real_popen  # type: ignore[assignment]
        claude_reader._find_claude = real_find  # type: ignore[assignment]

    assert sent.get("steered") is True
    written = [_json.loads(w) for w in procs[0].stdin.written]
    assert [m["message"]["content"][0]["text"] for m in written] == [
        "do a thing",
        "actually, stop",
    ]


def test_steer_is_refused_once_the_run_is_over():
    import claude_reader

    worker = claude_reader.ClaudeWorker(
        "hi",
        None,
        os.getcwd(),
        "default",
        on_session=lambda _s: None,
        on_started=lambda: None,
        on_activity=lambda _k, _t: None,
        on_complete=lambda _t: None,
        on_failed=lambda _m: None,
        on_done=lambda: None,
    )
    # Never started, so there is nothing listening — must refuse, not raise.
    assert worker.steer("too late") is False


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            passed += 1
        except Exception:
            failed += 1
            print(f"FAIL: {fn.__name__}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


# ----- Stopping a run -----
class _Button:
    """Minimal stand-in for the wx buttons the lifecycle handlers touch."""

    def __init__(self) -> None:
        self.enabled = True

    def Enable(self) -> None:
        self.enabled = True

    def Disable(self) -> None:
        self.enabled = False


class _Earcons:
    def __init__(self) -> None:
        self.stopped = 0

    def stop_progress(self) -> None:
        self.stopped += 1

    def play_received(self) -> None:
        pass


def _stub_panel(app, **overrides):
    """A SessionPanel stand-in carrying only the state these handlers use."""
    panel = type("PanelStub", (), {})()
    panel._earcons = _Earcons()
    panel._turns = []
    panel._rows = []
    panel._response_count = 0
    panel._stream_response = None
    panel._streamed_assistant = ""
    panel._stopping = False
    panel._assistant_narrated_this_turn = True
    panel._session_backend = app.BACKEND_FREEBUFF
    panel.announced = []
    panel.status = []
    panel._announce = lambda text: panel.announced.append(text)
    panel._set_status = lambda text: panel.status.append(text)
    panel._refresh_list = lambda: None
    panel._say = lambda _text: False
    panel.send_btn = _Button()
    panel.steer_btn = _Button()
    panel.stop_btn = _Button()
    panel._finish_stopped_turn = lambda: app.SessionPanel._finish_stopped_turn(panel)
    for name, value in overrides.items():
        setattr(panel, name, value)
    return panel


def test_stopping_keeps_the_partial_answer_and_is_not_reported_as_an_error():
    import blindpilot_app as app

    panel = _stub_panel(app)
    panel._stopping = True
    panel._turns = [app.Turn(prompt="Do the work")]
    panel._streamed_assistant = "Got partway."
    panel._stream_response = 1
    panel._rows = [app.Row(kind="header", label="Response 1", payload="", response_number=1)]

    # The cancelled backend reports its own interruption; the user asked for it.
    app.SessionPanel._on_failed(panel, "FreeBuff reported that the response was interrupted")
    assert panel.announced == []
    assert panel._turns == [app.Turn(prompt="Do the work")]

    app.SessionPanel._on_worker_finished(panel)

    assert panel.announced == ["Stopped"]
    assert panel._turns[0].response == "Got partway."
    assert panel._rows[0].payload == "Got partway."
    assert panel._stream_response is None
    assert panel._stopping is False
    assert panel.send_btn.enabled is True
    assert panel.steer_btn.enabled is False
    assert panel.stop_btn.enabled is False


def test_stop_without_a_running_task_says_so_and_does_nothing():
    import blindpilot_app as app

    panel = _stub_panel(app, _worker=None)

    app.SessionPanel._on_stop(panel)

    assert panel.status == ["Error: Nothing is running to stop"]
    assert panel._stopping is False


def test_finished_answer_that_never_streamed_is_still_added_to_the_list():
    """Streaming is best effort, so the final text is what the list must show."""
    import blindpilot_app as app

    panel = _stub_panel(app)
    panel._turns = [app.Turn(prompt="Say banana")]
    panel._stream_response = 1
    panel._response_count = 1
    panel._streamed_assistant = ""
    panel._rows = [app.Row(kind="header", label="Response 1", payload="", response_number=1)]
    panel._narrate_completed_response = lambda _text: None

    app.SessionPanel._on_response_complete(panel, "banana")

    assert [row.label for row in panel._rows[1:]] == ["FreeBuff: banana"]
    assert panel._rows[0].payload == "banana"


def test_answer_already_streamed_is_not_added_to_the_list_twice():
    import blindpilot_app as app

    panel = _stub_panel(app)
    panel._turns = [app.Turn(prompt="Say banana")]
    panel._stream_response = 1
    panel._response_count = 1
    # Codex joins its final text differently from the pieces it streamed, so
    # only the characters can decide whether it is the same answer.
    panel._streamed_assistant = "One.\n\nTwo."
    panel._rows = [app.Row(kind="header", label="Response 1", payload="", response_number=1)]
    panel._narrate_completed_response = lambda _text: None

    app.SessionPanel._on_response_complete(panel, "One.Two.")

    assert len(panel._rows) == 1
