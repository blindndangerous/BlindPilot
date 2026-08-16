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
import time
import urllib.parse
from pathlib import Path
from typing import Optional, Protocol, Sequence

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


def _text_output_kwargs() -> dict:
    """Decode a child's output as UTF-8, whatever the console code page is.

    Windows defaults to the legacy ANSI code page here, which raises
    UnicodeDecodeError the moment a child prints a byte outside it -- Hermes'
    own banner is enough to trigger it. Everything read from Hermes is UTF-8,
    so that is what gets asked for, and undecodable bytes are replaced rather
    than allowed to abort a check.
    """
    return {"text": True, "encoding": "utf-8", "errors": "replace"}


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
    return hermes_python() is not None or wsl_hermes_available()


# --------------------------------------------------------------------------
# Hermes inside WSL
#
# A very common arrangement on Windows: the desktop is a Windows program, but
# Hermes lives in a WSL distribution, where it was installed with the same
# one-line installer as on Linux. Nothing in Windows' PATH points at it, so
# without this it looks like Hermes is not installed at all -- which is what a
# Windows machine with a perfectly good Hermes reported before this existed.
#
# The gateway speaks over stdin and stdout, and "wsl.exe -e" connects those
# straight through, so the same protocol works with no translation. Paths are
# the one thing that does not carry across, which is why the working directory
# is converted below rather than passed as-is.
# --------------------------------------------------------------------------


# Cached because every backend check would otherwise start a WSL process, and
# the answer cannot change while the app is running.
_WSL_HERMES: Optional[str] = None
_WSL_CHECKED = False

# How long a history query inside WSL may take. The dialog runs this on the GUI
# thread, so it has to fail rather than freeze the window.
WSL_QUERY_TIMEOUT = 20.0


def wsl_exe() -> Optional[str]:
    """Windows' WSL launcher, or None when this is not Windows."""
    if platform.system() != "Windows":
        return None
    return shutil.which("wsl.exe") or shutil.which("wsl")


def wsl_hermes_path(timeout: int = 20) -> Optional[str]:
    """Hermes' path inside WSL, or None when there is no Hermes there."""
    global _WSL_HERMES, _WSL_CHECKED
    if _WSL_CHECKED:
        return _WSL_HERMES
    _WSL_CHECKED = True
    launcher = wsl_exe()
    if not launcher:
        return None
    try:
        proc = subprocess.run(
            [launcher, "-e", "sh", "-lc", "command -v hermes"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            **_text_output_kwargs(),
            **_no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    path = (proc.stdout or "").strip().splitlines()
    _WSL_HERMES = path[0].strip() if path and path[0].strip() else None
    return _WSL_HERMES


def wsl_hermes_available() -> bool:
    """Whether a Hermes in WSL can be driven from this Windows desktop."""
    return wsl_hermes_python() is not None


# Cached alongside the launcher: same reasoning, same lifetime.
_WSL_PYTHON: Optional[str] = None
_WSL_PYTHON_CHECKED = False
_WSL_STATE_DB: Optional[str] = None
_WSL_STATE_DB_CHECKED = False


def wsl_hermes_python(timeout: int = 25) -> Optional[str]:
    """The interpreter inside WSL that can import Hermes' gateway.

    Hermes' launcher has no subcommand for this protocol, so the gateway has to
    be started as a module -- which means finding the interpreter of the venv
    Hermes installed itself into. Its home is resolved inside WSL rather than
    guessed from Windows, so a distribution with HERMES_HOME set, or a home
    directory that is not /home/<user>, still works.
    """
    global _WSL_PYTHON, _WSL_PYTHON_CHECKED
    if _WSL_PYTHON_CHECKED:
        return _WSL_PYTHON
    _WSL_PYTHON_CHECKED = True
    launcher = wsl_exe()
    if not launcher:
        return None
    # Printed by WSL's own shell, so ${HERMES_HOME:-$HOME/.hermes} is expanded
    # there and the answer is a path that exists on that side.
    probe = (
        'root="${HERMES_HOME:-$HOME/.hermes}/' + HERMES_SOURCE_DIRNAME + '"; '
        'for p in "$root/venv/bin/python3" "$root/venv/bin/python"; do '
        'if [ -x "$p" ] && [ -d "$root/' + HERMES_GATEWAY_MODULE.split(".")[0] + '" ]; '
        'then printf %s "$p"; exit 0; fi; done; exit 1'
    )
    try:
        proc = subprocess.run(
            [launcher, "-e", "sh", "-lc", probe],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            **_text_output_kwargs(),
            **_no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    found = (proc.stdout or "").strip()
    _WSL_PYTHON = found or None
    return _WSL_PYTHON


def wsl_state_db() -> Optional[str]:
    """Hermes' session store inside WSL, as the POSIX path WSL sees.

    Both the path and Hermes' home are resolved inside WSL rather than guessed,
    so a distribution with HERMES_HOME set still works.
    """
    global _WSL_STATE_DB, _WSL_STATE_DB_CHECKED
    if _WSL_STATE_DB_CHECKED:
        return _WSL_STATE_DB
    _WSL_STATE_DB_CHECKED = True
    launcher = wsl_exe()
    if not launcher:
        return None
    try:
        proc = subprocess.run(
            [
                launcher,
                "-e",
                "sh",
                "-lc",
                'db="${HERMES_HOME:-$HOME/.hermes}/state.db"; [ -f "$db" ] && printf %s "$db"',
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=25,
            **_text_output_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    found = (proc.stdout or "").strip()
    _WSL_STATE_DB = found or None
    return _WSL_STATE_DB


def wsl_sqlite_query(sql: str, params: "Sequence" = ()) -> "list[dict]":
    """Run one read-only query against Hermes' store from inside WSL.

    The store is also reachable from Windows under \\\\wsl.localhost, but Hermes
    keeps it in WAL mode and WAL needs shared memory that a network share
    cannot provide: SQLite answers "database is locked" and no conversation is
    ever listed. Running the query on WSL's side avoids the problem instead of
    working around it.

    The query and its parameters are passed as JSON on stdin rather than
    interpolated into a shell command, so no value is ever quoted into code.
    Rows come back as JSON, which is why the caller gets plain dicts.
    """
    launcher = wsl_exe()
    store = wsl_state_db()
    if not launcher or not store:
        return []
    reader = (
        "import json,sqlite3,sys\n"
        "req=json.load(sys.stdin)\n"
        "db=sqlite3.connect('file:'+req['db']+'?mode=ro',uri=True,timeout=5.0)\n"
        "db.row_factory=sqlite3.Row\n"
        "try:\n"
        "    rows=[dict(r) for r in db.execute(req['sql'],tuple(req['params'])).fetchall()]\n"
        "finally:\n"
        "    db.close()\n"
        "json.dump(rows,sys.stdout,default=str)\n"
    )
    payload = json.dumps({"db": store, "sql": sql, "params": list(params)})
    try:
        proc = subprocess.run(
            [launcher, "-e", "python3", "-c", reader],
            input=payload,
            capture_output=True,
            timeout=WSL_QUERY_TIMEOUT,
            **_text_output_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    try:
        rows = json.loads(proc.stdout or "[]")
    except ValueError:
        return []
    return rows if isinstance(rows, list) else []


def windows_path_to_wsl(path: str) -> str:
    """Translate a Windows working directory into its WSL equivalent.

    Only the drive-letter form is translated, since that is what the folder
    picker produces. Anything already in POSIX form, or a network path with no
    WSL equivalent, is passed through for WSL itself to interpret.
    """
    text = str(path or "").strip()
    if len(text) >= 2 and text[1] == ":" and text[0].isalpha():
        drive = text[0].lower()
        # Collapse repeated separators: "D:\\projekty\\\\x" and a trailing slash
        # both otherwise produce a path with empty components in it.
        parts = [p for p in text[2:].replace("\\", "/").split("/") if p]
        return f"/mnt/{drive}/" + "/".join(parts) if parts else f"/mnt/{drive}"
    return text.replace("\\", "/")


def wsl_path_to_windows(path: str) -> str:
    """Translate a WSL path back into the Windows form, where one exists.

    Hermes records the working directory as it saw it, so a conversation run
    through WSL carries "/mnt/d/work". Reopening it in a Windows desktop needs
    the drive-letter form back. Paths inside the distribution's own filesystem
    have no drive-letter equivalent and are returned unchanged, for the caller
    to reject.
    """
    text = str(path or "").strip().replace("\\", "/")
    parts = [p for p in text.split("/") if p]
    if len(parts) >= 2 and parts[0].lower() == "mnt" and len(parts[1]) == 1:
        drive = parts[1].upper()
        rest = "\\".join(parts[2:])
        return f"{drive}:\\{rest}" if rest else f"{drive}:\\"
    return str(path or "")


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
        wsl_path = wsl_hermes_path()
        launcher = wsl_exe()
        if not wsl_path or not launcher:
            return False
        command = [launcher, "-e", wsl_path, "status"]
    else:
        command = [binary, "status"]
    try:
        proc = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            **_text_output_kwargs(),
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


# How long to wait for a remote handshake. Long enough for a sleepy machine on
# a private network, short enough that a wrong address is reported rather than
# leaving the user listening to silence.
REMOTE_CONNECT_TIMEOUT = 20.0

# Credential names Hermes' WebSocket upgrade accepts from a configured client.
# "token" is the server's session token; "ticket" is the one-shot ticket minted
# after a password login, which is what a server reachable from another machine
# requires. A third name exists for processes Hermes spawns itself and is
# deliberately not offered here.
REMOTE_CREDENTIALS = ("token", "ticket")


def remote_ws_url(host: str, port: int = 9119, secure: bool = False) -> str:
    """Build the gateway URL from the parts a user actually types.

    Asking for a whole ``ws://host:port/api/ws`` is asking to get the path
    wrong, so the settings take a host and a port and the path is added here.
    """
    host = host.strip()
    # Tolerate someone pasting a full URL anyway.
    for prefix in ("ws://", "wss://", "http://", "https://"):
        if host.lower().startswith(prefix):
            secure = prefix in ("wss://", "https://")
            host = host[len(prefix) :]
            break
    host = host.rstrip("/")
    if host.endswith("/api/ws"):
        host = host[: -len("/api/ws")].rstrip("/")
    if ":" in host and not host.startswith("["):
        host, _, given_port = host.rpartition(":")
        if given_port.isdigit():
            port = int(given_port)
    scheme = "wss" if secure else "ws"
    return f"{scheme}://{host}:{port}/api/ws"


def _authenticated_ws_url(url: str, token: str, credential: str) -> str:
    """Put the credential in the query string, where the upgrade looks for it."""
    if not token:
        return url
    name = credential if credential in REMOTE_CREDENTIALS else "token"
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{name}={urllib.parse.quote(token, safe='')}"


def _redacted_ws_url(url: str) -> str:
    """The address without any credential, safe to show or speak."""
    return url.split("?", 1)[0]


def _remote_failure_message(url: str, exc: Exception) -> str:
    """Say what to do about a failed connection, not just that it failed.

    A blind user cannot glance at a log, so the message has to carry the
    diagnosis: refused means nothing is listening, a rejected handshake means
    the key is wrong, a timeout means the machine is not answering.
    """
    detail = str(exc).strip() or exc.__class__.__name__
    lowered = detail.lower()
    if "403" in detail or "401" in detail or "handshake" in lowered:
        return (
            f"Hermes at {url} refused the connection key. Check the key in "
            "Remote Hermes settings, then try again."
        )
    if "refused" in lowered:
        return (
            f"Nothing is listening at {url}. Start Hermes there with "
            "'hermes serve', then try again."
        )
    if "name or service not known" in lowered or "getaddrinfo" in lowered:
        return f"The address {url} could not be found. Check the host name."
    if "timed out" in lowered or "timeout" in lowered:
        # A closed port on a reachable machine also reports a timeout rather
        # than a refusal, so name both causes instead of guessing one.
        return (
            f"{url} did not answer. Check that Hermes is running there with "
            "'hermes serve', and that the machine is awake and reachable."
        )
    return f"Could not reach Hermes at {url}: {detail}"


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
        env = os.environ.copy()
        if python and root is not None:
            # Hermes on this side of the machine: run its own interpreter.
            #
            # The gateway imports itself from the source tree, and Hermes' own
            # launcher clears these two before exec'ing, so mirror that: a
            # stale PYTHONHOME from the host application would break the child.
            env.pop("PYTHONHOME", None)
            existing = env.get("PYTHONPATH", "").strip()
            env["PYTHONPATH"] = f"{root}{os.pathsep}{existing}" if existing else str(root)
            env["HERMES_PYTHON_SRC_ROOT"] = str(root)
            command = [python, "-m", HERMES_GATEWAY_MODULE]
            cwd = self._cwd
        else:
            # Hermes inside WSL, driven from a Windows desktop. Its interpreter
            # is invisible to Windows, so the gateway is started with WSL's own
            # launcher instead. The module is asked for rather than a
            # subcommand, because Hermes has no CLI subcommand for this
            # protocol -- checked, rather than assumed.
            #
            # The working directory is translated and handed to WSL, not to
            # Popen, which only understands Windows paths.
            wsl_python = wsl_hermes_python()
            launcher = wsl_exe()
            if not wsl_python or not launcher:
                raise OSError("Hermes Agent is installed but its Python environment is missing")
            command = [
                launcher,
                "--cd",
                windows_path_to_wsl(self._cwd),
                "-e",
                wsl_python,
                "-m",
                HERMES_GATEWAY_MODULE,
            ]
            # Popen's cwd is a Windows path; WSL was already told where to run.
            cwd = None
        self._proc = subprocess.Popen(
            command,
            cwd=cwd,
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

    Authentication is a query parameter, not a header. Hermes' HTTP routes do
    accept ``X-Hermes-Session-Token``, but the WebSocket upgrade reads its
    credential from the URL and refuses the handshake without one - a header
    alone is answered with 403. Two credential names are meant for a client a
    person configures: ``token`` for the server's session token, and
    ``ticket`` for the one-shot ticket minted after a password login on a
    server reachable from outside this machine.
    """

    def __init__(self, url: str, token: str = "", credential: str = "token") -> None:
        self._url = _authenticated_ws_url(url, token, credential)
        # Kept credential-free for anything shown to the user: a token spoken
        # aloud by a screen reader is a token read out to the room.
        self._display_url = _redacted_ws_url(url)
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
        try:
            self._ws = create_connection(self._url, timeout=REMOTE_CONNECT_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - any failure is "cannot reach it"
            self._error = str(exc)
            raise OSError(_remote_failure_message(self._display_url, exc)) from exc

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


# --------------------------------------------------------------------------
# The model picker
# --------------------------------------------------------------------------

# Asking the gateway costs a Python start-up, so the picker is given a bounded
# wait rather than being allowed to hang the dialog open.
MODEL_QUERY_TIMEOUT = 60.0


def _model_rows(payload: dict) -> tuple[list[str], str]:
    """Flatten Hermes' provider catalog into picker rows.

    Hermes groups models under providers and can have the same model name in
    more than one of them, so each row is qualified with its provider - which
    is also the form ``/model`` accepts, meaning a picked row can be sent back
    unchanged.
    """
    models: list[str] = []
    current = ""
    providers = payload.get("providers")
    if not isinstance(providers, list):
        return models, current
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        slug = str(provider.get("slug") or "").strip()
        if not slug:
            continue
        # An unauthenticated provider is listed so the user can see it exists,
        # but picking one would fail at the first turn.
        if provider.get("authenticated") is False:
            continue
        entries = provider.get("models")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            name = str(entry).strip()
            if not name:
                continue
            qualified = f"{slug}:{name}"
            models.append(qualified)
            if provider.get("is_current") and not current:
                current = qualified
    return models, current


def hermes_model_options(
    cwd: Optional[str] = None,
) -> tuple[list[str], list[str], str, str, str]:
    """Ask Hermes which models it can run, for BlindPilot's model picker.

    Returns the same five-part shape the other backends' catalogs use:
    models, effort levels, current model, current effort, error. Hermes exposes
    no per-turn effort control on this protocol, so that list is always empty
    rather than offering a setting the turn would ignore.
    """
    if not hermes_installed():
        return [], [], "", "", "Hermes Agent was not found on this computer."

    transport = StdioTransport(cwd or str(Path.home()))
    try:
        transport.start()
    except OSError as exc:
        return [], [], "", "", str(exc)

    deadline = time.monotonic() + MODEL_QUERY_TIMEOUT
    try:
        ready = False
        while time.monotonic() < deadline and not ready:
            frame = transport.receive(0.5)
            if frame is None:
                continue
            params = frame.get("params")
            if isinstance(params, dict) and params.get("type") == "gateway.ready":
                ready = True
        if not ready:
            return [], [], "", "", "Hermes did not respond in time."

        transport.send({"jsonrpc": "2.0", "id": 1, "method": "model.options", "params": {}})
        while time.monotonic() < deadline:
            frame = transport.receive(0.5)
            if frame is None:
                continue
            if frame.get("id") != 1:
                continue
            error = frame.get("error")
            if isinstance(error, dict):
                message = error.get("message")
                return [], [], "", "", str(message or "Hermes could not list its models.")
            result = frame.get("result")
            if not isinstance(result, dict):
                return [], [], "", "", "Hermes returned no model list."
            models, current = _model_rows(result)
            if not models:
                return [], [], "", "", "Hermes reported no usable models."
            return models, [], current, "", ""
        return [], [], "", "", "Hermes did not answer the model request in time."
    finally:
        transport.close()
