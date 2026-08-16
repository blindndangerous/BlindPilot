"""Hermes backend regression tests.

These lock down the behaviours that were wrong in the first working version
and were only found by talking to a real Hermes: the spinner arriving as
reasoning, a tool result that is an object rather than a string, and a status
check whose exit code says nothing. Each of those produced a turn that looked
successful while losing something a screen-reader user needs.
"""

from __future__ import annotations

import json
import threading

import agent_backends
import hermes_backend
import hermes_worker
from agent_backends import (
    BACKEND_CLAUDE,
    BACKEND_HERMES,
    BACKENDS,
    backend_label,
    compaction_request,
    normalize_backend,
    worker_class,
)
from hermes_worker import HermesWorker


def _callbacks() -> dict:
    return {
        "on_session": lambda _value: None,
        "on_started": lambda: None,
        "on_activity": lambda _kind, _value: None,
        "on_complete": lambda _value: None,
        "on_failed": lambda _value: None,
        "on_done": lambda: None,
    }


def _worker(**overrides) -> HermesWorker:
    callbacks = _callbacks()
    callbacks.update(overrides)
    return HermesWorker("test", None, ".", "default", **callbacks)


class _FakeTransport:
    """A transport that replays scripted frames, so no Hermes is needed."""

    def __init__(self, frames: list[dict]) -> None:
        self.frames = list(frames)
        self.sent: list[dict] = []
        self.closed = False

    def send(self, message: dict) -> bool:
        self.sent.append(message)
        return True

    def receive(self, timeout: float) -> dict | None:  # noqa: ARG002 - interface
        return self.frames.pop(0) if self.frames else None

    def close(self) -> None:
        self.closed = True

    def failure_detail(self) -> str:
        return "fake transport ended"


def _event(kind: str, payload: dict | None = None) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"type": kind, "session_id": "s1", "payload": payload or {}},
    }


# -- registration ----------------------------------------------------------


def test_hermes_is_registered_and_named():
    assert normalize_backend("Hermes") == BACKEND_HERMES
    assert normalize_backend("hermes-agent") == BACKEND_HERMES
    assert normalize_backend("NOUS") == BACKEND_HERMES
    assert backend_label(BACKEND_HERMES) == "Hermes"
    assert BACKEND_HERMES in agent_backends.BACKEND_IDS


def test_worker_class_selects_the_hermes_adapter():
    class Claude:
        pass

    assert worker_class(BACKEND_HERMES, Claude) is HermesWorker
    # The other backends must keep resolving as before.
    assert worker_class(BACKEND_CLAUDE, Claude) is Claude


def test_declared_capabilities_match_what_the_worker_implements():
    info = BACKENDS[BACKEND_HERMES]
    assert info.supports_steering is True
    assert info.supports_compaction is True
    assert info.supports_permissions is True
    # Hermes exposes no per-turn reasoning effort on this protocol. Claiming it
    # would put a control in the UI that silently does nothing.
    assert info.supports_effort is False
    # A backend that says it compacts must have a request to compact with.
    assert compaction_request(BACKEND_HERMES) is not None


# -- reasoning versus the spinner -----------------------------------------


def test_the_terminal_spinner_never_becomes_a_reasoning_row():
    """Hermes streams a kawaii spinner on the reasoning channel.

    It is a progress indicator for a terminal. Read aloud it is noise, so it
    must not reach the transcript.
    """
    rows = []
    worker = _worker(on_activity=lambda kind, value: rows.append((kind, value)))
    for text in ("(⌐■_■) contemplating...", "٩(๑❛ᴗ❛๑)۶ musing...", "ಠ_ಠ deliberating..."):
        worker._handle_event(_event("thinking.delta", {"text": text}))
    worker._handle_event(_event("message.complete", {"text": "Done.", "status": "complete"}))

    assert [kind for kind, _ in rows] == []


def test_real_reasoning_becomes_its_own_row():
    rows = []
    worker = _worker(on_activity=lambda kind, value: rows.append((kind, value)))
    worker._handle_event(
        _event("reasoning.available", {"text": "The file is missing, so I will create it."})
    )
    worker._handle_event(_event("message.complete", {"text": "Created.", "status": "complete"}))

    assert rows == [("thinking", "The file is missing, so I will create it.")]


def test_reasoning_identical_to_the_answer_is_not_read_twice():
    """Some providers report a one-word answer as its own reasoning."""
    rows = []
    completed = []
    worker = _worker(
        on_activity=lambda kind, value: rows.append((kind, value)),
        on_complete=completed.append,
    )
    worker._handle_event(_event("reasoning.available", {"text": "PONG"}))
    worker._handle_event(_event("message.complete", {"text": "PONG", "status": "complete"}))

    assert rows == []
    assert completed == ["PONG"]


# -- tool rows -------------------------------------------------------------


def test_a_tool_result_object_still_produces_a_result_row():
    """Hermes sends most results decoded, not as a string.

    The first version only read strings, so tools ran and reported nothing -
    a silent turn that looked like a success.
    """
    rows = []
    worker = _worker(on_activity=lambda kind, value: rows.append((kind, value)))
    worker._handle_event(
        _event("tool.start", {"tool_id": "t1", "name": "terminal", "context": "echo hi"})
    )
    worker._handle_event(
        _event(
            "tool.complete",
            {
                "tool_id": "t1",
                "name": "terminal",
                "result": {"output": "hi", "exit_code": 0, "error": None},
            },
        )
    )

    assert rows == [("tool", "terminal: echo hi"), ("result", "terminal: hi")]


def test_a_failing_command_reports_its_error_and_exit_code():
    rows = []
    worker = _worker(on_activity=lambda kind, value: rows.append((kind, value)))
    worker._handle_event(
        _event(
            "tool.complete",
            {
                "tool_id": "t2",
                "name": "terminal",
                "result": {"output": "", "exit_code": 127, "error": "not found"},
            },
        )
    )

    assert len(rows) == 1
    kind, text = rows[0]
    assert kind == "result"
    assert "not found" in text
    assert "127" in text


def test_hermes_own_summary_is_preferred_over_the_raw_result():
    rows = []
    worker = _worker(on_activity=lambda kind, value: rows.append((kind, value)))
    worker._handle_event(
        _event(
            "tool.complete",
            {
                "tool_id": "t3",
                "name": "web_search",
                "summary": "Did 3 searches in 2.1s",
                "result": {"data": {"web": [1, 2, 3]}},
            },
        )
    )

    assert rows == [("result", "web_search: Did 3 searches in 2.1s")]


def test_a_huge_tool_result_is_bounded_and_says_so():
    """A screen reader walks a row line by line, so results are capped."""
    rows = []
    worker = _worker(on_activity=lambda kind, value: rows.append((kind, value)))
    output = "\n".join(f"line {n}" for n in range(500))
    worker._handle_event(
        _event("tool.complete", {"tool_id": "t4", "name": "terminal", "result": {"output": output}})
    )

    _kind, text = rows[0]
    assert len(text.splitlines()) <= hermes_worker._RESULT_MAX_LINES + 2
    assert "more lines not shown" in text


def test_an_unrecognised_result_shape_still_says_the_tool_finished():
    """Silence reads as a hang, so an unknown shape is still reported."""
    rows = []
    worker = _worker(on_activity=lambda kind, value: rows.append((kind, value)))
    worker._handle_event(
        _event("tool.complete", {"tool_id": "t5", "name": "mystery", "result": {"odd": 1}})
    )

    assert rows and rows[0][0] == "result"
    assert "finished" in rows[0][1]


# -- streaming and turn completion ---------------------------------------


def test_streamed_deltas_become_the_completed_answer():
    completed = []
    worker = _worker(on_complete=completed.append)
    for chunk in ("Blind", "Pilot", " ready."):
        worker._handle_event(_event("message.delta", {"text": chunk}))
    worker._handle_event(_event("message.complete", {"status": "complete"}))

    assert completed == ["BlindPilot ready."]


def test_an_interrupted_turn_is_reported_once_and_not_as_an_answer():
    failed = []
    completed = []
    worker = _worker(on_failed=failed.append, on_complete=completed.append)
    worker._handle_event(_event("message.complete", {"status": "interrupted"}))

    assert completed == []
    assert len(failed) == 1


def test_a_user_cancelled_turn_is_not_reported_as_a_failure():
    """Stopping a turn on purpose is not an error worth announcing."""
    failed = []
    worker = _worker(on_failed=failed.append)
    worker._cancelled = True
    worker._handle_event(_event("message.complete", {"status": "interrupted"}))

    assert failed == []


def test_unknown_events_are_ignored_rather_than_breaking_the_turn():
    """Hermes gains events over releases; an unknown one must not be fatal."""
    rows = []
    failed = []
    worker = _worker(
        on_activity=lambda kind, value: rows.append((kind, value)),
        on_failed=failed.append,
    )
    assert worker._handle_event(_event("some.future.event", {"whatever": True})) is None
    assert worker._handle_event({"jsonrpc": "2.0", "result": {}, "id": 1}) is None
    assert rows == []
    assert failed == []


def test_the_noisy_session_list_event_produces_no_rows():
    """``sessions.changed`` arrives dozens of times with an empty payload.

    Acting on each one would rebuild the conversation list constantly, which
    on Windows clears the selection and interrupts whatever is being read.
    """
    rows = []
    worker = _worker(on_activity=lambda kind, value: rows.append((kind, value)))
    for _ in range(30):
        worker._handle_event(_event("sessions.changed", {}))

    assert rows == []


# -- permissions and approvals -------------------------------------------


def test_permission_modes_map_onto_whether_hermes_may_act_unattended():
    worker = _worker()
    worker._permission_mode = "bypassPermissions"
    assert worker._session_params()["yolo"] is True
    worker._permission_mode = "default"
    assert worker._session_params()["yolo"] is False
    worker._permission_mode = "plan"
    assert worker._session_params()["yolo"] is False


def test_an_approval_is_answered_so_the_turn_cannot_hang():
    """An unanswered approval leaves Hermes waiting forever."""
    for mode, expected in (("auto", "approve"), ("default", "deny")):
        rows = []
        worker = _worker(on_activity=lambda kind, value: rows.append((kind, value)))
        worker._permission_mode = mode
        transport = _FakeTransport([])
        worker._transport = transport
        worker._handle_event(
            _event("approval.request", {"request_id": "r1", "command": "rm -rf /tmp/x"})
        )

        answers = [m for m in transport.sent if m.get("method") == "approval.respond"]
        assert len(answers) == 1
        assert answers[0]["params"]["decision"] == expected
        # Either way the user is told what happened.
        assert rows and rows[0][0] == "tool"


def test_a_denied_approval_explains_how_to_allow_it():
    rows = []
    worker = _worker(on_activity=lambda kind, value: rows.append((kind, value)))
    worker._permission_mode = "default"
    worker._transport = _FakeTransport([])
    worker._handle_event(_event("approval.request", {"request_id": "r2", "command": "git push"}))

    assert "permission mode" in rows[0][1]


# -- steering and cancelling ---------------------------------------------


def test_steering_is_refused_before_the_turn_is_live():
    worker = _worker()
    worker._transport = _FakeTransport([])
    worker._gateway_session = "s1"
    assert worker.steer("go left") is False

    worker._accepting_input.set()
    worker._live_session = "s1"
    assert worker.steer("go left") is True
    steers = [m for m in worker._transport.sent if m.get("method") == "session.steer"]
    assert steers and steers[0]["params"]["text"] == "go left"


def test_cancel_asks_hermes_to_stop_before_dropping_the_connection():
    """A remote Hermes would otherwise keep working on a dead answer."""
    worker = _worker()
    transport = _FakeTransport([])
    worker._transport = transport
    worker._gateway_session = "s1"
    worker.cancel()

    assert [m["method"] for m in transport.sent] == ["session.interrupt"]
    assert transport.closed is True


# -- compaction -----------------------------------------------------------


def test_compacting_without_a_conversation_is_refused_clearly():
    failed = []
    callbacks = _callbacks()
    callbacks["on_failed"] = failed.append
    worker = HermesWorker("", None, ".", "default", compact=True, **callbacks)
    worker._do_run()

    assert failed and "compact" in failed[0].lower()


def test_compaction_uses_hermes_own_request_and_says_what_happened():
    completed = []
    callbacks = _callbacks()
    callbacks["on_complete"] = completed.append
    worker = HermesWorker("", "stored-1", ".", "default", compact=True, **callbacks)
    transport = _FakeTransport([{"jsonrpc": "2.0", "id": 101, "result": {}}])
    worker._transport = transport
    worker._live_session = "live-1"
    worker._request_id = 100
    worker._run_compaction()

    assert [m["method"] for m in transport.sent] == ["session.compress"]
    # A compaction turn has no answer text, so silence would read as failure.
    assert completed == ["Conversation compacted."]


# -- resuming -------------------------------------------------------------


def test_resuming_asks_hermes_to_reopen_the_stored_conversation():
    callbacks = _callbacks()
    sessions = []
    callbacks["on_session"] = sessions.append
    worker = HermesWorker("hi", "stored-7", ".", "default", **callbacks)
    transport = _FakeTransport([{"jsonrpc": "2.0", "id": 101, "result": {"session_id": "live-7"}}])
    worker._transport = transport
    worker._request_id = 100

    assert worker._ensure_session() is True
    request = transport.sent[0]
    assert request["method"] == "session.resume"
    assert request["params"]["session_id"] == "stored-7"
    # The transcript is already on screen; replaying it would duplicate rows.
    assert request["params"]["omit_messages"] is True


def test_a_new_conversation_reports_the_id_that_survives_a_restart():
    """Two ids come back; only the stored one can reopen the conversation."""
    sessions = []
    callbacks = _callbacks()
    callbacks["on_session"] = sessions.append
    worker = HermesWorker("hi", None, "/tmp", "default", **callbacks)
    transport = _FakeTransport(
        [
            {
                "jsonrpc": "2.0",
                "id": 101,
                "result": {"session_id": "live-1", "stored_session_id": "20260816_1200_abc"},
            }
        ]
    )
    worker._transport = transport
    worker._request_id = 100

    assert worker._ensure_session() is True
    assert sessions == ["20260816_1200_abc"]
    assert worker._live_session == "live-1"


def test_a_session_error_is_surfaced_with_hermes_own_message():
    failed = []
    callbacks = _callbacks()
    callbacks["on_failed"] = failed.append
    worker = HermesWorker("hi", None, "/tmp", "default", **callbacks)
    worker._transport = _FakeTransport(
        [{"jsonrpc": "2.0", "id": 101, "error": {"code": 4001, "message": "session not found"}}]
    )
    worker._request_id = 100

    assert worker._ensure_session() is False
    assert failed == ["session not found"]


# -- discovery ------------------------------------------------------------


def test_the_model_catalog_qualifies_every_model_with_its_provider():
    """Hermes groups models under providers and repeats names across them.

    The qualified form is also what its /model command accepts, so a picked
    row can go back unchanged.
    """
    models, current = hermes_backend._model_rows(
        {
            "providers": [
                {"slug": "openai", "models": ["gpt-5", "gpt-5-mini"], "is_current": False},
                {"slug": "anthropic", "models": ["gpt-5"], "is_current": True},
            ]
        }
    )

    assert models == ["openai:gpt-5", "openai:gpt-5-mini", "anthropic:gpt-5"]
    assert current == "anthropic:gpt-5"


def test_providers_without_credentials_are_left_out_of_the_picker():
    """Offering one would fail at the first turn, after the user chose it."""
    models, _current = hermes_backend._model_rows(
        {
            "providers": [
                {"slug": "ready", "models": ["a"], "authenticated": True},
                {"slug": "nope", "models": ["b"], "authenticated": False},
            ]
        }
    )

    assert models == ["ready:a"]


def test_a_malformed_catalog_produces_no_rows_rather_than_raising():
    assert hermes_backend._model_rows({}) == ([], "")
    assert hermes_backend._model_rows({"providers": "nonsense"}) == ([], "")
    assert hermes_backend._model_rows({"providers": [None, {"models": ["x"]}]}) == ([], "")


def test_the_picker_reports_a_missing_hermes_instead_of_waiting(monkeypatch):
    monkeypatch.setattr(hermes_backend, "hermes_installed", lambda: False)
    models, efforts, current, effort, error = hermes_backend.hermes_model_options()

    assert (models, efforts, current, effort) == ([], [], "", "")
    assert "not found" in error


def test_hermes_is_only_usable_when_its_python_environment_is_present(monkeypatch):
    """A launcher alone cannot run the gateway, which is a module in the venv."""
    monkeypatch.setattr(hermes_backend, "hermes_python", lambda: None)
    assert hermes_backend.hermes_installed() is False
    monkeypatch.setattr(hermes_backend, "hermes_python", lambda: "/somewhere/python")
    assert hermes_backend.hermes_installed() is True


def test_a_configured_model_counts_as_authenticated(monkeypatch):
    """``hermes status`` exits 0 even when nothing is set up.

    So the check has to read the output. Trusting the exit code reported an
    unconfigured Hermes as ready, and the failure only showed up mid-turn.
    """
    monkeypatch.setattr(hermes_backend, "find_hermes_cli", lambda: "/bin/hermes")

    def _status(output: str, code: int = 0):
        def _run(*_args, **_kwargs):
            return type("Proc", (), {"returncode": code, "stdout": output, "stderr": ""})()

        return _run

    monkeypatch.setattr(hermes_backend.subprocess, "run", _status("  Model:  claude-sonnet-4\n"))
    assert hermes_backend.hermes_auth_ok() is True

    monkeypatch.setattr(hermes_backend.subprocess, "run", _status("  Model:  (not set)\n"))
    assert hermes_backend.hermes_auth_ok() is False

    monkeypatch.setattr(hermes_backend.subprocess, "run", _status("no model line here\n"))
    assert hermes_backend.hermes_auth_ok() is False


def test_a_missing_hermes_is_reported_rather_than_crashing(monkeypatch):
    failed = []
    callbacks = _callbacks()
    callbacks["on_failed"] = failed.append
    monkeypatch.setattr(hermes_worker, "hermes_installed", lambda: False)
    worker = HermesWorker("hi", None, ".", "default", **callbacks)

    assert worker._open_transport() is False
    assert failed and "not installed" in failed[0]


# -- remote mode ----------------------------------------------------------


def test_the_gateway_url_is_built_from_a_host_the_user_types():
    """Asking for a full ws:// URL with the right path invites getting it wrong."""
    assert hermes_backend.remote_ws_url("garfield") == "ws://garfield:9119/api/ws"
    assert hermes_backend.remote_ws_url("100.64.0.5", 9223) == "ws://100.64.0.5:9223/api/ws"
    # A port typed into the host field wins over the separate field.
    assert hermes_backend.remote_ws_url("garfield:9300", 9119) == "ws://garfield:9300/api/ws"
    # And a pasted URL is tolerated rather than mangled.
    assert hermes_backend.remote_ws_url("ws://box:9119/api/ws") == "ws://box:9119/api/ws"
    assert hermes_backend.remote_ws_url("https://box") == "wss://box:9119/api/ws"


def test_the_credential_travels_in_the_query_string_not_a_header():
    """Hermes' WebSocket upgrade reads its credential from the URL.

    A header alone is answered with 403, which was the first version's bug.
    """
    url = hermes_backend._authenticated_ws_url("ws://box:9119/api/ws", "s3cret", "token")
    assert url == "ws://box:9119/api/ws?token=s3cret"

    # A server reachable from outside this machine wants a minted ticket.
    ticket = hermes_backend._authenticated_ws_url("ws://box:9119/api/ws", "abc", "ticket")
    assert ticket == "ws://box:9119/api/ws?ticket=abc"

    # An unknown credential name falls back rather than sending nonsense.
    odd = hermes_backend._authenticated_ws_url("ws://box:9119/api/ws", "abc", "nonsense")
    assert odd == "ws://box:9119/api/ws?token=abc"

    # A credential with URL-significant characters must survive intact.
    quoted = hermes_backend._authenticated_ws_url("ws://box/api/ws", "a b&c=d", "token")
    assert "a%20b%26c%3Dd" in quoted


def test_no_credential_means_no_query_string():
    assert hermes_backend._authenticated_ws_url("ws://box/api/ws", "", "token") == (
        "ws://box/api/ws"
    )


def test_the_address_shown_to_the_user_never_carries_the_key():
    """A token read aloud by a screen reader is a token read out to the room."""
    assert hermes_backend._redacted_ws_url("ws://box/api/ws?token=s3cret") == "ws://box/api/ws"


def test_connection_failures_say_what_to_do_about_them():
    """ "Could not connect" is useless to someone who cannot glance at a log."""
    url = "ws://box:9119/api/ws"

    rejected = hermes_backend._remote_failure_message(url, OSError("Handshake status 403"))
    assert "key" in rejected.lower()

    refused = hermes_backend._remote_failure_message(url, OSError("[Errno 111] Connection refused"))
    assert "hermes serve" in refused

    timeout = hermes_backend._remote_failure_message(url, OSError("timed out"))
    assert "hermes serve" in timeout and "reachable" in timeout

    unknown = hermes_backend._remote_failure_message(url, OSError("getaddrinfo failed"))
    assert "host name" in unknown

    # Every message names the address, and none of them leaks a credential.
    for message in (rejected, refused, timeout, unknown):
        assert url in message


def test_a_missing_websocket_library_is_reported_as_a_fixable_thing(monkeypatch):
    """The local backend needs no such library, so this must not be fatal.

    It is an optional dependency; the message has to name the package.
    """
    import builtins

    real_import = builtins.__import__

    def _no_websocket(name, *args, **kwargs):
        if name == "websocket":
            raise ImportError("no module named websocket")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_websocket)
    transport = hermes_backend.WebSocketTransport("ws://box/api/ws", "t")
    try:
        transport.start()
    except OSError as exc:
        assert "websocket-client" in str(exc)
    else:  # pragma: no cover - the import must fail here
        raise AssertionError("expected the missing library to be reported")


def test_a_remote_url_selects_the_network_transport(monkeypatch):
    """The remote path is the whole point of using this protocol."""
    created = {}

    class _Fake:
        def __init__(self, url, token="", credential="token"):
            created["url"] = url
            created["token"] = token
            created["credential"] = credential

        def start(self):
            return None

    monkeypatch.setattr(hermes_worker, "WebSocketTransport", _Fake)
    callbacks = _callbacks()
    worker = HermesWorker(
        "hi",
        None,
        ".",
        "default",
        remote_url="ws://192.168.1.10:9119/api/ws",
        remote_token="secret",
        **callbacks,
    )

    assert worker._open_transport() is True
    assert created == {
        "url": "ws://192.168.1.10:9119/api/ws",
        "token": "secret",
        "credential": "token",
    }


def test_an_unreachable_remote_hermes_says_where_it_tried(monkeypatch):
    """ "Could not connect" without an address is useless to a blind user."""

    class _Fake:
        def __init__(self, url, token="", credential="token"):
            self._url = url

        def start(self):
            raise OSError(f"Could not reach Hermes at {self._url}: refused")

    monkeypatch.setattr(hermes_worker, "WebSocketTransport", _Fake)
    failed = []
    callbacks = _callbacks()
    callbacks["on_failed"] = failed.append
    worker = HermesWorker(
        "hi", None, ".", "default", remote_url="ws://nope:9119/api/ws", **callbacks
    )

    assert worker._open_transport() is False
    assert "ws://nope:9119/api/ws" in failed[0]


# -- transport framing ----------------------------------------------------


def test_frames_are_newline_delimited_json(tmp_path):
    """Hermes' gateway reads one JSON object per line.

    Verified against the real gateway; this keeps a future refactor from
    quietly switching to Content-Length framing, which it does not speak.
    """
    transport = hermes_backend.StdioTransport(str(tmp_path))
    written: list[str] = []

    class _Stdin:
        def write(self, data):
            written.append(data)

        def flush(self):
            return None

    transport._proc = type("Proc", (), {"stdin": _Stdin(), "poll": lambda self: None})()
    assert transport.send({"method": "ping", "params": {}}) is True
    assert written[0].endswith("\n")
    assert json.loads(written[0]) == {"method": "ping", "params": {}}


def test_stderr_noise_is_never_treated_as_protocol(tmp_path):
    """Hermes warns about SQLite and MCP servers on stderr while working fine.

    Those lines only ever explain an exit that already happened.
    """
    transport = hermes_backend.StdioTransport(str(tmp_path))
    transport._stderr.extend(["state.db: linked SQLite is vulnerable", "browser tools missing"])
    detail = transport.failure_detail()

    assert "SQLite" in detail
    # And with nothing to report, the message still says something usable.
    empty = hermes_backend.StdioTransport(str(tmp_path))
    assert empty.failure_detail()


def test_the_reader_thread_hands_frames_over_in_order(tmp_path):
    transport = hermes_backend.StdioTransport(str(tmp_path))
    with transport._frames_ready:
        transport._frames.extend([{"n": 1}, {"n": 2}])
        transport._frames_ready.notify_all()

    assert transport.receive(0.1) == {"n": 1}
    assert transport.receive(0.1) == {"n": 2}
    assert transport.receive(0.01) is None


def test_the_worker_never_blocks_forever_on_a_silent_peer():
    """A dropped network link goes quiet without closing, so reads time out."""
    worker = _worker()
    worker._transport = _FakeTransport([])
    worker._request_id = 100
    done = threading.Event()

    def _run():
        worker._await_response(999, 0.2)
        done.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    assert done.wait(5) is True
