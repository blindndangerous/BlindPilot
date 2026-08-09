"""Backend adapters for BlindPilot.

BlindPilot began as Claude Code Reader. Claude's adapter remains in
``blindpilot_app.py``; this module contains the
provider-neutral discovery helpers plus Codex and FreeBuff workers.

Copyright (c) 2026 doubletaponair and BlindPilot contributors.
Based on the original Claude Code Reader application by doubletaponair:
https://github.com/doubletaponair/claude-code-reader
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import json
import os
import platform
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


BACKEND_CLAUDE = "claude"
BACKEND_CODEX = "codex"
BACKEND_FREEBUFF = "freebuff"
BACKEND_IDS = (BACKEND_CLAUDE, BACKEND_CODEX, BACKEND_FREEBUFF)
BACKEND_LABELS = {
    BACKEND_CLAUDE: "Claude Code",
    BACKEND_CODEX: "Codex",
    BACKEND_FREEBUFF: "FreeBuff",
}

# FreeBuff has no model-list or model-selection CLI flags. Its installed
# package and downloaded executable do contain the live picker catalog, so the
# adapter discovers that catalog at runtime and writes the same setting the
# picker uses. Pro remains the preferred default when FreeBuff still offers it.
FREEBUFF_PREFERRED_MODEL = "deepseek/deepseek-v4-pro"
_FREEBUFF_SETTINGS_LOCK = threading.Lock()
_freebuff_catalog_cache: tuple[tuple[str, float, int], list[str]] | None = None


@dataclass(frozen=True)
class BackendInfo:
    id: str
    label: str
    executable: str
    install_command: str
    login_args: tuple[str, ...]
    supports_model: bool
    supports_effort: bool
    supports_permissions: bool
    supports_steering: bool


BACKENDS = {
    BACKEND_CLAUDE: BackendInfo(
        BACKEND_CLAUDE,
        "Claude Code",
        "claude",
        "See https://claude.com/claude-code",
        ("/login",),
        True,
        True,
        True,
        True,
    ),
    BACKEND_CODEX: BackendInfo(
        BACKEND_CODEX,
        "Codex",
        "codex",
        "npm install -g @openai/codex",
        ("login",),
        True,
        True,
        True,
        True,
    ),
    BACKEND_FREEBUFF: BackendInfo(
        BACKEND_FREEBUFF,
        "FreeBuff",
        "freebuff",
        "npm install -g freebuff",
        ("login",),
        True,
        False,
        False,
        True,
    ),
}


def normalize_backend(value: object) -> str:
    """Return a supported backend id, defaulting to Claude Code."""
    if not isinstance(value, str):
        return BACKEND_CLAUDE
    compact = re.sub(r"[\s_-]+", "", value).casefold()
    aliases = {
        "claude": BACKEND_CLAUDE,
        "claudecode": BACKEND_CLAUDE,
        "codex": BACKEND_CODEX,
        "freebuff": BACKEND_FREEBUFF,
    }
    return aliases.get(compact, BACKEND_CLAUDE)


def backend_label(backend: str) -> str:
    return BACKEND_LABELS[normalize_backend(backend)]


def _fallback_cli_paths(name: str) -> tuple[Path, ...]:
    home = Path.home()
    if platform.system() == "Windows":
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        candidates: list[Path] = []
        for suffix in (".exe", ".cmd", ".ps1", ""):
            filename = name + suffix
            candidates.extend(
                [
                    appdata / "npm" / filename,
                    home / ".local" / "bin" / filename,
                    home / ".volta" / "bin" / filename,
                    local / "Microsoft" / "WinGet" / "Links" / filename,
                    local / "Programs" / name / filename,
                ]
            )
        return tuple(candidates)
    return (
        home / ".local" / "bin" / name,
        Path("/opt/homebrew/bin") / name,
        Path("/usr/local/bin") / name,
        home / ".npm-global" / "bin" / name,
        home / ".volta" / "bin" / name,
    )


def find_backend_cli(backend: str) -> Optional[str]:
    """Find a provider CLI even in the restricted PATH inherited by a GUI."""
    info = BACKENDS[normalize_backend(backend)]
    found = shutil.which(info.executable)
    if found:
        return found
    for candidate in _fallback_cli_paths(info.executable):
        if candidate.is_file():
            return str(candidate)
    if platform.system() != "Windows":
        shell = os.environ.get("SHELL")
        if shell and os.path.isfile(shell):
            try:
                proc = subprocess.run(
                    [shell, "-l", "-c", f"command -v {info.executable}"],
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
                result = proc.stdout.strip().splitlines()
                if proc.returncode == 0 and result and os.path.isfile(result[0]):
                    return result[0]
            except (OSError, subprocess.TimeoutExpired):
                pass
    return None


def backend_auth_ok(backend: str, timeout: int = 12) -> bool:
    """Best-effort non-interactive authentication check."""
    backend = normalize_backend(backend)
    binary = find_backend_cli(backend)
    if not binary:
        return False
    try:
        if backend == BACKEND_CLAUDE:
            proc = subprocess.run(
                [binary, "auth", "status"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
            )
            return proc.returncode == 0
        if backend == BACKEND_CODEX:
            proc = subprocess.run(
                [binary, "login", "status"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
            )
            return proc.returncode == 0
        if backend == BACKEND_FREEBUFF:
            credential = Path.home() / ".config" / "manicode" / "credentials.json"
            return credential.is_file() and credential.stat().st_size > 2
    except (OSError, subprocess.TimeoutExpired):
        return False
    return True


def _subprocess_env(binary: str) -> dict[str, str]:
    env = os.environ.copy()
    directory = os.path.dirname(binary)
    if directory and directory not in env.get("PATH", "").split(os.pathsep):
        env["PATH"] = directory + os.pathsep + env.get("PATH", "")
    return env


def _codex_app_server_binary(binary: str) -> str:
    """Prefer Codex's native executable over an npm wrapper on Windows.

    Terminating an npm ``.cmd`` launcher does not terminate its native child,
    which leaves the app-server writer lock attached to the thread. Launching
    the packaged executable directly gives BlindPilot reliable ownership and
    makes immediate session continuation possible.
    """
    if platform.system() != "Windows" or Path(binary).suffix.casefold() == ".exe":
        return binary
    package_root = Path(binary).parent / "node_modules" / "@openai" / "codex"
    try:
        candidates = sorted(
            package_root.glob("node_modules/@openai/codex-win32-*/vendor/**/bin/codex.exe")
        )
    except OSError:
        candidates = []
    return str(candidates[0]) if candidates else binary


def codex_model_options(
    cwd: Optional[str] = None,
) -> tuple[list[str], list[str], str, str, str]:
    """Read Codex's installed model catalog and configured defaults."""
    binary = find_backend_cli(BACKEND_CODEX)
    efforts: list[str] = []
    if not binary:
        return [], efforts, "", "", "Codex was not found."
    try:
        result = subprocess.run(
            [binary, "debug", "models", "--bundled"],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=_subprocess_env(binary),
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return [], efforts, "", "", "Could not read the model list from Codex."

    entries = payload if isinstance(payload, list) else payload.get("models", [])
    models: list[str] = []
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            model = entry.get("slug") or entry.get("id") or entry.get("model")
            if isinstance(model, str) and model and model not in models:
                models.append(model)
            supported = entry.get("supported_reasoning_levels") or []
            if isinstance(supported, list):
                for level in supported:
                    effort = level.get("effort") if isinstance(level, dict) else level
                    if isinstance(effort, str) and effort and effort not in efforts:
                        efforts.append(effort)

    if not efforts:
        efforts = ["low", "medium", "high", "xhigh"]

    current_model = ""
    current_effort = ""
    config_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    try:
        import tomllib

        with open(config_home / "config.toml", "rb") as handle:
            config = tomllib.load(handle)
        if isinstance(config.get("model"), str):
            current_model = config["model"]
        if isinstance(config.get("model_reasoning_effort"), str):
            current_effort = config["model_reasoning_effort"]
    except (OSError, ValueError):
        pass
    error = "" if models else "Codex returned an empty model catalog."
    return models, efforts, current_model, current_effort, error


def _freebuff_package_readme(binary: Optional[str]) -> Optional[Path]:
    if not binary:
        return None
    wrapper = Path(binary)
    candidates = [
        wrapper.parent / "node_modules" / "freebuff" / "README.md",
        wrapper.parent.parent / "lib" / "node_modules" / "freebuff" / "README.md",
    ]
    try:
        resolved = wrapper.resolve()
        candidates.extend([resolved.parent / "README.md", resolved.parent.parent / "README.md"])
    except OSError:
        pass
    return next((path for path in candidates if path.is_file()), None)


def _resolve_minified_string(
    source: str, expression: str, direct: Optional[dict[str, str]] = None
) -> str:
    expression = expression.strip()
    if len(expression) >= 2 and expression[0] == expression[-1] == '"':
        return expression[1:-1]
    if direct is None:
        direct = dict(re.findall(r'(?<![\w$])([A-Za-z_$][\w$]*)="([^"]+)"', source))
    if expression in direct:
        return direct[expression]
    if "." in expression:
        owner, member = expression.split(".", 1)
        marker = f"{owner}={{"
        owner_start = source.find(marker)
        if owner_start >= 0:
            body_start = owner_start + len(marker)
            body = source[body_start : body_start + 3000].split("}", 1)[0]
            value = re.search(rf'(?:^|,){re.escape(member)}:"([^"]+)"', body)
            if value:
                return value.group(1)
    alias = re.search(
        rf"(?<![\w$]){re.escape(expression)}=([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?)",
        source,
    )
    if alias and alias.group(1) != expression:
        return _resolve_minified_string(source, alias.group(1), direct)
    return ""


def _freebuff_models_from_install(binary: Optional[str]) -> list[str]:
    """Read the current picker catalog from the installed FreeBuff release."""
    executable = (
        Path.home()
        / ".config"
        / "manicode"
        / ("freebuff.exe" if platform.system() == "Windows" else "freebuff")
    )
    readme = _freebuff_package_readme(binary)
    try:
        stat = executable.stat()
        stamp = (str(executable), stat.st_mtime, stat.st_size)
    except OSError:
        return []

    global _freebuff_catalog_cache
    if _freebuff_catalog_cache and _freebuff_catalog_cache[0] == stamp:
        return list(_freebuff_catalog_cache[1])

    try:
        source = executable.read_bytes().decode("latin-1", errors="ignore")
        readme_text = readme.read_text(encoding="utf-8") if readme else ""
    except OSError:
        return []

    pattern = re.compile(
        r'([A-Za-z_$][\w$]*)=\{id:([^,}]+),displayName:"([^"]+)"'
        r'[^{}]{0,1000}?availability:"always"'
    )
    matches = list(pattern.finditer(source))
    if not matches:
        return []
    start = max(0, min(match.start() for match in matches) - 50_000)
    end = min(len(source), max(match.end() for match in matches) + 10_000)
    catalog_source = source[start:end]
    direct = dict(re.findall(r'(?<![\w$])([A-Za-z_$][\w$]*)="([^"]+)"', catalog_source))
    discovered: list[tuple[int, str]] = []
    for match in matches:
        display_name = match.group(3)
        # The packaged README documents the user-facing regular/earned models.
        # If it is absent, the catalog-shaped object is still better than a
        # stale list compiled into BlindPilot.
        if readme_text and display_name not in readme_text:
            continue
        model_id = _resolve_minified_string(source, match.group(2), direct)
        if not model_id or "/" not in model_id:
            continue
        order = readme_text.find(display_name) if readme_text else match.start()
        discovered.append((order if order >= 0 else match.start(), model_id))

    models: list[str] = []
    for _order, model_id in sorted(discovered):
        if model_id not in models:
            models.append(model_id)
    if FREEBUFF_PREFERRED_MODEL in models:
        models.remove(FREEBUFF_PREFERRED_MODEL)
        models.insert(0, FREEBUFF_PREFERRED_MODEL)
    _freebuff_catalog_cache = (stamp, list(models))
    return models


def freebuff_model_options() -> tuple[list[str], list[str], str, str, str]:
    """Return the model choices exposed by the installed FreeBuff release."""
    binary = find_backend_cli(BACKEND_FREEBUFF)
    models = _freebuff_models_from_install(binary)
    settings = Path.home() / ".config" / "manicode" / "settings.json"
    saved = ""
    try:
        payload = json.loads(settings.read_text(encoding="utf-8"))
        selected = payload.get("freebuffModel") if isinstance(payload, dict) else None
        if isinstance(selected, str) and selected.strip():
            saved = selected.strip()
            if saved not in models:
                models.append(saved)
    except (OSError, ValueError):
        pass
    if not models:
        models = [FREEBUFF_PREFERRED_MODEL]
        if saved and saved not in models:
            models.append(saved)
        error = "Could not refresh FreeBuff's model catalog; showing the preferred default."
    else:
        error = ""
    current = (
        saved
        if saved in models
        else FREEBUFF_PREFERRED_MODEL
        if FREEBUFF_PREFERRED_MODEL in models
        else models[0]
    )
    return models, [], current, "", error


def invalidate_backend_cache(backend: str | None = None) -> None:
    """Drop version-derived provider data before an explicit runtime refresh."""
    global _freebuff_catalog_cache
    if backend is None or normalize_backend(backend) == BACKEND_FREEBUFF:
        _freebuff_catalog_cache = None


def set_freebuff_model(model: str) -> None:
    """Persist the model using FreeBuff's own settings format."""
    selected = model.strip()
    if not selected:
        models, _efforts, current, _effort, _error = freebuff_model_options()
        selected = current or (models[0] if models else FREEBUFF_PREFERRED_MODEL)
    settings = Path.home() / ".config" / "manicode" / "settings.json"
    with _FREEBUFF_SETTINGS_LOCK:
        try:
            payload = json.loads(settings.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if payload.get("freebuffModel") == selected:
            return
        payload["freebuffModel"] = selected
        settings.parent.mkdir(parents=True, exist_ok=True)
        temporary = settings.with_suffix(".json.blindpilot.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, settings)


class CodexWorker(threading.Thread):
    """Run one Codex turn through the official app-server JSONL protocol."""

    def __init__(
        self,
        prompt: str,
        session_id: Optional[str],
        cwd: str,
        permission_mode: str,
        *,
        model: str = "",
        effort: str = "",
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
        self._effort = effort
        self._on_session = on_session
        self._on_started = on_started
        self._on_activity = on_activity
        self._on_complete = on_complete
        self._on_failed = on_failed
        self._on_done = on_done
        self._proc: Optional[subprocess.Popen[str]] = None
        self._cancelled = False
        self._write_lock = threading.Lock()
        self._accepting_input = threading.Event()
        self._thread_id = session_id or ""
        self._turn_id = ""
        self._request_id = 10
        self._assistant_parts: list[str] = []
        self._assistant_delta_seen: set[str] = set()
        self._assistant_streams: dict[str, list[str]] = {}
        self._reasoning_streams: dict[str, list[str]] = {}
        self._tool_outputs: dict[str, list[str]] = {}
        self._stderr: list[str] = []

    def accepting_input(self) -> bool:
        return self._accepting_input.is_set() and not self._cancelled

    def _send(self, message: dict) -> bool:
        proc = self._proc
        if not proc or not proc.stdin:
            return False
        try:
            data = json.dumps(message, ensure_ascii=False) + "\n"
            with self._write_lock:
                proc.stdin.write(data)
                proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def steer(self, text: str) -> bool:
        if not self.accepting_input() or not self._thread_id or not self._turn_id:
            return False
        return self._send(
            {
                "method": "turn/steer",
                "id": self._next_id(),
                "params": {
                    "threadId": self._thread_id,
                    "input": [{"type": "text", "text": text}],
                    "expectedTurnId": self._turn_id,
                },
            }
        )

    def cancel(self) -> None:
        self._cancelled = True
        self._accepting_input.clear()
        if self._thread_id and self._turn_id:
            self._send(
                {
                    "method": "turn/interrupt",
                    "id": self._next_id(),
                    "params": {"threadId": self._thread_id, "turnId": self._turn_id},
                }
            )
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def run(self) -> None:
        try:
            self._do_run()
        finally:
            self._accepting_input.clear()
            proc = self._proc
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        proc.kill()
                    except OSError:
                        pass
            self._on_done()

    @staticmethod
    def _policy(mode: str) -> tuple[str, dict]:
        if mode == "bypassPermissions":
            return "never", {"type": "dangerFullAccess"}
        if mode == "plan":
            return "never", {"type": "readOnly", "networkAccess": False}
        if mode in ("auto", "dontAsk"):
            return "never", {"type": "workspaceWrite", "networkAccess": True}
        return "on-request", {"type": "workspaceWrite", "networkAccess": True}

    def _do_run(self) -> None:
        binary = find_backend_cli(BACKEND_CODEX)
        if not binary:
            self._on_failed("Codex is not installed. Run: npm install -g @openai/codex")
            return
        try:
            server_binary = _codex_app_server_binary(binary)
            self._proc = subprocess.Popen(
                [server_binary, "app-server", "--stdio"],
                cwd=self._cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                env=_subprocess_env(server_binary),
            )
        except OSError as exc:
            self._on_failed(f"Failed to launch Codex: {exc}")
            return

        if self._proc.stderr:
            threading.Thread(target=self._read_stderr, daemon=True).start()

        init_id = self._next_id()
        self._send(
            {
                "method": "initialize",
                "id": init_id,
                "params": {
                    "clientInfo": {
                        "name": "blindpilot",
                        "title": "BlindPilot",
                        "version": "0.3.0",
                    }
                },
            }
        )
        self._send({"method": "initialized", "params": {}})

        thread_request = self._next_id()
        if self._session_id:
            request = {
                "method": "thread/resume",
                "id": thread_request,
                "params": {"threadId": self._session_id},
            }
        else:
            approval, sandbox = self._policy(self._permission_mode)
            params: dict = {
                "cwd": self._cwd,
                "approvalPolicy": approval,
                "sandbox": {
                    "readOnly": "read-only",
                    "workspaceWrite": "workspace-write",
                    "dangerFullAccess": "danger-full-access",
                }.get(sandbox.get("type"), "workspace-write"),
                "serviceName": "blindpilot",
            }
            if self._model:
                params["model"] = self._model
            request = {"method": "thread/start", "id": thread_request, "params": params}
        self._send(request)

        turn_request = 0
        started_notified = False
        assert self._proc.stdout is not None
        for raw in self._proc.stdout:
            if self._cancelled:
                return
            try:
                message = json.loads(raw)
            except ValueError:
                continue

            if message.get("id") == thread_request:
                error = message.get("error")
                if error:
                    self._on_failed(self._error_text(error, "Could not start a Codex session"))
                    return
                thread = (message.get("result") or {}).get("thread") or {}
                self._thread_id = str(thread.get("id") or self._session_id or "")
                if not self._thread_id:
                    self._on_failed("Codex did not return a session id")
                    return
                self._on_session(self._thread_id)
                approval, sandbox = self._policy(self._permission_mode)
                params = {
                    "threadId": self._thread_id,
                    "input": [{"type": "text", "text": self._prompt}],
                    "cwd": self._cwd,
                    "approvalPolicy": approval,
                    "sandboxPolicy": sandbox,
                }
                if self._model:
                    params["model"] = self._model
                if self._effort:
                    params["effort"] = self._effort
                turn_request = self._next_id()
                self._send({"method": "turn/start", "id": turn_request, "params": params})
                continue

            if turn_request and message.get("id") == turn_request:
                error = message.get("error")
                if error:
                    self._on_failed(self._error_text(error, "Codex could not start the turn"))
                    return
                turn = (message.get("result") or {}).get("turn") or {}
                self._turn_id = str(turn.get("id") or "")
                self._accepting_input.set()
                if not started_notified:
                    self._on_started()
                    started_notified = True
                continue

            if "method" in message and "id" in message:
                self._handle_server_request(message)
                continue

            method = message.get("method")
            params = message.get("params") or {}
            if method == "turn/started":
                turn = params.get("turn") or {}
                self._turn_id = str(turn.get("id") or self._turn_id)
                self._accepting_input.set()
                if not started_notified:
                    self._on_started()
                    started_notified = True
            elif method == "item/started":
                self._item_started(params.get("item") or {})
            elif method == "item/completed":
                self._item_completed(params.get("item") or {})
            elif method == "item/agentMessage/delta":
                item_id = str(params.get("itemId") or "")
                delta = str(params.get("delta") or "")
                if delta:
                    self._assistant_delta_seen.add(item_id)
                    self._assistant_streams.setdefault(item_id, []).append(delta)
                    self._assistant_parts.append(delta)
            elif method in (
                "item/reasoning/summaryTextDelta",
                "item/reasoning/textDelta",
                "item/plan/delta",
            ):
                delta = str(params.get("delta") or "")
                if delta:
                    item_id = str(params.get("itemId") or "")
                    self._reasoning_streams.setdefault(item_id, []).append(delta)
            elif method == "item/commandExecution/outputDelta":
                item_id = str(params.get("itemId") or "")
                delta = str(params.get("delta") or "")
                if delta:
                    self._tool_outputs.setdefault(item_id, []).append(delta)
            elif method in ("warning", "configWarning"):
                warning = str(params.get("message") or params.get("summary") or "")
                if warning:
                    self._on_activity("tool", f"Codex warning: {warning}")
            elif method == "turn/completed":
                self._accepting_input.clear()
                turn = params.get("turn") or {}
                status = turn.get("status")
                if status == "failed":
                    self._on_failed(self._error_text(turn.get("error"), "Codex turn failed"))
                elif status == "interrupted":
                    if not self._cancelled:
                        self._on_failed("Codex turn was interrupted")
                else:
                    self._on_complete("".join(self._assistant_parts).strip())
                return

        if not self._cancelled:
            detail = "\n".join(self._stderr[-10:]).strip()
            self._on_failed(detail or "Codex app server closed before the turn completed")

    def _read_stderr(self) -> None:
        if not self._proc or not self._proc.stderr:
            return
        for line in self._proc.stderr:
            if line.strip():
                self._stderr.append(line.strip())

    @staticmethod
    def _error_text(error: object, fallback: str) -> str:
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message
        return fallback

    def _handle_server_request(self, message: dict) -> None:
        method = str(message.get("method") or "")
        request_id = message.get("id")
        mode = self._permission_mode
        if method == "item/commandExecution/requestApproval":
            decision = "accept" if mode in ("auto", "bypassPermissions") else "decline"
            self._send({"id": request_id, "result": {"decision": decision}})
        elif method == "item/fileChange/requestApproval":
            decision = (
                "accept" if mode in ("acceptEdits", "auto", "bypassPermissions") else "decline"
            )
            self._send({"id": request_id, "result": {"decision": decision}})
        else:
            self._send(
                {
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": "BlindPilot cannot handle this request yet",
                    },
                }
            )

    def _item_started(self, item: dict) -> None:
        kind = item.get("type")
        if kind == "commandExecution":
            command = item.get("command")
            if isinstance(command, list):
                command = " ".join(map(str, command))
            self._on_activity("tool", f"Running: {command or 'command'}")
        elif kind == "fileChange":
            changes = item.get("changes") or []
            paths = [str(c.get("path")) for c in changes if isinstance(c, dict) and c.get("path")]
            self._on_activity("tool", "Editing " + (", ".join(paths) or "files"))
        elif kind == "mcpToolCall":
            self._on_activity(
                "tool",
                f"Using {item.get('server') or 'MCP'}: {item.get('tool') or 'tool'}",
            )
        elif kind == "webSearch":
            self._on_activity("tool", f"Searching the web: {item.get('query') or ''}".strip())
        elif kind == "imageView":
            self._on_activity("tool", f"Viewing {item.get('path') or 'image'}")

    def _item_completed(self, item: dict) -> None:
        kind = item.get("type")
        item_id = str(item.get("id") or "")
        if kind == "agentMessage":
            text = str(item.get("text") or "")
            if item_id in self._assistant_delta_seen:
                streamed = "".join(self._assistant_streams.pop(item_id, []))
                if streamed:
                    self._on_activity("assistant", streamed)
            elif text:
                self._assistant_parts.append(text)
                self._on_activity("assistant", text)
        elif kind in ("reasoning", "plan"):
            reasoning = "".join(self._reasoning_streams.pop(item_id, []))
            if reasoning:
                self._on_activity("thinking", reasoning)
        elif kind == "commandExecution":
            chunks = self._tool_outputs.pop(item_id, [])
            output = "".join(chunks) or str(item.get("aggregatedOutput") or "")
            if output.strip():
                self._on_activity("result", output.strip())
        elif kind == "fileChange":
            changes = item.get("changes") or []
            summary = []
            for change in changes:
                if isinstance(change, dict) and change.get("path"):
                    summary.append(f"{change.get('kind') or 'changed'}: {change['path']}")
            if summary:
                self._on_activity("result", "\n".join(summary))


_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[()][A-Z0-9])")


def _strip_terminal_noise(text: str) -> str:
    text = _ANSI_RE.sub("", text).replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _freebuff_chat_dirs(cwd: str) -> dict[str, float]:
    """Return known FreeBuff chat ids and modification times.

    FreeBuff derives its storage bucket from the Git root, which is not always
    the workspace basename supplied with ``--cwd``.  Search every chat bucket
    so a newly-created id can still be captured and resumed.
    """
    root = Path.home() / ".config" / "manicode" / "projects"
    candidates = [root / Path(cwd).name / "chats", root / "chats"]
    try:
        candidates.extend(p for p in root.glob("*/chats") if p not in candidates)
    except OSError:
        pass
    found: dict[str, float] = {}
    for folder in candidates:
        try:
            for path in folder.iterdir():
                if path.is_dir():
                    found[path.name] = max(found.get(path.name, 0.0), path.stat().st_mtime)
        except OSError:
            pass
    return found


def _freebuff_chat_path(cwd: str, session_id: str) -> Optional[Path]:
    """Locate one FreeBuff chat regardless of its Git-derived project bucket."""
    if not session_id:
        return None
    root = Path.home() / ".config" / "manicode" / "projects"
    candidates = [root / Path(cwd).name / "chats", root / "chats"]
    try:
        candidates.extend(path for path in root.glob("*/chats") if path not in candidates)
    except OSError:
        pass
    for folder in candidates:
        candidate = folder / session_id
        if candidate.is_dir():
            return candidate
    return None


def _freebuff_chat_snapshot(chat: Optional[Path]) -> tuple[str, str, list[tuple[str, str, str]]]:
    """Read reasoning, answer text, and agent states from FreeBuff's live chat file."""
    if chat is None:
        return "", "", []
    try:
        payload = json.loads((chat / "chat-messages.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", "", []
    if not isinstance(payload, list):
        return "", "", []
    message = next(
        (
            item
            for item in reversed(payload)
            if isinstance(item, dict) and item.get("variant") == "ai"
        ),
        None,
    )
    if not isinstance(message, dict):
        return "", "", []

    thinking: list[str] = []
    answer: list[str] = []
    agents: list[tuple[str, str, str]] = []
    blocks = message.get("blocks")
    if not isinstance(blocks, list):
        blocks = []
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            content = str(block.get("content") or "").strip()
            if not content or content == "[response interrupted]":
                continue
            if block.get("textType") == "reasoning":
                thinking.append(content)
            else:
                answer.append(content)
        elif kind == "agent":
            name = str(block.get("agentName") or block.get("agentType") or "agent").strip()
            status = str(block.get("status") or "running").strip().casefold()
            agent_id = str(block.get("agentId") or f"{index}:{name}")
            agents.append((agent_id, name, status))
    return "\n\n".join(thinking), "\n\n".join(answer), agents


def _freebuff_run_status(chat: Optional[Path], offset: int = 0) -> str:
    """Return complete/cancelled from FreeBuff's authoritative per-chat log."""
    if chat is None:
        return ""
    try:
        with (chat / "log.jsonl").open("rb") as handle:
            handle.seek(max(0, offset))
            tail = handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
    if "Agent run cancelled by user" in tail:
        return "cancelled"
    if "Main prompt finished" in tail:
        return "complete"
    return ""


class FreebuffWorker(threading.Thread):
    """Drive FreeBuff's interactive TUI through a pseudo-terminal.

    FreeBuff currently has no JSON or headless interface.  A PTY is therefore
    required so its OpenTUI client behaves exactly as it does in a terminal.
    The adapter keeps live narration useful by turning visible TUI updates into
    activity rows and resumes the chat id FreeBuff creates on the next turn.
    """

    _PROMPT_RE = re.compile(r"(?mi)(?:^\s*[›>]\s*$|Enter a coding task or / for commands)")
    _BUSY_RE = re.compile(
        r"(?mi)(?:thinking(?:\.\.\.|…)|working(?:\.\.\.|…)|■\s*Esc|Esc\s+to\s+(?:stop|interrupt))"
    )

    def __init__(
        self,
        prompt: str,
        session_id: Optional[str],
        cwd: str,
        permission_mode: str,
        *,
        model: str = "",
        effort: str = "",
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
        self._model = model.strip()
        self._on_session = on_session
        self._on_started = on_started
        self._on_activity = on_activity
        self._on_complete = on_complete
        self._on_failed = on_failed
        self._on_done = on_done
        self._cancelled = False
        self._accepting_input = threading.Event()
        self._write_lock = threading.Lock()
        self._pty = None

    def accepting_input(self) -> bool:
        return self._accepting_input.is_set() and not self._cancelled

    def steer(self, text: str) -> bool:
        if not self.accepting_input():
            return False
        return self._submit_text(text)

    def _submit_text(self, text: str) -> bool:
        # OpenTUI handles paste and Enter as separate input events. Sending
        # both in one ConPTY write fills the composer but does not submit it.
        if not self._write(text):
            return False
        time.sleep(0.05)
        return self._write("\r")

    def _write(self, text: str) -> bool:
        if self._pty is None:
            return False
        try:
            with self._write_lock:
                self._pty.write(text)
            return True
        except Exception:
            return False

    def cancel(self) -> None:
        self._cancelled = True
        self._accepting_input.clear()
        pty = self._pty
        if pty is not None:
            try:
                pty.terminate(force=True)
            except Exception:
                try:
                    pty.close(force=True)
                except Exception:
                    pass

    def run(self) -> None:
        try:
            self._do_run()
        finally:
            self._accepting_input.clear()
            self.cancel()
            self._on_done()

    def _do_run(self) -> None:
        binary = find_backend_cli(BACKEND_FREEBUFF)
        if not binary:
            self._on_failed("FreeBuff is not installed. Run: npm install -g freebuff")
            return
        try:
            set_freebuff_model(self._model)
        except OSError as exc:
            self._on_failed(f"Could not select the FreeBuff model: {exc}")
            return
        before = _freebuff_chat_dirs(self._cwd)
        chat_path = _freebuff_chat_path(self._cwd, self._session_id or "")
        log_offset = 0
        if chat_path is not None:
            try:
                log_offset = (chat_path / "log.jsonl").stat().st_size
            except OSError:
                pass
        args = [binary, "--cwd", self._cwd]
        if self._session_id:
            args.extend(["--continue", self._session_id])
        try:
            read = self._spawn_pty(args)
        except ImportError:
            package = "pywinpty" if platform.system() == "Windows" else "pexpect"
            self._on_failed(
                f"FreeBuff support needs {package} and pyte. Reinstall BlindPilot dependencies."
            )
            return
        except Exception as exc:
            self._on_failed(f"Failed to launch FreeBuff: {exc}")
            return

        try:
            import pyte
        except ImportError:
            self._on_failed("FreeBuff support needs pyte. Reinstall BlindPilot dependencies.")
            return
        screen = pyte.HistoryScreen(180, 60, history=4000)
        stream = pyte.Stream(screen)
        sent = False
        started = False
        ready_since: Optional[float] = None
        last_visible = ""
        last_thinking = ""
        last_answer = ""
        structured_thinking = ""
        structured_answer = ""
        agent_states: dict[str, str] = {}
        pending_sections: Optional[tuple[str, str]] = None
        screen_changed_at = time.monotonic()
        accepted_recommended_model = False
        saw_busy = False
        session_reported = bool(self._session_id)
        next_session_check = time.monotonic()
        turn_started_at: Optional[float] = None
        next_heartbeat = float("inf")
        completion_seen_at: Optional[float] = None
        deadline = time.monotonic() + 60 * 60

        while not self._cancelled and time.monotonic() < deadline:
            chunk = read(0.25)
            if chunk:
                stream.feed(chunk)
                visible = "\n".join(line.rstrip() for line in screen.display).strip()
                if visible != last_visible:
                    last_visible = visible
                    ready_since = None
                    screen_changed_at = time.monotonic()
                    pending_sections = self._freebuff_sections(visible)

            # The first FreeBuff launch opens an accessible model chooser.
            # Accept its highlighted recommended model so the hidden terminal
            # reaches the composer; later launches remember the selection.
            if not accepted_recommended_model and not sent and "RECOMMENDED" in last_visible:
                self._write("\r")
                accepted_recommended_model = True
                continue

            at_prompt = bool(self._PROMPT_RE.search(last_visible))
            busy = bool(self._BUSY_RE.search(last_visible))
            if sent and busy:
                saw_busy = True
            if not sent and at_prompt:
                if not self._submit_text(self._prompt):
                    self._on_failed("Could not send the prompt to FreeBuff")
                    return
                sent = True
                started = True
                turn_started_at = time.monotonic()
                next_heartbeat = turn_started_at + 30
                # A resumed TUI initially contains the previous turn. Treat it
                # as a baseline so it is not announced again while the new
                # prompt is being painted onto the screen.
                last_thinking, last_answer = self._freebuff_sections(last_visible)
                self._accepting_input.set()
                self._on_started()
                continue

            now = time.monotonic()
            if sent and now >= next_session_check:
                next_session_check = now + 1.0
                if chat_path is None:
                    after = _freebuff_chat_dirs(self._cwd)
                    new_ids = set(after) - set(before)
                    discovered = max(new_ids, key=after.get) if new_ids else self._session_id
                    if discovered:
                        self._session_id = discovered
                        chat_path = _freebuff_chat_path(self._cwd, discovered)
                        log_offset = 0
                        # The terminal fallback may have emitted the first
                        # settled frame just before the chat directory became
                        # visible. Seed the structured trackers from that
                        # frame so switching sources does not narrate it twice.
                        structured_thinking = last_thinking
                        structured_answer = last_answer
                if self._session_id and not session_reported:
                    self._on_session(self._session_id)
                    session_reported = True

            if sent and chat_path is not None:
                thinking, answer, agents = _freebuff_chat_snapshot(chat_path)
                if thinking:
                    structured_thinking = self._emit_stable_delta(
                        "thinking", structured_thinking, thinking
                    )
                if answer:
                    structured_answer = self._emit_stable_delta(
                        "assistant", structured_answer, answer
                    )
                for agent_id, name, status in agents:
                    previous_status = agent_states.get(agent_id)
                    if previous_status is None:
                        self._on_activity("tool", f"FreeBuff started {name}")
                    elif previous_status != status and status in ("complete", "completed"):
                        self._on_activity("result", f"FreeBuff finished {name}")
                    agent_states[agent_id] = status
                run_status = _freebuff_run_status(chat_path, log_offset)
                if run_status == "cancelled":
                    self._on_failed("FreeBuff reported that the response was interrupted")
                    return
                if run_status == "complete":
                    if completion_seen_at is None:
                        completion_seen_at = now
                    elif now - completion_seen_at >= 0.5:
                        break

            if sent and now >= next_heartbeat:
                elapsed = max(1, int(now - (turn_started_at or now)))
                self._on_activity("tool", f"FreeBuff is still working; {elapsed} seconds elapsed")
                next_heartbeat = now + 30
            if (
                started
                and saw_busy
                and pending_sections is not None
                and not (structured_thinking or structured_answer)
                and time.monotonic() - screen_changed_at >= 0.35
            ):
                thinking, answer = pending_sections
                last_thinking = self._emit_stable_delta("thinking", last_thinking, thinking)
                last_answer = self._emit_stable_delta("assistant", last_answer, answer)
                if chat_path is not None:
                    structured_thinking = last_thinking
                    structured_answer = last_answer
                pending_sections = None
            if sent and saw_busy and at_prompt and not busy and chat_path is None:
                if ready_since is None:
                    ready_since = time.monotonic()
                elif time.monotonic() - ready_since >= 1.0:
                    break

        if self._cancelled:
            return
        if not sent:
            self._on_failed("FreeBuff did not become ready for input")
            return

        self._accepting_input.clear()
        _thinking, screen_response = self._freebuff_sections(last_visible)
        response = structured_answer or screen_response
        if response:
            announced_answer = structured_answer or last_answer
            if response != announced_answer:
                addition = (
                    response[len(announced_answer) :].strip()
                    if response.startswith(announced_answer)
                    else response
                )
                if addition:
                    self._on_activity("assistant", addition)
            self._on_complete(response)
        else:
            self._on_failed("No response received from FreeBuff")
            return

        after = _freebuff_chat_dirs(self._cwd)
        new_ids = set(after) - set(before)
        session = max(new_ids, key=after.get) if new_ids else self._session_id
        if session and not session_reported:
            self._on_session(session)

    def _emit_stable_delta(self, kind: str, previous: str, current: str) -> str:
        """Emit only append-only text from a settled TUI frame.

        OpenTUI redraws wrapped lines in place. Announcing a transient frame
        produces duplicated or corrupted speech, so replacements become a new
        baseline and only stable appended text is narrated.
        """
        if not current or current == previous:
            return current
        if previous and not current.startswith(previous):
            return current
        addition = current[len(previous) :].strip()
        if addition:
            self._on_activity(kind, addition)
        return current

    def _spawn_pty(self, args: list[str]) -> Callable[[float], str]:
        if platform.system() == "Windows":
            from winpty import PtyProcess

            # pywinpty accepts an argv list and supplies a real ConPTY console.
            self._pty = PtyProcess.spawn(args, dimensions=(60, 180), cwd=self._cwd)
            chunks: queue.Queue[str] = queue.Queue()

            def pump() -> None:
                while self._pty and self._pty.isalive():
                    try:
                        data = self._pty.read(4096)
                    except Exception:
                        break
                    if data:
                        chunks.put(data)

            threading.Thread(target=pump, daemon=True).start()

            def read(timeout: float) -> str:
                try:
                    return chunks.get(timeout=timeout)
                except queue.Empty:
                    return ""

            return read

        import pexpect

        self._pty = pexpect.spawn(
            args[0],
            args[1:],
            cwd=self._cwd,
            encoding="utf-8",
            dimensions=(60, 180),
            timeout=0.25,
        )

        def read(timeout: float) -> str:
            try:
                return self._pty.read_nonblocking(4096, timeout=timeout)
            except (pexpect.TIMEOUT, pexpect.EOF):
                return ""

        return read

    def _freebuff_sections(self, visible: str) -> tuple[str, str]:
        """Extract reasoning and answer text from FreeBuff's rendered screen."""
        raw_lines = _strip_terminal_noise(visible).splitlines()
        thinking_index = -1
        thinking_indent = 0
        for index, raw in enumerate(raw_lines):
            if re.fullmatch(r"\s*[•*]\s*Thinking\s*", raw, re.IGNORECASE):
                thinking_index = index
                thinking_indent = len(raw) - len(raw.lstrip())

        if thinking_index >= 0:
            candidates = raw_lines[thinking_index + 1 :]
            in_thinking = True
        else:
            prompt_indexes = [index for index, raw in enumerate(raw_lines) if self._prompt in raw]
            candidates = raw_lines[prompt_indexes[-1] + 1 :] if prompt_indexes else []
            in_thinking = False

        thinking: list[str] = []
        answer: list[str] = []
        for raw in candidates:
            stripped = raw.strip()
            if not stripped:
                continue
            lower = stripped.lower()
            if self._PROMPT_RE.search(stripped):
                break
            if re.search(r"\bunlimited\b.*(?:end session|[✕x])", lower):
                break
            if lower.startswith(
                (
                    "freebuff ",
                    "esc ",
                    "ctrl+",
                    "shift+",
                    "sponsored",
                    "ad ",
                    "learn more",
                    "directory:",
                    "model:",
                    "start monetizing",
                    "get api access",
                )
            ):
                continue
            if lower.startswith(("thinking...", "thinking…", "working...", "working…")):
                continue
            if (
                lower.endswith(" ad │")
                or "learn more" in lower
                or "get api access" in lower
                or "start monetizing" in lower
                or "sponsored" in lower
                or (
                    stripped.startswith("│")
                    and stripped.endswith("│")
                    and re.search(r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)+\b", lower)
                )
            ):
                continue
            if re.fullmatch(r"[╭╮╰╯│─━═┄┈\s]+", raw):
                continue
            if stripped.startswith("⎘") or re.fullmatch(r"[⎘•△▽✕xs\d.\s]+", stripped):
                continue
            if self._BUSY_RE.search(stripped):
                continue
            if stripped.startswith("│") and stripped.endswith("│"):
                continue
            if "▸" in stripped and re.search(r"\b(?:running|completed)\b", lower):
                continue

            indent = len(raw) - len(raw.lstrip())
            if in_thinking and indent > thinking_indent:
                thinking.append(stripped)
                continue
            in_thinking = False
            answer.append(stripped)

        return "\n".join(thinking).strip(), "\n".join(answer).strip()

    def _clean_freebuff_screen(self, visible: str) -> str:
        """Compatibility helper returning only user-facing answer text."""
        return self._freebuff_sections(visible)[1]


def worker_class(backend: str, claude_worker: type[threading.Thread]) -> type[threading.Thread]:
    backend = normalize_backend(backend)
    if backend == BACKEND_CODEX:
        return CodexWorker
    if backend == BACKEND_FREEBUFF:
        return FreebuffWorker
    return claude_worker
