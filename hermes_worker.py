"""The Hermes turn worker: one BlindPilot turn over Hermes' JSON-RPC gateway.

Split from ``hermes_backend.py`` so the transport question (how bytes travel)
stays separate from the conversation question (what a turn looks like).

Copyright (c) 2026 doubletaponair and BlindPilot contributors.
Based on the original Claude Code Reader application by doubletaponair:
https://github.com/doubletaponair/claude-code-reader
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from hermes_backend import (
    StdioTransport,
    Transport,
    WebSocketTransport,
    hermes_installed,
)

# How long a single read may block. Short enough that cancel is felt promptly,
# long enough not to spin: the loop simply re-reads until the turn ends.
_READ_TIMEOUT = 0.5

# A turn that produces nothing at all for this long is treated as lost. Hermes
# emits usage and tool events throughout real work, so silence this long means
# the peer went away without closing the connection - the failure mode of a
# dropped network link, which the local pipe never has.
_IDLE_LIMIT = 900.0

# Hermes advertises its permission surface as slash commands and config rather
# than per-turn flags, so BlindPilot's picker maps onto the closest Hermes
# behaviour: whether this session may act without asking.
_MODE_TO_YOLO = {
    "bypassPermissions": True,
    "auto": True,
    "dontAsk": True,
    "acceptEdits": False,
    "default": False,
    "plan": False,
}


def _first_text(*candidates: object) -> str:
    """The first candidate that is a non-empty string."""
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return ""


# Hermes' ``thinking.delta`` carries its terminal spinner - a kawaii face and a
# random verb ("(⌐■_■) contemplating...") drawn from agent/display.py - not the
# model's reasoning. It exists to show a sighted user that work is happening.
# Spoken aloud it is pure noise, and the real reasoning arrives separately in
# ``reasoning.available``, so the spinner is dropped rather than read out.
_SPINNER_VERBS = (
    "pondering",
    "contemplating",
    "musing",
    "cogitating",
    "ruminating",
    "deliberating",
    "mulling",
    "reflecting",
    "processing",
    "reasoning",
    "analyzing",
    "computing",
    "synthesizing",
    "formulating",
    "brainstorming",
)


def _is_spinner_text(text: str) -> bool:
    """Whether a thinking delta is Hermes' spinner rather than real reasoning.

    Matched on shape, not on an exact list of faces: a skin can replace the
    faces, but the trailing "verb..." is what the spinner always looks like,
    and real reasoning is prose that does not end that way.
    """
    stripped = text.strip()
    if not stripped:
        return True
    if not stripped.endswith("..."):
        return False
    tail = stripped[:-3].strip().rsplit(" ", 1)[-1].lower()
    return tail in _SPINNER_VERBS


# How much of a tool result becomes a row. A screen reader reads a row line by
# line, so an unbounded result is not merely untidy - it buries the answer.
_RESULT_MAX_LINES = 40
_RESULT_MAX_CHARS = 4000

# Keys Hermes' own tools use for the part of a result a person wants to hear,
# in the order they are worth trying.
_RESULT_TEXT_KEYS = ("output", "content", "text", "summary", "stdout", "message", "result")


def _describe_tool_result(result: object) -> str:
    """Render a tool result as something worth reading aloud.

    Most Hermes tools answer with a decoded object rather than a string, so
    the interesting part has to be picked out: the output of a command, the
    text of a file, the error that explains a failure. Dumping the raw JSON
    would technically be complete and practically unusable.
    """
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        parts: list[str] = []
        for key in _RESULT_TEXT_KEYS:
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
                break
        # An error and a non-zero exit are the whole point of the row when
        # they are present, so they are reported even alongside output.
        error = result.get("error")
        if isinstance(error, str) and error.strip():
            parts.append(f"error: {error.strip()}")
        exit_code = result.get("exit_code")
        if isinstance(exit_code, int) and exit_code != 0:
            parts.append(f"exit code {exit_code}")
        if parts:
            return "\n".join(parts)
        # Nothing recognisable: say what came back rather than staying silent,
        # because a tool that ran and reported nothing reads as a hang.
        keys = ", ".join(sorted(str(k) for k in result)[:8])
        return f"finished ({keys})" if keys else "finished"
    if isinstance(result, list):
        return f"{len(result)} {'item' if len(result) == 1 else 'items'}"
    if result is None:
        return ""
    return str(result).strip()


class HermesWorker(threading.Thread):
    """Run one Hermes turn, reporting it through BlindPilot's callbacks.

    The signature matches the other backends' workers so the window can hold
    whichever one the user picked without knowing which it is.
    """

    def __init__(
        self,
        prompt: str,
        session_id: Optional[str],
        cwd: str,
        permission_mode: str,
        *,
        model: str = "",
        effort: str = "",
        compact: bool = False,
        remote_url: str = "",
        remote_token: str = "",
        remote_credential: str = "token",
        on_session: Callable[[str], None],
        on_started: Callable[[], None],
        on_activity: Callable[[str, str], None],
        on_complete: Callable[[str], None],
        on_failed: Callable[[str], None],
        on_done: Callable[[], None],
    ) -> None:
        super().__init__(daemon=True)
        self._prompt = prompt
        self._session_id = session_id
        self._cwd = cwd
        self._permission_mode = permission_mode
        self._model = model
        # Hermes exposes no per-turn reasoning-effort control on this protocol,
        # so the value is accepted and ignored rather than silently changing
        # something else. BackendInfo says so too (supports_effort=False).
        self._effort = effort
        self._compact = compact
        self._remote_url = remote_url
        self._remote_token = remote_token
        # Which credential name the remote server expects: its session token,
        # or a ticket minted after a password login.
        self._remote_credential = remote_credential
        self._on_session = on_session
        self._on_started = on_started
        self._on_activity = on_activity
        self._on_complete = on_complete
        self._on_failed = on_failed
        self._on_done = on_done
        self._transport: Optional[Transport] = None
        self._cancelled = False
        self._accepting_input = threading.Event()
        self._request_id = 100
        self._gateway_session = session_id or ""
        # The per-process session id Hermes addresses turns with. Distinct from
        # the stored id above, which is what survives a restart.
        self._live_session = ""
        self._answer_parts: list[str] = []
        self._thinking_parts: list[str] = []
        # Hermes sends the turn's finished reasoning as one replacement event;
        # it wins over the streamed fragments when both arrive.
        self._reasoning = ""
        # Hermes reports a tool's name at start and again at completion; the
        # name is kept so a result row can say which tool produced it.
        self._tool_names: dict[str, str] = {}

    # -- public surface the window drives ---------------------------------

    def accepting_input(self) -> bool:
        return self._accepting_input.is_set() and not self._cancelled

    def steer(self, text: str) -> bool:
        """Push guidance into the turn that is already running."""
        if not self.accepting_input() or not self._gateway_session:
            return False
        return self._request("session.steer", {"session_id": self._gateway_session, "text": text})

    def cancel(self) -> None:
        self._cancelled = True
        self._accepting_input.clear()
        if self._gateway_session:
            # Ask Hermes to stop the turn before dropping the connection, so a
            # remote Hermes is not left working on an answer nobody will read.
            self._request("session.interrupt", {"session_id": self._gateway_session})
        transport = self._transport
        if transport is not None:
            transport.close()

    def run(self) -> None:
        try:
            self._do_run()
        finally:
            self._accepting_input.clear()
            transport = self._transport
            if transport is not None:
                transport.close()
            self._on_done()

    # -- protocol plumbing -------------------------------------------------

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _request(self, method: str, params: dict) -> bool:
        transport = self._transport
        if transport is None:
            return False
        return transport.send(
            {"jsonrpc": "2.0", "id": self._next_id(), "method": method, "params": params}
        )

    def _await_response(self, request_id: int, timeout: float) -> Optional[dict]:
        """Wait for one reply, handling the events that arrive alongside it."""
        waited = 0.0
        while waited < timeout and not self._cancelled:
            transport = self._transport
            if transport is None:
                return None
            frame = transport.receive(_READ_TIMEOUT)
            if frame is None:
                waited += _READ_TIMEOUT
                continue
            if frame.get("id") == request_id:
                return frame
            self._handle_event(frame)
        return None

    def _open_transport(self) -> bool:
        """Connect, local or remote. Reports its own failure and returns False."""
        if self._remote_url:
            transport = WebSocketTransport(
                self._remote_url, self._remote_token, self._remote_credential
            )
        else:
            if not hermes_installed():
                self._on_failed(
                    "Hermes Agent is not installed. See https://hermes-agent.nousresearch.com/docs"
                )
                return False
            transport = StdioTransport(self._cwd)
        try:
            transport.start()
        except OSError as exc:
            self._on_failed(str(exc))
            return False
        self._transport = transport
        return True

    def _do_run(self) -> None:
        if self._compact and not self._session_id:
            self._on_failed("There is no Hermes conversation to compact yet")
            return
        if not self._open_transport():
            return

        # Hermes announces itself before accepting work; waiting for that is
        # how a client knows the far end is a Hermes gateway at all, which on
        # the remote path is the difference between "wrong address" and "slow".
        if self._wait_for_ready() is False:
            return
        if not self._ensure_session():
            return
        if self._compact:
            self._run_compaction()
            return
        self._run_turn()

    def _wait_for_ready(self) -> bool:
        deadline = 60.0
        waited = 0.0
        while waited < deadline and not self._cancelled:
            transport = self._transport
            if transport is None:
                return False
            frame = transport.receive(_READ_TIMEOUT)
            if frame is None:
                waited += _READ_TIMEOUT
                continue
            if self._event_type(frame) == "gateway.ready":
                return True
            self._handle_event(frame)
        if not self._cancelled:
            detail = self._transport.failure_detail() if self._transport else ""
            self._on_failed(detail or "Hermes did not become ready in time")
        return False

    def _ensure_session(self) -> bool:
        """Create a conversation, or reopen the one we were given."""
        transport = self._transport
        if transport is None:
            return False
        request_id = self._next_id()
        if self._gateway_session:
            # Reopening a stored conversation. Hermes resolves the id through
            # its compression chain, so a conversation that was compacted since
            # it was last seen still lands on the turns written afterwards.
            # ``omit_messages`` keeps it from replaying the whole transcript:
            # BlindPilot already rebuilt those rows from history.
            transport.send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "session.resume",
                    "params": {
                        "session_id": self._gateway_session,
                        "omit_messages": True,
                    },
                }
            )
        else:
            transport.send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "session.create",
                    "params": {"cwd": self._cwd, **self._session_params()},
                }
            )
        reply = self._await_response(request_id, 120.0)
        if reply is None:
            if not self._cancelled:
                detail = self._transport.failure_detail() if self._transport else ""
                self._on_failed(detail or "Hermes did not answer the session request")
            return False
        error = reply.get("error")
        if error:
            self._on_failed(self._error_text(error, "Hermes could not start a session"))
            return False
        result = reply.get("result") or {}
        # Two ids come back: one for this gateway process and one that survives
        # restarts. The turn is addressed with the first; the second is what
        # BlindPilot stores so the conversation can be reopened later.
        self._live_session = str(result.get("session_id") or "")
        stored = str(result.get("stored_session_id") or "")
        if not self._live_session:
            self._on_failed("Hermes did not return a session id")
            return False
        self._on_session(stored or self._live_session)
        return True

    def _session_params(self) -> dict:
        params: dict = {}
        if self._model:
            params["model"] = self._model
        yolo = _MODE_TO_YOLO.get(self._permission_mode)
        if yolo is not None:
            params["yolo"] = yolo
        return params

    def _run_turn(self) -> None:
        request_id = self._next_id()
        transport = self._transport
        if transport is None:
            return
        transport.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "prompt.submit",
                "params": {"session_id": self._live_session, "text": self._prompt},
            }
        )
        reply = self._await_response(request_id, 120.0)
        if reply is None:
            if not self._cancelled:
                detail = transport.failure_detail()
                self._on_failed(detail or "Hermes did not accept the prompt")
            return
        error = reply.get("error")
        if error:
            self._on_failed(self._error_text(error, "Hermes could not start the turn"))
            return
        self._accepting_input.set()
        self._on_started()
        self._consume_turn()

    def _run_compaction(self) -> None:
        """Compact in place. Hermes answers when the summary is written."""
        request_id = self._next_id()
        transport = self._transport
        if transport is None:
            return
        transport.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "session.compress",
                "params": {"session_id": self._live_session},
            }
        )
        reply = self._await_response(request_id, 600.0)
        if reply is None:
            if not self._cancelled:
                self._on_failed("Hermes did not finish compacting the conversation")
            return
        error = reply.get("error")
        if error:
            self._on_failed(self._error_text(error, "Hermes could not compact"))
            return
        # Compaction produces no answer of its own, so say what happened rather
        # than finishing in silence - a silent end reads as a failed turn.
        self._on_complete("Conversation compacted.")

    def _consume_turn(self) -> None:
        """Read events until the answer is complete, failed, or interrupted."""
        idle = 0.0
        while not self._cancelled:
            transport = self._transport
            if transport is None:
                return
            frame = transport.receive(_READ_TIMEOUT)
            if frame is None:
                idle += _READ_TIMEOUT
                if idle >= _IDLE_LIMIT:
                    self._on_failed(transport.failure_detail())
                    return
                continue
            idle = 0.0
            if self._handle_event(frame) is True:
                return

    # -- events into accessible rows --------------------------------------

    @staticmethod
    def _event_type(frame: dict) -> str:
        params = frame.get("params")
        if isinstance(params, dict):
            return str(params.get("type") or "")
        return ""

    def _handle_event(self, frame: dict) -> Optional[bool]:
        """Turn one frame into rows. ``True`` means the turn is over.

        Anything unrecognised is ignored on purpose: Hermes gains events over
        time, and a front end that fails on an unknown one would break at the
        next Hermes release.
        """
        params = frame.get("params")
        if not isinstance(params, dict):
            return None
        event = str(params.get("type") or "")
        payload = params.get("payload")
        payload = payload if isinstance(payload, dict) else {}

        if event == "message.delta":
            text = str(payload.get("text") or "")
            if text:
                self._answer_parts.append(text)
            return None

        if event == "thinking.delta":
            text = str(payload.get("text") or "")
            # Only real reasoning is kept; the spinner is Hermes drawing a
            # progress indicator for a terminal nobody is looking at here.
            if text and not _is_spinner_text(text):
                self._thinking_parts.append(text)
            return None

        if event == "reasoning.available":
            # Where Hermes puts the model's actual reasoning for the turn. It
            # replaces whatever was streamed, rather than adding to it, so the
            # row is the reasoning once and not the reasoning twice.
            text = str(payload.get("text") or "").strip()
            if text:
                self._reasoning = text
            return None

        if event == "tool.start":
            self._tool_start(payload)
            return None

        if event == "tool.complete":
            self._tool_complete(payload)
            return None

        if event == "approval.request":
            # BlindPilot answers approvals through its permission picker rather
            # than a modal, so an approval that arrives in a mode which cannot
            # grant it is reported instead of silently blocking the turn.
            self._answer_approval(payload)
            return None

        if event in ("clarify.request", "sudo.request", "secret.request"):
            question = _first_text(
                payload.get("question"), payload.get("prompt"), payload.get("message")
            )
            self._on_activity(
                "tool", f"Hermes is asking: {question or 'a question needing the terminal'}"
            )
            return None

        if event == "message.complete":
            return self._turn_complete(payload)

        if event == "error":
            message = _first_text(payload.get("message"), payload.get("error"))
            if message:
                self._on_failed(message)
                return True
            return None

        return None

    def _tool_start(self, payload: dict) -> None:
        name = str(payload.get("name") or "tool")
        tool_id = str(payload.get("tool_id") or "")
        if tool_id:
            self._tool_names[tool_id] = name
        context = _first_text(payload.get("context"), payload.get("args_text"))
        self._on_activity("tool", f"{name}: {context}" if context else name)

    def _tool_complete(self, payload: dict) -> None:
        tool_id = str(payload.get("tool_id") or "")
        name = str(payload.get("name") or self._tool_names.pop(tool_id, "") or "tool")
        # Prefer Hermes' own one-line summary when it sends one: it is written
        # for a human. Otherwise fall back to the result itself, which arrives
        # as a decoded object for most tools rather than a string.
        detail = _first_text(
            payload.get("summary"),
            payload.get("inline_diff"),
            payload.get("result_text"),
        )
        if not detail:
            detail = _describe_tool_result(payload.get("result"))
        if not detail:
            return
        # A tool result can be thousands of lines, and a screen reader has to
        # walk every one of them. The row says how much was left out rather
        # than pretending the whole result is there.
        lines = detail.splitlines()
        if len(lines) > _RESULT_MAX_LINES:
            omitted = len(lines) - _RESULT_MAX_LINES
            detail = "\n".join(lines[:_RESULT_MAX_LINES])
            detail += f"\n[{omitted} more lines not shown]"
        elif len(detail) > _RESULT_MAX_CHARS:
            detail = detail[:_RESULT_MAX_CHARS] + " […truncated]"
        self._on_activity("result", f"{name}: {detail}")

    def _answer_approval(self, payload: dict) -> None:
        request_id = payload.get("request_id")
        allowed = _MODE_TO_YOLO.get(self._permission_mode, False)
        command = _first_text(payload.get("command"), payload.get("summary"))
        if allowed:
            self._on_activity("tool", f"Approved automatically: {command or 'a command'}")
            decision = "approve"
        else:
            self._on_activity(
                "tool",
                f"Hermes needs approval for: {command or 'a command'}. "
                "Switch the permission mode to allow it.",
            )
            decision = "deny"
        if request_id is not None:
            self._request("approval.respond", {"request_id": request_id, "decision": decision})

    def _turn_complete(self, payload: dict) -> Optional[bool]:
        self._accepting_input.clear()
        status = str(payload.get("status") or "complete")
        # The replacement event wins; the streamed fragments are the fallback
        # for a provider that never sends one.
        thinking = self._reasoning or "".join(self._thinking_parts).strip()
        answer = _first_text(payload.get("text")) or "".join(self._answer_parts)
        if thinking and thinking.strip() != answer.strip():
            # Reasoning is its own row so it can be skipped or read on demand,
            # the same way the other backends present it. When a provider
            # reports the answer as its reasoning - some do, for a one-word
            # reply - saying it twice would just make the reader repeat itself.
            self._on_activity("thinking", thinking)
        self._thinking_parts.clear()
        self._reasoning = ""
        if status in ("interrupted", "cancelled"):
            if not self._cancelled:
                self._on_failed("Hermes stopped before finishing the answer")
            return True
        if status in ("error", "failed"):
            self._on_failed(
                _first_text(payload.get("error"), payload.get("text")) or "Hermes turn failed"
            )
            return True
        self._on_complete(answer.strip())
        return True

    @staticmethod
    def _error_text(error: object, fallback: str) -> str:
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message
        return fallback
