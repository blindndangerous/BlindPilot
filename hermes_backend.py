"""Hermes Agent backend for BlindPilot.

Hermes Agent (https://github.com/NousResearch/hermes-agent) speaks several
protocols. This adapter uses its TUI gateway JSON-RPC, which Hermes documents
as the protocol for "custom hosts that want fine-grained control of sessions,
slash commands, approvals, and streaming events" — everything a screen-reader
front end needs, and the one Hermes protocol that runs over *both* a local
pipe and a network socket.

That last property is why this adapter uses the TUI gateway rather than
Hermes' ACP mode. ACP is stdio-only, so a remote Hermes would have needed a
second, unrelated protocol and a second worker. Here one worker drives both:

    local   -- ``python -m tui_gateway.entry`` over a pipe, nothing to configure
    remote  -- the same JSON-RPC over a WebSocket to ``hermes serve``

The wire format is identical on both paths — one JSON object per line, no
Content-Length framing — because Hermes' own TUI parses stdout lines and
WebSocket frames with the same call. So :class:`HermesWorker` is written
against a transport interface and never learns which one it got.

Copyright (c) 2026 doubletaponair and BlindPilot contributors.
Based on the original Claude Code Reader application by doubletaponair:
https://github.com/doubletaponair/claude-code-reader
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional, Protocol

# Hermes' launcher is a plain executable on PATH, but the gateway itself is a
# Python module inside Hermes' own virtual environment. A GUI cannot rely on
# either being importable, so both are located on disk instead.
HERMES_SOURCE_DIRNAME = "hermes-agent"
HERMES_GATEWAY_MODULE = "tui_gateway.entry"

# Hermes writes warnings to stderr that have nothing to do with the protocol:
# SQLite build advisories, optional-tool notices, MCP server descriptions.
# Treating those as failures would break sessions that are working fine, so
# stderr is only ever used to explain an exit that already happened.
STDERR_KEEP_LINES = 20


def _no_window_kwargs() -> dict:
    """``subprocess`` keyword arguments that keep children off the screen."""
    if platform.system() == "Windows":
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}


# --------------------------------------------------------------------------
# Locating Hermes
# --------------------------------------------------------------------------


def hermes_home() -> Path:
    """Where Hermes keeps its configuration, sessions, and installed source."""
    override = os.environ.get("HERMES_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".hermes"


def hermes_source_root() -> Optional[Path]:
    """The Hermes source tree, which is what ``-m tui_gateway.entry`` needs.

    Hermes installs itself under its home directory rather than into the
    interpreter running BlindPilot, so importing it is not an option: the
    gateway has to be launched as a subprocess out of that tree.
    """
    candidates = [hermes_home() / HERMES_SOURCE_DIRNAME]
    launcher = shutil.which("hermes")
    if launcher:
        # A packaged install ships the launcher next to the venv that holds the
        # source, so walk up from it before giving up.
        here = Path(launcher).resolve().parent
        candidates.extend([here.parent, here.parent.parent])
    for candidate in candidates:
        if (candidate / HERMES_GATEWAY_MODULE.split(".")[0]).is_dir():
            return candidate
    return None


def hermes_python() -> Optional[str]:
    """The interpreter that can import Hermes, i.e. the one in its venv."""
    root = hermes_source_root()
    if root is None:
        return None
    names = ("python.exe", "python") if platform.system() == "Windows" else ("python3", "python")
    for directory in ("Scripts", "bin"):
        for name in names:
            candidate = root / "venv" / directory / name
            if candidate.is_file():
                return str(candidate)
    return None


def find_hermes_cli() -> Optional[str]:
    """Hermes' own launcher, used for version and authentication checks."""
    found = shutil.which("hermes")
    if found:
        return found
    home = Path.home()
    if platform.system() == "Windows":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        candidates = [
            home / ".local" / "bin" / "hermes.exe",
            home / ".local" / "bin" / "hermes",
            local / "Programs" / "hermes" / "hermes.exe",
        ]
    else:
        candidates = [
            home / ".local" / "bin" / "hermes",
            Path("/usr/local/bin/hermes"),
            Path("/opt/homebrew/bin/hermes"),
        ]
    candidates.append(hermes_home() / HERMES_SOURCE_DIRNAME / "venv" / "bin" / "hermes")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def hermes_installed() -> bool:
    """Whether a local Hermes can actually be driven, not merely found.

    The launcher alone is not enough: the gateway is a module in Hermes' venv,
    so a copy with a launcher but no importable source cannot run a session.
    """
    return hermes_python() is not None


def hermes_auth_ok(timeout: int = 25) -> bool:
    """Best-effort, non-interactive check that Hermes has a model configured.

    Hermes has no ``auth status`` command, and ``hermes model`` is the
    interactive picker rather than a report, so neither can answer this. What
    makes Hermes usable is a configured provider and model, and ``hermes
    status`` prints both without prompting for anything. Its exit code is 0
    even when nothing is set up, so the output is what gets inspected.
    """
    binary = find_hermes_cli()
    if not binary:
        return False
    try:
        proc = subprocess.run(
            [binary, "status"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            **_no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    for line in (proc.stdout or "").splitlines():
        # "Model:" followed by anything real means a provider is selected. The
        # line is decorated for the terminal, so only its shape is relied on.
        if "Model:" in line:
            value = line.split("Model:", 1)[1].strip()
            if value and value.lower() not in ("(not set)", "not set", "none", "-"):
                return True
    return False


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------


class Transport(Protocol):
    """One JSON-RPC connection to Hermes, local or remote.

    Both transports carry the same protocol, so the worker is written against
    this interface and never branches on which one it was handed.
    """

    def send(self, message: dict) -> bool:
        """Write one frame. ``False`` means the peer is gone."""
        ...

    def receive(self, timeout: float) -> Optional[dict]:
        """Read one frame, or ``None`` when nothing arrived in ``timeout``."""
        ...

    def close(self) -> None:
        """Release the connection."""
        ...

    def failure_detail(self) -> str:
        """Why the connection ended, for a message a user can act on."""
        ...


class StdioTransport:
    """Hermes' gateway as a child process, spoken to over its pipes.

    This is the zero-configuration path: no address, no port, no key. The
    frames are newline-delimited JSON, which is why stdout is read by line.
    """

    def __init__(self, cwd: str) -> None:
        self._cwd = cwd
        self._proc: Optional[subprocess.Popen] = None
        self._frames: "list[dict]" = []
        self._frames_lock = threading.Lock()
        self._frames_ready = threading.Condition(self._frames_lock)
        self._stderr: list[str] = []
        self._closed = False

    def start(self) -> None:
        """Launch the gateway. Raises ``OSError`` if it cannot be started."""
        python = hermes_python()
        root = hermes_source_root()
        if not python or root is None:
            raise OSError("Hermes Agent is installed but its Python environment is missing")
        env = os.environ.copy()
        # The gateway imports itself from the source tree, and Hermes' own
        # launcher clears these two before exec'ing, so mirror that: a stale
        # PYTHONHOME from the host application would break the child.
        env.pop("PYTHONHOME", None)
        existing = env.get("PYTHONPATH", "").strip()
        env["PYTHONPATH"] = f"{root}{os.pathsep}{existing}" if existing else str(root)
        env["HERMES_PYTHON_SRC_ROOT"] = str(root)
        self._proc = subprocess.Popen(
            [python, "-m", HERMES_GATEWAY_MODULE],
            cwd=self._cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
            env=env,
            **_no_window_kwargs(),
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except ValueError:
                # Not protocol. Hermes keeps stdout clean, so this is either a
                # library printing over it or a truncated line; either way the
                # remedy is the same as for stderr - remember it for the
                # failure message and carry on.
                self._stderr.append(line[:400])
                continue
            with self._frames_ready:
                self._frames.append(frame)
                self._frames_ready.notify_all()
        with self._frames_ready:
            self._closed = True
            self._frames_ready.notify_all()

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            line = line.strip()
            if line:
                self._stderr.append(line)
                del self._stderr[:-STDERR_KEEP_LINES]

    def send(self, message: dict) -> bool:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return False
        try:
            proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            return False
        return True

    def receive(self, timeout: float) -> Optional[dict]:
        with self._frames_ready:
            if not self._frames and not self._closed:
                self._frames_ready.wait(timeout)
            if self._frames:
                return self._frames.pop(0)
        return None

    def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                except OSError:
                    pass

    def failure_detail(self) -> str:
        detail = "\n".join(self._stderr[-6:]).strip()
        return detail or "Hermes closed the connection before the turn completed"


class WebSocketTransport:
    """A Hermes running elsewhere, reached over ``hermes serve``.

    The point of this path is a Hermes that lives on another machine — a
    home server, or a WSL instance reached over a private network — so the
    desktop can drive it without a terminal. The protocol is the same as the
    local path; only the pipe changes.
    """

    def __init__(self, url: str, token: str = "") -> None:
        self._url = url
        self._token = token
        self._ws = None
        self._error = ""

    def start(self) -> None:
        """Connect. Raises ``OSError`` with a message worth showing a user."""
        try:
            from websocket import create_connection  # type: ignore
        except ImportError as exc:  # pragma: no cover - dependency probe
            raise OSError(
                "Remote Hermes needs the websocket-client package: pip install websocket-client"
            ) from exc
        headers = []
        if self._token:
            headers.append(f"X-Hermes-Session-Token: {self._token}")
        try:
            self._ws = create_connection(self._url, header=headers, timeout=20)
        except Exception as exc:  # noqa: BLE001 - any failure is "cannot reach it"
            self._error = str(exc)
            raise OSError(f"Could not reach Hermes at {self._url}: {exc}") from exc

    def send(self, message: dict) -> bool:
        if self._ws is None:
            return False
        try:
            self._ws.send(json.dumps(message, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            self._error = str(exc)
            return False
        return True

    def receive(self, timeout: float) -> Optional[dict]:
        if self._ws is None:
            return None
        try:
            self._ws.settimeout(timeout)
            raw = self._ws.recv()
        except Exception as exc:  # noqa: BLE001 - timeout included
            name = type(exc).__name__
            if "timeout" not in name.lower():
                self._error = str(exc)
            return None
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001
                pass

    def failure_detail(self) -> str:
        if self._error:
            return f"Lost the connection to Hermes at {self._url}: {self._error}"
        return f"Hermes at {self._url} closed the connection before the turn completed"
