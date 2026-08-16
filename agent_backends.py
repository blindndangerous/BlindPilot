"""Backend adapters for BlindPilot.

BlindPilot began as Claude Code Reader. Claude's adapter remains in
``blindpilot_app.py``; this module contains the
provider-neutral discovery helpers plus Codex and FreeBuff workers.
Hermes lives in ``hermes_backend.py`` and ``hermes_worker.py``, imported
on demand so a machine without Hermes pays nothing for it.

Copyright (c) 2026 doubletaponair and BlindPilot contributors.
Based on the original Claude Code Reader application by doubletaponair:
https://github.com/doubletaponair/claude-code-reader
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import atexit
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
from typing import Callable, Optional, Protocol, cast

from markdown_rows import _SENTENCE_END_RE as _markdown_sentence_end_re
from markdown_rows import complete_sentences


# A windowed app owns no console, so every child process Windows considers a
# console program is given a brand new one - a terminal that pops up on screen,
# takes focus away from the screen reader, and in the case of a long-running
# agent CLI stays there for the whole turn. CREATE_NO_WINDOW suppresses it.
CREATE_NO_WINDOW = 0x08000000 if platform.system() == "Windows" else 0


def no_window_kwargs() -> dict:
    """``subprocess`` keyword arguments that keep children off the screen."""
    return {"creationflags": CREATE_NO_WINDOW} if CREATE_NO_WINDOW else {}


# A pseudo-terminal is the one child that cannot be given CREATE_NO_WINDOW: the
# flag belongs to its host, which pywinpty starts itself. Launched from a
# windowed application, which owns no console to lend it, that host puts a real
# console on screen. It can still be hidden the moment it appears.
_CONSOLE_WINDOW_CLASSES = ("ConsoleWindowClass", "PseudoConsoleWindow")


def reserve_hidden_console() -> bool:
    """Take a console for this process now, hidden, and keep it.

    Creating a pseudo-terminal gives a windowed application a console whether
    it wants one or not, and that console arrives visible. Claiming one up
    front, before any terminal is created, means there is nothing left to
    create later: the console already exists and is already hidden.
    """
    if platform.system() != "Windows":
        return False
    import ctypes

    kernel32 = ctypes.windll.kernel32
    try:
        handle = kernel32.GetConsoleWindow()
        if not handle:
            if not kernel32.AllocConsole():
                return False
            handle = kernel32.GetConsoleWindow()
        if handle:
            _banish_window(handle)
        return bool(handle)
    except OSError:
        return False


def _banish_window(handle: int) -> None:
    """Hide a window, and park it off-screen in case it is shown again.

    Hiding alone leaves a window that something else can show, and the console
    is shown again while the terminal is torn down. Off-screen and sized to
    nothing, being shown costs nothing that can be seen.
    """
    import ctypes

    user32 = ctypes.windll.user32
    try:
        user32.ShowWindow(handle, 0)  # SW_HIDE
        # SWP_NOACTIVATE | SWP_NOZORDER | SWP_NOOWNERZORDER
        user32.SetWindowPos(handle, 0, -32000, -32000, 0, 0, 0x0010 | 0x0004 | 0x0200)
    except OSError:
        pass


def _descendant_pids(roots: set[int]) -> set[int]:
    """Every process descended from ``roots``, so only our own are touched."""
    import ctypes
    import ctypes.wintypes as wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == -1:
        return set(roots)
    parents: dict[int, int] = {}
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(ProcessEntry)
        ok = kernel32.Process32First(snapshot, ctypes.byref(entry))
        while ok:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = kernel32.Process32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)

    family = set(roots)
    for pid in parents:
        seen: set[int] = set()
        walker = pid
        while walker and walker not in seen:
            seen.add(walker)
            if walker in family:
                family.update(seen)
                break
            walker = parents.get(walker, 0)
    return family


def hide_console_windows(roots: Optional[set[int]] = None) -> int:
    """Hide the console this process was given, and any its children raise.

    Creating the pseudo-terminal attaches a console to BlindPilot itself, which
    is why the window belongs to us rather than to the program being run. That
    one is found directly; the enumeration is the safety net for a child that
    raises its own, and never touches a console outside this process tree.
    """
    if platform.system() != "Windows":
        return 0
    import ctypes
    import ctypes.wintypes as wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    hidden = 0
    try:
        own = kernel32.GetConsoleWindow()
        if own and user32.IsWindowVisible(own):
            _banish_window(own)
            hidden += 1
    except OSError:
        pass

    candidates: list[tuple[int, int]] = []

    def visit(handle, _param):
        try:
            if not user32.IsWindowVisible(handle):
                return True
            name = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(handle, name, 64)
            if name.value in _CONSOLE_WINDOW_CLASSES:
                owner = wintypes.DWORD()
                user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
                candidates.append((handle, int(owner.value)))
        except Exception:
            pass
        return True

    try:
        callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(visit)
        user32.EnumWindows(callback, 0)
    except Exception:
        return hidden
    # Walking the process table costs milliseconds, and this runs in a tight
    # loop to catch the window in the frame it appears. Pay for it only when
    # there is actually a console window on screen to account for.
    if not candidates:
        return hidden
    try:
        family = _descendant_pids(set(roots or ()) | {os.getpid()})
    except Exception:
        family = set(roots or ()) | {os.getpid()}
    for handle, owner in candidates:
        if owner in family:
            try:
                _banish_window(handle)
                hidden += 1
            except Exception:
                pass
    return hidden


BACKEND_CLAUDE = "claude"
BACKEND_CODEX = "codex"
BACKEND_FREEBUFF = "freebuff"
BACKEND_HERMES = "hermes"
BACKEND_IDS = (BACKEND_CLAUDE, BACKEND_CODEX, BACKEND_FREEBUFF, BACKEND_HERMES)
BACKEND_LABELS = {
    BACKEND_CLAUDE: "Claude Code",
    BACKEND_CODEX: "Codex",
    BACKEND_FREEBUFF: "FreeBuff",
    BACKEND_HERMES: "Hermes",
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
    # Whether the provider can summarise a long conversation in place to free
    # up its context window. FreeBuff's CLI has no such command — its only
    # context control is starting a new conversation.
    supports_compaction: bool = False
    # Whether signing in has to happen in a terminal the user can type into.
    # Claude and Codex authenticate through a browser and report the result on
    # exit, so BlindPilot can run them hidden and watch. Hermes' equivalent is
    # an interactive picker: run hidden with no stdin it simply fails, so the
    # wizard opens a real console instead of pretending to have signed in.
    login_needs_terminal: bool = False


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
        supports_compaction=True,
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
        supports_compaction=True,
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
        supports_compaction=False,
    ),
    BACKEND_HERMES: BackendInfo(
        BACKEND_HERMES,
        "Hermes",
        "hermes",
        "See https://hermes-agent.nousresearch.com/docs",
        ("model",),
        True,
        # Hermes has no per-turn reasoning-effort control on the protocol this
        # adapter speaks; effort is a profile setting rather than a turn flag.
        False,
        True,
        True,
        supports_compaction=True,
        login_needs_terminal=True,
    ),
}

# What a "compact this conversation" turn looks like per provider: the text to
# send, and any extra keyword arguments its worker needs.
#
# Claude Code takes ``/compact`` as an ordinary message even in headless
# streaming mode, and acts on it. Codex has no such message — compaction is a
# separate app-server request — so its worker is told to compact instead, and
# ignores the text. The text is still shown to the user either way, so the row
# in the list says what was asked for.
_COMPACTION_REQUESTS: dict[str, tuple[str, dict]] = {
    BACKEND_CLAUDE: ("/compact", {}),
    BACKEND_CODEX: ("/compact", {"compact": True}),
    # Hermes compacts through a request of its own, like Codex. Its own name
    # for the command is /compress; the text shown to the user says what was
    # asked for in BlindPilot's words, and the worker acts on the flag.
    BACKEND_HERMES: ("/compact", {"compact": True}),
}


def compaction_request(backend: object) -> Optional[tuple[str, dict]]:
    """(message text, extra worker arguments) to compact, or None if it can't."""
    return _COMPACTION_REQUESTS.get(normalize_backend(backend))


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
        "hermes": BACKEND_HERMES,
        "hermesagent": BACKEND_HERMES,
        "nous": BACKEND_HERMES,
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
    if info.id == BACKEND_HERMES:
        # Hermes installs itself under its own home directory rather than into
        # a shared bin, so it has a search of its own that also knows about the
        # virtual environment the gateway is launched from.
        from hermes_backend import find_hermes_cli

        return find_hermes_cli()
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
    if backend == BACKEND_HERMES:
        from hermes_backend import hermes_auth_ok

        return hermes_auth_ok(timeout=max(timeout, 25))
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
                **no_window_kwargs(),
            )
            return proc.returncode == 0
        if backend == BACKEND_CODEX:
            proc = subprocess.run(
                [binary, "login", "status"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                **no_window_kwargs(),
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
            **no_window_kwargs(),
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


def blindpilot_config_dir() -> Path:
    """Where BlindPilot keeps its own settings, separate from any backend's."""
    if platform.system() == "Windows":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "BlindPilot"
    return Path.home() / ".config" / "blindpilot"


def _freebuff_choice_path() -> Path:
    return blindpilot_config_dir() / "freebuff-model.json"


def _read_freebuff_choice() -> str:
    """Return the FreeBuff model BlindPilot last selected, if any."""
    try:
        payload = json.loads(_freebuff_choice_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    model = payload.get("model") if isinstance(payload, dict) else None
    return model.strip() if isinstance(model, str) else ""


def _write_freebuff_choice(model: str) -> None:
    """Record the selection somewhere only BlindPilot writes.

    FreeBuff resets ``freebuffModel`` in its own settings to whichever model it
    recommends once a turn has run, so that file cannot be read back as the
    user's choice.  This record is what survives.
    """
    path = _freebuff_choice_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"model": model}, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def _freebuff_catalog_cache_path() -> Path:
    return blindpilot_config_dir() / "freebuff-catalog.json"


def _read_freebuff_catalog_cache(stamp: tuple[str, float, int]) -> list[str]:
    """The catalog last read out of this exact FreeBuff release, if any.

    Discovering the catalog means scanning a hundred megabytes of compiled
    FreeBuff, which takes seconds.  Doing that once per installed release rather
    than once per run of BlindPilot is the difference between a message that
    sends immediately and one that waits on a file scan first.
    """
    try:
        payload = json.loads(_freebuff_catalog_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    if payload.get("path") != stamp[0] or payload.get("size") != stamp[2]:
        return []
    if abs(float(payload.get("mtime") or 0.0) - stamp[1]) > 0.001:
        return []
    models = payload.get("models")
    if not isinstance(models, list):
        return []
    return [model for model in models if isinstance(model, str) and model]


def _write_freebuff_catalog_cache(stamp: tuple[str, float, int], models: list[str]) -> None:
    path = _freebuff_catalog_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"path": stamp[0], "mtime": stamp[1], "size": stamp[2], "models": models},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


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


# FreeBuff stamps a release date onto some display names, and revises it in
# place: "DeepSeek V4 Pro" became "DeepSeek V4 Pro 08/13" without the packaged
# README following. Reading the two as different models drops the dated one.
_MODEL_DATE_SUFFIX_RE = re.compile(r"\s+\d{1,2}/\d{1,2}(?:/\d{2,4})?$")


def _readme_offset(readme_text: str, display_name: str) -> int:
    """Where the README documents this model, or -1 if it does not at all."""
    offset = readme_text.find(display_name)
    if offset >= 0:
        return offset
    undated = _MODEL_DATE_SUFFIX_RE.sub("", display_name)
    return readme_text.find(undated) if undated != display_name else -1


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
    remembered = _read_freebuff_catalog_cache(stamp)
    if remembered:
        _freebuff_catalog_cache = (stamp, list(remembered))
        return list(remembered)

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
        order = _readme_offset(readme_text, display_name) if readme_text else -1
        if readme_text and order < 0:
            continue
        model_id = _resolve_minified_string(source, match.group(2), direct)
        if not model_id or "/" not in model_id:
            continue
        discovered.append((order if order >= 0 else match.start(), model_id))

    models: list[str] = []
    for _order, model_id in sorted(discovered):
        if model_id not in models:
            models.append(model_id)
    if FREEBUFF_PREFERRED_MODEL in models:
        models.remove(FREEBUFF_PREFERRED_MODEL)
        models.insert(0, FREEBUFF_PREFERRED_MODEL)
    _freebuff_catalog_cache = (stamp, list(models))
    _write_freebuff_catalog_cache(stamp, list(models))
    return models


def freebuff_model_options() -> tuple[list[str], list[str], str, str, str]:
    """Return the model choices exposed by the installed FreeBuff release."""
    binary = find_backend_cli(BACKEND_FREEBUFF)
    models = _freebuff_models_from_install(binary)
    chosen = _read_freebuff_choice()
    saved = ""
    try:
        settings = Path.home() / ".config" / "manicode" / "settings.json"
        payload = json.loads(settings.read_text(encoding="utf-8"))
        selected = payload.get("freebuffModel") if isinstance(payload, dict) else None
        if isinstance(selected, str) and selected.strip():
            saved = selected.strip()
    except (OSError, ValueError):
        pass
    if not models:
        models = [FREEBUFF_PREFERRED_MODEL]
        error = "Could not refresh FreeBuff's model catalog; showing the preferred default."
    else:
        error = ""
    for candidate in (chosen, saved):
        if candidate and candidate not in models:
            models.append(candidate)
    # BlindPilot's own record wins over FreeBuff's settings file. FreeBuff
    # rewrites that file to its recommended model after a turn, so honouring it
    # would quietly downgrade every following turn to the recommendation.
    current = (
        chosen
        if chosen in models
        else FREEBUFF_PREFERRED_MODEL
        if FREEBUFF_PREFERRED_MODEL in models
        else saved
        if saved in models
        else models[0]
    )
    return models, [], current, "", error


def _freebuff_picker_options(visible: str, models: list[str]) -> tuple[list[str], int]:
    """Return the model IDs painted by FreeBuff's picker and its focused index."""
    options: list[str] = []
    focused = -1
    for raw in visible.splitlines():
        if "│" not in raw:
            continue
        words = set(re.findall(r"[a-z0-9]+", raw.casefold()))
        matched = ""
        for model in models:
            leaf = model.rsplit("/", 1)[-1]
            tokens = re.findall(r"[a-z0-9]+", leaf.casefold())
            if tokens and all(token in words for token in tokens):
                matched = model
                break
        if not matched or matched in options:
            continue
        options.append(matched)
        if "›" in raw or re.search(r"(?:^|│)\s*>\s*", raw):
            focused = len(options) - 1
    return options, focused


def invalidate_backend_cache(backend: str | None = None) -> None:
    """Drop version-derived provider data before an explicit runtime refresh."""
    global _freebuff_catalog_cache
    if backend is None or normalize_backend(backend) == BACKEND_FREEBUFF:
        _freebuff_catalog_cache = None
        try:
            _freebuff_catalog_cache_path().unlink()
        except OSError:
            pass


def set_freebuff_model(model: str) -> None:
    """Select the model in FreeBuff, and record the choice as BlindPilot's own."""
    selected = model.strip()
    if not selected:
        models, _efforts, current, _effort, _error = freebuff_model_options()
        selected = current or (models[0] if models else FREEBUFF_PREFERRED_MODEL)
    settings = Path.home() / ".config" / "manicode" / "settings.json"
    with _FREEBUFF_SETTINGS_LOCK:
        _write_freebuff_choice(selected)
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
        compact: bool = False,
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
        # Compaction is a request of its own rather than a message, so this
        # turn summarises the conversation instead of adding to it.
        self._compact = compact
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
        if self._compact and not self._session_id:
            self._on_failed("There is no Codex conversation to compact yet")
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
                **no_window_kwargs(),
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
            sandbox_type = str(sandbox.get("type", ""))
            params: dict = {
                "cwd": self._cwd,
                "approvalPolicy": approval,
                "sandbox": {
                    "readOnly": "read-only",
                    "workspaceWrite": "workspace-write",
                    "dangerFullAccess": "danger-full-access",
                }.get(sandbox_type, "workspace-write"),
                "serviceName": "blindpilot",
            }
            if self._model:
                params["model"] = self._model
            request = {"method": "thread/start", "id": thread_request, "params": params}
        self._send(request)

        turn_request = 0
        compact_request = 0
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
                if self._compact:
                    # Compaction is not a message: it answers immediately with
                    # an empty result and then runs a turn of its own, whose
                    # notifications the loop below already understands.
                    compact_request = self._next_id()
                    self._send(
                        {
                            "method": "thread/compact/start",
                            "id": compact_request,
                            "params": {"threadId": self._thread_id},
                        }
                    )
                    continue
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

            if compact_request and message.get("id") == compact_request:
                error = message.get("error")
                if error:
                    self._on_failed(self._error_text(error, "Codex could not compact"))
                    return
                # The result is empty; the compaction turn announces itself.
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
                elif self._compact:
                    # A compaction turn produces no answer text of its own, so
                    # say what happened rather than finishing in silence.
                    self._on_complete("Conversation compacted.")
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


_FREEBUFF_INTERRUPTED = "[response interrupted]"

# How long a frame must hold still before it is read out. The terminal repaints
# in bursts, so this is only long enough to let one burst land whole.
_FREEBUFF_FRAME_SECONDS = 0.1

# Never wait longer than this to read out a finished sentence, however busy the
# repainting is. Text that arrives faster than the frames settle would otherwise
# never reach the listener until the turn ended.
_FREEBUFF_MAX_LAG_SECONDS = 0.4

# Sentence-ending punctuation, or the end of a paragraph, either of which is a
# place a listener expects the reading to stop. The definition lives in
# ``markdown_rows`` (which depends on nothing of ours) so the Hermes worker can
# share it without importing this module and closing an import cycle.
_SENTENCE_END_RE = _markdown_sentence_end_re


def _complete_sentences(text: str) -> str:
    """The part of ``text`` that reads as finished, or nothing yet."""
    return complete_sentences(text)


def _unwrap_screen_text(text: str) -> str:
    """Rejoin the lines the terminal broke, keeping the ones the answer meant.

    A captured frame is text laid out to the width of FreeBuff's box, so most of
    its newlines belong to the terminal rather than the answer.  Left in, every
    one of them reads as the end of a sentence, and the reading stops in the
    middle of a clause.  A line that runs the full width was broken because it
    ran out of room and continues below; a short one ended because the text did.
    """
    lines = [line.rstrip() for line in text.splitlines()]
    if len(lines) < 2:
        return text.strip()
    width = max(len(line) for line in lines)
    if width < 24:
        return text.strip()
    pieces: list[str] = []
    for index, line in enumerate(lines[:-1]):
        pieces.append(line)
        following = lines[index + 1]
        wrapped = bool(line) and bool(following) and len(line) >= width - 12
        pieces.append(" " if wrapped else "\n")
    pieces.append(lines[-1])
    return "".join(pieces).strip()


def _keyed(text: str, letters_only: bool = False) -> tuple[str, list[int]]:
    """A comparable form of ``text``, and where each kept character came from.

    Whitespace always goes, because the terminal decides where the answer
    breaks its lines and revises that as the text grows: the same words can be
    one line in one frame and two in the next. Dropping everything except
    letters and digits goes further, and makes the answer as the terminal drew
    it comparable with the Markdown it was drawn from.
    """
    kept: list[str] = []
    positions: list[int] = []
    for index, character in enumerate(text):
        if character.isspace() or (letters_only and not character.isalnum()):
            continue
        kept.append(character.casefold() if letters_only else character)
        positions.append(index)
    positions.append(len(text))
    return "".join(kept), positions


def _unspoken_tail(narrated: str, answer: str) -> str:
    """The part of the finished answer that was never read out.

    What was read came off the screen, laid out for a terminal; the finished
    answer comes from the saved chat, written as Markdown. Comparing only the
    letters and digits is what makes the two comparable, so that an answer
    which scrolled out of view while it was being written is still finished
    aloud, and one that was read in full is not read twice.
    """
    spoken_key, _spoken_positions = _keyed(narrated, letters_only=True)
    answer_key, positions = _keyed(answer, letters_only=True)
    if not answer_key:
        return ""
    if not spoken_key:
        return answer.strip()
    if answer_key.startswith(spoken_key):
        return answer[positions[len(spoken_key)] :].strip()
    if answer_key in spoken_key:
        return ""
    # The two do not line up at all, which means the reading was of something
    # else. Reading the answer again is better than never reading it.
    return answer.strip()


def _append_delta(spoken: str, current: str) -> str:
    """The part of ``current`` that has not been read out yet.

    The screen is a window onto the answer, not the whole of it: once the answer
    is taller than the terminal, the top scrolls away and a frame becomes a
    continuation rather than an extension.  Matching the longest overlap between
    the end of what was read and the start of what is on screen keeps the
    reading in one piece across that scroll, instead of repeating a paragraph or
    silently dropping one.
    """
    if not current:
        return ""
    if not spoken:
        return current
    spoken_key, _spoken_positions = _keyed(spoken)
    current_key, positions = _keyed(current)
    if not current_key:
        return ""
    if current_key.startswith(spoken_key):
        return current[positions[len(spoken_key)] :]
    # The tail of what was read is normally still on screen, so find it there
    # first; that is one string search rather than a scan over every overlap.
    anchor = spoken_key[-160:]
    if anchor:
        position = current_key.rfind(anchor)
        if position >= 0:
            return current[positions[position + len(anchor)] :]
    for size in range(min(len(spoken_key), len(current_key), 400), 0, -1):
        if spoken_key.endswith(current_key[:size]):
            return current[positions[size] :]
    return current


def _freebuff_answer_message(payload: object) -> Optional[dict]:
    """Return the newest AI message that actually carries a reply.

    FreeBuff writes a ``mode-divider`` message ahead of every turn, and it also
    has ``variant: "ai"``.  Skipping messages with no reply blocks keeps the
    identity of the turn's real answer stable from the moment it appears.
    """
    if not isinstance(payload, list):
        return None
    for item in reversed(payload):
        if not isinstance(item, dict) or item.get("variant") != "ai":
            continue
        blocks = item.get("blocks")
        if isinstance(blocks, list) and any(
            isinstance(block, dict) and block.get("type") in ("text", "agent") for block in blocks
        ):
            return item
    return None


def _freebuff_answer_id(chat: Optional[Path]) -> str:
    """Identify the newest answer already in a chat, before a new turn starts."""
    if chat is None:
        return ""
    try:
        payload = json.loads((chat / "chat-messages.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    message = _freebuff_answer_message(payload)
    return str(message.get("id") or "") if message else ""


def _freebuff_chat_snapshot(
    chat: Optional[Path],
) -> tuple[str, str, str, list[tuple[str, str, str]]]:
    """Read the newest answer's id, reasoning, text, and agent states.

    The id is what tells one turn's answer from the one before it: FreeBuff
    rewrites the whole file on every save, so text alone cannot distinguish new
    output from a resumed conversation's history.
    """
    if chat is None:
        return "", "", "", []
    try:
        payload = json.loads((chat / "chat-messages.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", "", "", []
    message = _freebuff_answer_message(payload)
    if message is None:
        return "", "", "", []
    message_id = str(message.get("id") or "")

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
            # Closing the hidden terminal stamps this marker onto whatever the
            # turn had produced, so it has to come off the end as well as being
            # rejected on its own.
            if content.endswith(_FREEBUFF_INTERRUPTED):
                content = content[: -len(_FREEBUFF_INTERRUPTED)].strip()
            if not content:
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
    return message_id, "\n\n".join(thinking), "\n\n".join(answer), agents


def _freebuff_chat_stamp(chat: Optional[Path]) -> tuple:
    """A cheap fingerprint of a chat's files, to skip re-reading them.

    The loop looks at the chat many times a second so that the end of a turn is
    noticed the moment it happens; asking the filesystem what changed is orders
    of magnitude cheaper than parsing the whole conversation to find out.
    """
    if chat is None:
        return ()
    marks: list[tuple[int, int]] = []
    for name in ("chat-messages.json", "log.jsonl"):
        try:
            stat = (chat / name).stat()
            marks.append((stat.st_mtime_ns, stat.st_size))
        except OSError:
            marks.append((0, 0))
    return tuple(marks)


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


class FreebuffTerminal(Protocol):
    """The pseudo-terminal handle, whichever library produced it.

    winpty on Windows and pexpect everywhere else hand back objects with
    nothing in common but the three calls made on them here. What those calls
    give back differs too, and none of it is used, so none of it is named.
    """

    def write(self, text: str, /) -> object: ...

    def terminate(self, force: bool = False) -> object: ...

    def close(self, force: bool = False) -> object: ...


def _spawn_freebuff_pty(
    args: list[str], cwd: str, stream_ended: threading.Event
) -> tuple[FreebuffTerminal, Callable[[float], str]]:
    """Start FreeBuff under a pseudo-terminal, off screen, and read it.

    Returns the terminal and a call that yields whatever it has produced.
    Output is pumped into a queue by a thread of its own, so a terminal that
    started before anyone asked for its output keeps every byte of it.
    """
    if platform.system() == "Windows":
        from winpty import PtyProcess

        # Own the console before the terminal asks for one, so none is
        # created on screen. The watcher below is the safety net for the
        # consoles FreeBuff's own tools can still raise.
        reserve_hidden_console()
        roots = {os.getpid()}
        holder: dict[str, object] = {}

        def hide_terminal() -> None:
            # Poll hard while the terminal is starting, then slowly: the
            # tools FreeBuff itself starts can each raise a console of
            # their own, and any of them would take the reader's focus.
            # Tearing the terminal down raises one last console, after the
            # output has stopped, so this outlives the stream it guards.
            started = time.monotonic()
            grace_until: Optional[float] = None
            while True:
                now = time.monotonic()
                if stream_ended.is_set():
                    if grace_until is None:
                        grace_until = now + 5
                    elif now > grace_until:
                        return
                try:
                    hide_console_windows(roots)
                except Exception:
                    # Never let this thread die: it is the only thing
                    # keeping the terminal off the screen for the whole run.
                    pass
                quick = now - started < 8 or grace_until is not None
                time.sleep(0.005 if quick else 0.25)

        threading.Thread(target=hide_terminal, daemon=True).start()

        # pywinpty accepts an argv list and supplies a real ConPTY console.
        pty = PtyProcess.spawn(args, dimensions=(60, 180), cwd=cwd)
        holder["pty"] = pty
        pty_pid = getattr(pty, "pid", 0)
        if pty_pid:
            roots.add(int(pty_pid))
        chunks: queue.Queue[str] = queue.Queue()

        def pump() -> None:
            try:
                while pty.isalive():
                    try:
                        data = pty.read(4096)
                    except Exception:
                        break
                    if data:
                        chunks.put(data)
            finally:
                # Nothing more will ever arrive. Saying so turns a terminal
                # that died at startup into a reported failure instead of an
                # hour of silence.
                stream_ended.set()

        threading.Thread(target=pump, daemon=True).start()

        def read(timeout: float) -> str:
            try:
                return chunks.get(timeout=timeout)
            except queue.Empty:
                return ""

        return pty, read

    import pexpect

    child = pexpect.spawn(
        args[0],
        args[1:],
        cwd=cwd,
        encoding="utf-8",
        dimensions=(60, 180),
        timeout=0.25,
    )

    def read_posix(timeout: float) -> str:
        try:
            return child.read_nonblocking(4096, timeout=timeout)
        except pexpect.TIMEOUT:
            return ""
        except pexpect.EOF:
            stream_ended.set()
            return ""

    return child, read_posix


# A FreeBuff terminal takes seconds to reach its composer, and that wait is the
# largest part of how long a message takes to start answering. One is therefore
# started ahead of time, for the conversation the next message will most likely
# continue, and handed to the run that claims it.
_FREEBUFF_PREWARM_LOCK = threading.Lock()
_freebuff_prewarm: Optional[dict] = None
# Long enough to cover thinking time between messages, short enough that an
# abandoned terminal does not sit there all day.
_FREEBUFF_PREWARM_TTL = 15 * 60


def _kill_pty(pty: object) -> None:
    for method, arguments in (("terminate", (True,)), ("close", (True,))):
        call = getattr(pty, method, None)
        if call is None:
            continue
        try:
            call(*arguments)
            return
        except Exception:
            continue


def discard_freebuff_prewarm() -> None:
    """Throw away any terminal held for the next message."""
    global _freebuff_prewarm
    with _FREEBUFF_PREWARM_LOCK:
        holding, _freebuff_prewarm = _freebuff_prewarm, None
    if holding is not None:
        holding["ended"].set()
        _kill_pty(holding["pty"])


def prewarm_freebuff(cwd: str, session_id: Optional[str], model: str, delay: float = 0.0) -> None:
    """Start the terminal the next FreeBuff message will use.

    Doing nothing here is always safe: a message that finds no terminal waiting,
    or one started for a different conversation, simply starts its own.
    """
    binary = find_backend_cli(BACKEND_FREEBUFF)
    if not binary:
        return
    key = (os.path.abspath(cwd), session_id or "", model or _read_freebuff_choice())
    with _FREEBUFF_PREWARM_LOCK:
        holding = _freebuff_prewarm
        if holding is not None and holding["key"] == key and not holding["ended"].is_set():
            # One is already waiting for exactly this. Starting another would
            # throw away a terminal that has finished starting for one that has
            # not, which is the opposite of the point.
            return

    def work() -> None:
        if delay:
            time.sleep(delay)
        # FreeBuff reads the selected model once, at launch, and rewrites the
        # setting to its own recommendation after a turn. Applying the choice
        # before the terminal starts is what makes a waiting one usable.
        try:
            set_freebuff_model(key[2])
        except OSError:
            return
        args = [binary, "--cwd", cwd]
        if session_id:
            args.extend(["--continue", session_id])
        # A new conversation's chat is created when the terminal starts, not
        # when it is given a message, so the record of which chats existed
        # beforehand has to be taken here. Taken later it would already include
        # this one, and the message would finish without ever learning the id of
        # the conversation it had just had.
        before = _freebuff_chat_dirs(cwd)
        ended = threading.Event()
        try:
            pty, read = _spawn_freebuff_pty(args, cwd, ended)
        except Exception:
            return
        holding = {
            "key": key,
            "pty": pty,
            "read": read,
            "ended": ended,
            "before": before,
            "expires": time.monotonic() + _FREEBUFF_PREWARM_TTL,
        }
        stale = None
        with _FREEBUFF_PREWARM_LOCK:
            global _freebuff_prewarm
            stale, _freebuff_prewarm = _freebuff_prewarm, holding
        if stale is not None:
            stale["ended"].set()
            _kill_pty(stale["pty"])

    threading.Thread(target=work, daemon=True).start()


def _take_freebuff_prewarm(cwd: str, session_id: Optional[str], model: str) -> Optional[dict]:
    """Claim the waiting terminal, if it is the one this message needs."""
    key = (os.path.abspath(cwd), session_id or "", model)
    global _freebuff_prewarm
    with _FREEBUFF_PREWARM_LOCK:
        holding = _freebuff_prewarm
        if holding is None:
            return None
        expired = time.monotonic() > holding["expires"]
        if holding["key"] != key or expired or holding["ended"].is_set():
            # Whatever is waiting cannot serve this message. Drop it rather
            # than leave a terminal running for a conversation nobody resumed.
            _freebuff_prewarm = None
        else:
            _freebuff_prewarm = None
            return holding
    holding["ended"].set()
    _kill_pty(holding["pty"])
    return None


atexit.register(discard_freebuff_prewarm)


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
        self._pty: Optional[FreebuffTerminal] = None
        # Set once the terminal can produce no further output.
        self._stream_ended = threading.Event()
        # Everything read out this turn, and the frame each kind is waiting to
        # see a second time before believing it.
        self._narrated: dict[str, str] = {}
        self._pending_frame: dict[str, str] = {}

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
        # Reading the model catalog means scanning the whole of FreeBuff, which
        # is far too slow to do before sending a message. The recorded choice is
        # all that is needed here; the catalog is only fetched if the model
        # picker actually appears, which happens on a first launch.
        try:
            self._model = self._model or _read_freebuff_choice() or FREEBUFF_PREFERRED_MODEL
            set_freebuff_model(self._model)
        except OSError as exc:
            self._on_failed(f"Could not select the FreeBuff model: {exc}")
            return
        models: list[str] = []
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
        waiting = _take_freebuff_prewarm(self._cwd, self._session_id, self._model)
        adopted = waiting is not None
        if waiting is not None:
            # A terminal was started for this conversation while the message was
            # being typed, and has been buffering its output ever since. Taking
            # it saves the whole of FreeBuff's start-up. Its own record of the
            # chats that existed before it started is what identifies the new
            # conversation, since it created that chat when it started.
            # The prewarm record holds a terminal, a reader, an event and a
            # listing side by side, so what comes back out of it is opaque
            # until it is named.
            self._pty = cast(FreebuffTerminal, waiting["pty"])
            read = waiting["read"]
            self._stream_ended = waiting["ended"]
            before = waiting["before"]
        else:
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
        adopted_at = time.monotonic()

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
        # What has been read out already, in the shape the screen paints it, so
        # the next frame can be compared against it.
        spoken_thinking = ""
        spoken_answer = ""
        last_emit_at = 0.0
        structured_answer = ""
        # A resumed chat already holds the previous turn's answer. Remember its
        # id so it is never mistaken for this turn's, and so the real answer is
        # recognised as new the moment FreeBuff writes it.
        baseline_answer_id = _freebuff_answer_id(chat_path)
        structured_answer_id = ""
        chat_stamp: tuple = ()
        run_status = ""
        agent_states: dict[str, str] = {}
        screen_dirty = False
        screen_changed_at = time.monotonic()
        accepted_recommended_model = False
        picker_expanded = False
        picker_expanded_at = 0.0
        saw_busy = False
        session_reported = bool(self._session_id)
        next_session_check = time.monotonic()
        turn_started_at: Optional[float] = None
        next_heartbeat = float("inf")
        completion_seen_at: Optional[float] = None
        deadline = time.monotonic() + 60 * 60

        def restart() -> Optional[Callable[[float], str]]:
            """Replace a terminal that was waiting and is no longer usable.

            One started ahead of time can have been sitting for a quarter of an
            hour, and can have died or moved on in that time. Starting again
            costs the usual wait; refusing to would cost the message.
            """
            nonlocal before
            _kill_pty(self._pty)
            # The terminal being replaced had a whole FreeBuff in it, and one
            # rewrites the model setting to its own recommendation as it goes.
            # The replacement reads that setting at launch, so the choice has to
            # be put back before it starts rather than only before the first.
            try:
                set_freebuff_model(self._model)
            except OSError:
                pass
            before = _freebuff_chat_dirs(self._cwd)
            self._stream_ended = threading.Event()
            try:
                return self._spawn_pty(args)
            except Exception as exc:
                self._on_failed(f"Failed to launch FreeBuff: {exc}")
                return None

        while not self._cancelled and time.monotonic() < deadline:
            # Short enough that a frame is picked up as soon as it lands. The
            # answer is read off the screen, so this interval is the floor on
            # how quickly a finished sentence can reach the listener.
            chunk = read(0.03)
            if chunk:
                # Take everything already waiting as one frame. A repaint
                # arrives as a burst of small writes, and feeding them one at a
                # time would mean laying out the screen over and over.
                for _ in range(256):
                    more = read(0)
                    if not more:
                        break
                    chunk += more
            stale = adopted and not sent and time.monotonic() - adopted_at >= 12
            if stale or (not chunk and self._stream_ended.is_set() and not sent and adopted):
                adopted = False
                replacement = restart()
                if replacement is None:
                    return
                read = replacement
                screen = pyte.HistoryScreen(180, 60, history=4000)
                stream = pyte.Stream(screen)
                last_visible = ""
                screen_dirty = False
                continue
            if not chunk and self._stream_ended.is_set():
                if not sent:
                    self._on_failed(
                        "FreeBuff's terminal closed before it was ready for a prompt. "
                        "Reinstall BlindPilot, then try again."
                    )
                    return
                # It ended mid-turn, so whatever was captured is the whole
                # answer; fall through to report it rather than wait it out.
                break
            if chunk:
                stream.feed(chunk)
                visible = "\n".join(line.rstrip() for line in screen.display).strip()
                if visible != last_visible:
                    last_visible = visible
                    ready_since = None
                    screen_changed_at = time.monotonic()
                    screen_dirty = True

            # The first FreeBuff launch opens an accessible model chooser.
            # Accept its highlighted recommended model so the hidden terminal
            # reaches the composer; later launches remember the selection.
            if not accepted_recommended_model and not sent and "RECOMMENDED" in last_visible:
                if not models:
                    models = freebuff_model_options()[0]
                picker_models, focused = _freebuff_picker_options(last_visible, models)
                if (
                    self._model in picker_models
                    and focused >= 0
                    and picker_models[focused] == self._model
                ):
                    self._write("\r")
                    accepted_recommended_model = True
                    continue
                if not picker_expanded:
                    # Move from the recommended card to "See all models" and
                    # open it. The expanded picker is then navigated from its
                    # actual runtime ordering, so model catalog changes do not
                    # require a BlindPilot update.
                    self._write("\x1b[B")
                    time.sleep(0.05)
                    self._write("\r")
                    picker_expanded = True
                    picker_expanded_at = time.monotonic()
                    continue
                if self._model in picker_models and focused >= 0:
                    target = picker_models.index(self._model)
                    arrow = "\x1b[B" if target > focused else "\x1b[A"
                    for _step in range(abs(target - focused)):
                        self._write(arrow)
                        time.sleep(0.05)
                    self._write("\r")
                    accepted_recommended_model = True
                    continue
                if time.monotonic() - picker_expanded_at >= 5:
                    self._on_failed(f"FreeBuff's model picker did not offer {self._model}")
                    return

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
                previous_thinking, previous_answer = self._freebuff_sections(last_visible)
                spoken_thinking = _complete_sentences(_unwrap_screen_text(previous_thinking))
                spoken_answer = _complete_sentences(_unwrap_screen_text(previous_answer))
                self._accepting_input.set()
                self._on_started()
                continue

            now = time.monotonic()
            if sent and now >= next_session_check:
                next_session_check = now + 1.0
                if chat_path is None:
                    after = _freebuff_chat_dirs(self._cwd)
                    new_ids = set(after) - set(before)
                    discovered = (
                        max(new_ids, key=lambda chat_id: after[chat_id])
                        if new_ids
                        else self._session_id
                    )
                    if discovered:
                        self._session_id = discovered
                        chat_path = _freebuff_chat_path(self._cwd, discovered)
                        log_offset = 0
                if self._session_id and not session_reported:
                    self._on_session(self._session_id)
                    session_reported = True

            if sent and chat_path is not None:
                # Parsing the chat costs far more than looking at it, and it is
                # looked at many times a second, so it is only read when
                # FreeBuff has actually written to it.
                stamp = _freebuff_chat_stamp(chat_path)
                if stamp != chat_stamp:
                    chat_stamp = stamp
                    answer_id, _thinking, answer, agents = _freebuff_chat_snapshot(chat_path)
                    if answer_id and answer_id == baseline_answer_id:
                        # Still the answer this turn was resumed from; nothing
                        # of this turn has been written yet.
                        answer_id, answer, agents = "", "", []
                    elif answer_id and answer_id != structured_answer_id:
                        # FreeBuff opened this turn's answer. Everything tracked
                        # so far belonged to an earlier message.
                        structured_answer_id = answer_id
                        baseline_answer_id = ""
                        agent_states.clear()
                    # The file is written whole, once the reply is finished, so
                    # it is the authoritative text of the answer rather than a
                    # source to read from: the reading happens off the screen.
                    if answer:
                        structured_answer = answer
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
                    elif now - completion_seen_at >= 0.2:
                        break

            if sent and now >= next_heartbeat:
                elapsed = max(1, int(now - (turn_started_at or now)))
                self._on_activity("tool", f"FreeBuff is still working; {elapsed} seconds elapsed")
                next_heartbeat = now + 30
            # The screen is the only place the answer appears as it is written:
            # the chat file is not saved until the reply is finished. So the
            # reading follows the terminal, one finished sentence at a time.
            # A frame is taken once it has held still for a moment, or, if the
            # text is arriving faster than that, as soon as the wait would start
            # to be audible.
            if started and screen_dirty:
                settled = now - screen_changed_at >= _FREEBUFF_FRAME_SECONDS
                overdue = now - last_emit_at >= _FREEBUFF_MAX_LAG_SECONDS
                if settled or overdue:
                    thinking, answer = self._freebuff_sections(last_visible)
                    spoken_thinking = self._emit_screen_delta("thinking", spoken_thinking, thinking)
                    spoken_answer = self._emit_screen_delta("assistant", spoken_answer, answer)
                    screen_dirty = False
                    last_emit_at = now
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
        # The turn ends holding the last sentence, which never became "finished
        # text" while the answer was still growing. Nothing arrives after this,
        # so release the rest of the frame whole.
        screen_thinking, screen_response = self._freebuff_sections(last_visible)
        self._emit_screen_delta("thinking", spoken_thinking, screen_thinking, whole=True)
        self._emit_screen_delta("assistant", spoken_answer, screen_response, whole=True)
        # The saved chat is the answer as FreeBuff wrote it, rather than as the
        # terminal laid it out, so it is what the transcript keeps.
        response = structured_answer or _unwrap_screen_text(screen_response)
        if response:
            # An answer taller than the terminal scrolls its own beginning off
            # the screen, and the reading stops there. The saved chat is the
            # whole of it, so whatever was never read is read now.
            tail = _unspoken_tail(self._narrated.get("assistant", ""), response)
            if tail:
                self._on_activity("assistant", tail)
            self._on_complete(response)
        else:
            self._on_failed("No response received from FreeBuff")
            return

        after = _freebuff_chat_dirs(self._cwd)
        new_ids = set(after) - set(before)
        session = max(new_ids, key=lambda chat_id: after[chat_id]) if new_ids else self._session_id
        if session and not session_reported:
            self._on_session(session)
        # The next message in this conversation should not have to wait for a
        # terminal to start, so start one now, while nobody is waiting on it.
        if session:
            prewarm_freebuff(self._cwd, session, self._model, delay=1.0)

    def _emit_screen_delta(self, kind: str, spoken: str, current: str, whole: bool = False) -> str:
        """Read out whatever the screen has gained since the last frame.

        Only finished sentences are released while the answer is still being
        written: the live edge of a frame is a half-written word, and half a
        sentence read aloud is what makes a run sound broken. At the end of the
        turn nothing more is coming, so the rest is released as it stands.
        """
        text = _unwrap_screen_text(current)
        ready = text if whole else _complete_sentences(text)
        if not ready:
            return spoken
        addition = _append_delta(spoken, ready)
        unrelated = bool(spoken) and addition == ready and len(ready) < len(spoken)
        if unrelated and not whole:
            # This frame shares nothing with what was read and holds less of it,
            # which is what a half-drawn repaint looks like. Wait to see it
            # again before reading it, rather than read a torn frame aloud —
            # but do not wait forever, because it is also what the start of a
            # genuinely shorter answer looks like.
            if self._pending_frame.get(kind) != ready:
                self._pending_frame[kind] = ready
                return spoken
        self._pending_frame.pop(kind, None)
        addition = addition.strip()
        if addition:
            self._on_activity(kind, addition)
            narrated = self._narrated.get(kind, "")
            self._narrated[kind] = f"{narrated}\n{addition}" if narrated else addition
        return ready

    def _spawn_pty(self, args: list[str]) -> Callable[[float], str]:
        self._pty, read = _spawn_freebuff_pty(args, self._cwd, self._stream_ended)
        return read

    def _freebuff_sections(self, visible: str) -> tuple[str, str]:
        """Extract reasoning and answer text from FreeBuff's rendered screen."""
        raw_lines = _strip_terminal_noise(visible).splitlines()
        # This turn's output is whatever follows the echo of this turn's prompt.
        # A resumed conversation paints the turn before it above that, reasoning
        # and all, and none of that is an answer to what was just asked.
        needle = next((line.strip() for line in self._prompt.splitlines() if line.strip()), "")[:60]
        prompt_index = -1
        if needle:
            for index, raw in enumerate(raw_lines):
                if needle in raw:
                    prompt_index = index
        start = prompt_index + 1 if prompt_index >= 0 else 0

        thinking_index = -1
        thinking_indent = 0
        for index in range(start, len(raw_lines)):
            raw = raw_lines[index]
            if re.fullmatch(r"\s*[•*]\s*Thinking\s*", raw, re.IGNORECASE):
                thinking_index = index
                thinking_indent = len(raw) - len(raw.lstrip())

        if thinking_index >= 0:
            candidates = raw_lines[thinking_index + 1 :]
            in_thinking = True
        elif prompt_index >= 0:
            candidates = raw_lines[start:]
            in_thinking = False
        else:
            # The prompt has scrolled off the top, so there is no way to tell
            # this turn's output from the conversation above it. What is missed
            # here is read out from the saved chat once the turn ends.
            candidates = []
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
            # The status bar sits between the answer and the composer, and
            # carries the model, what is left of the session, and the control
            # that ends it. None of that is the answer.
            if lower.endswith("end session") or re.search(
                r"\bunlimited\b.*(?:end session|[✕x])", lower
            ):
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


class AgentWorker(Protocol):
    """The part of a backend's worker that the window actually drives.

    All three workers are threads, but a thread is not what the window wants
    from them: it wants to start a turn, ask whether it is still running, stop
    it, and wait for it to let go. Saying that here is what lets the window
    hold whichever worker the backend chose without knowing which one it is —
    and lets `cancel` be a method the code is allowed to call, rather than one
    a reader has to take on trust.
    """

    def start(self) -> None: ...

    def is_alive(self) -> bool: ...

    def join(self, timeout: Optional[float] = None) -> None: ...

    def cancel(self) -> None: ...


# Callable rather than type[...]: each backend's worker takes the keywords its
# own turn needs — Codex alone understands `compact` — and the caller passes
# the extras for the backend it picked.
AgentWorkerFactory = Callable[..., AgentWorker]


def worker_class(backend: str, claude_worker: AgentWorkerFactory) -> AgentWorkerFactory:
    backend = normalize_backend(backend)
    if backend == BACKEND_CODEX:
        return CodexWorker
    if backend == BACKEND_FREEBUFF:
        return FreebuffWorker
    if backend == BACKEND_HERMES:
        # Imported here rather than at module scope so a machine without Hermes
        # pays nothing for it, and an import error in the adapter cannot stop
        # the other three backends from working.
        from hermes_worker import HermesWorker

        return HermesWorker
    return claude_worker
