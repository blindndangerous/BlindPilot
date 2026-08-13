"""BlindPilot — accessible wxPython frontend for coding-agent CLIs.

Based on the original Claude Code Reader application. BlindPilot retains the
original application's accessibility-first design while adding pluggable
Claude Code, Codex, and FreeBuff backends.

Copyright (c) 2026 doubletaponair and BlindPilot contributors.
SPDX-License-Identifier: MIT

Uses wxPython so the UI is built from native widgets per platform — on macOS
the responses list is a real NSTableView (the same widget Finder uses), which
VoiceOver reads cleanly with no interaction quirks. On Windows the same code
uses Win32 widgets that NVDA / JAWS handle natively.

v2 segments each assistant turn into navigable *rows* (a header, one row per
paragraph / heading / list / quote, and one pristine row per fenced code block)
via the keystone parser in ``markdown_rows``. The flat list of rows sits above
the prompt box; arrowing Up from the prompt enters the newest row, while arrow
keys at either end of the list stay in the list. Tab is the only navigation key
that moves from the responses into the prompt.

Multi-session: the main window hosts a notebook with one tab per conversation.
Each tab owns its own conversation (session_id, prompt, rows) and its subprocess
runs with that directory as cwd, mirroring how a user would open multiple
terminal sessions in different project folders.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, List, Optional

import wx

from app_updater import (
    ReleaseInfo,
    UpdateError,
    clear_pending_failure,
    download_update,
    fetch_latest_release,
    pending_failure,
    schedule_install,
    sweep_temporary_files,
    version_tuple,
)
from agent_backends import (
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    BACKEND_FREEBUFF,
    BACKEND_IDS,
    BACKEND_LABELS,
    BACKENDS,
    FREEBUFF_PREFERRED_MODEL,
    backend_auth_ok,
    backend_label,
    blindpilot_config_dir,
    codex_model_options,
    compaction_request,
    discard_freebuff_prewarm,
    find_backend_cli,
    freebuff_model_options,
    invalidate_backend_cache,
    normalize_backend,
    prewarm_freebuff,
    reserve_hidden_console,
    set_freebuff_model,
    worker_class,
)

from markdown_rows import (
    Row,
    _strip_noise,
    parse_response,
    reassemble,
    reassemble_all,
)
from session_history import (
    HistoryEntry,
    HistoryTurn,
    describe_age,
    list_history,
    load_turns,
)

# Optional macOS-only path for posting NSAccessibility announcements so
# VoiceOver speaks a label when focus enters fields it would otherwise
# silently land on (notably the multi-line prompt TextCtrl, whose name set
# via wx.SetName lands on the outer NSScrollView rather than the focused
# NSTextView).
if platform.system() == "Darwin":
    try:
        from AppKit import (  # type: ignore
            NSApp,
            NSAccessibilityPostNotificationWithUserInfo,
            NSAccessibilityAnnouncementRequestedNotification,
            NSAccessibilityAnnouncementKey,
            NSAccessibilityPriorityKey,
            NSAccessibilityPriorityHigh,
        )

        _MAC_ANNOUNCE = True
    except ImportError:
        _MAC_ANNOUNCE = False
else:
    _MAC_ANNOUNCE = False

# Windows has no equivalent of the NSAccessibility announcement API, and neither
# NVDA nor JAWS speaks a status-bar change on its own. accessible_output2 talks
# to whichever reader is running (NVDA controller client, JAWS COM, SAPI as a
# last resort), which is what makes live narration audible on Windows.
_SPEAKER = None
if platform.system() == "Windows":
    try:
        from accessible_output2.outputs.auto import Auto as _AutoOutput  # type: ignore

        _SPEAKER = _AutoOutput()
    except Exception:  # library missing, or no usable output found
        _SPEAKER = None


def announce(text: str) -> None:
    """Speak `text` via the screen reader without stealing focus.

    macOS uses the NSAccessibility announcement API; Windows goes through
    accessible_output2. Callers also mirror the message to the status bar so
    there is a fallback the review cursor can reach.
    """
    if _SPEAKER is not None:
        try:
            # interrupt=False so a long narration is queued behind whatever the
            # reader is already saying instead of chopping it off.
            _SPEAKER.speak(text, interrupt=False)
        except Exception:
            pass
        return
    if not _MAC_ANNOUNCE:
        return
    app = NSApp()
    if app is None:
        return
    window = app.keyWindow() or app.mainWindow()
    if window is None:
        return
    info = {
        NSAccessibilityAnnouncementKey: text,
        NSAccessibilityPriorityKey: NSAccessibilityPriorityHigh,
    }
    NSAccessibilityPostNotificationWithUserInfo(
        window,
        NSAccessibilityAnnouncementRequestedNotification,
        info,
    )


APP_NAME = "BlindPilot"
APP_VERSION = "0.3.12"

# Streamed coding-agent output can arrive much faster than a native list and a
# screen reader can consume it. Process a bounded number of events per GUI turn
# so keyboard and accessibility events always get a chance to run, and redraw
# the responses control only once for each batch.
_WORKER_EVENT_BATCH_SIZE = 16
_WORKER_EVENT_BUDGET_SECONDS = 0.02
ORIGINAL_APP_CREDIT = (
    "Based on the original Claude Code Reader application by doubletaponair.\n"
    "https://github.com/doubletaponair/claude-code-reader"
)
CLAUDE_BIN = "claude"


# Common install locations to check when `claude` isn't on PATH.
# macOS GUI apps launched from Finder/Dock inherit a minimal PATH
# (/usr/bin:/bin:/usr/sbin:/sbin) and miss Homebrew, nvm, the official
# ~/.claude/local installer, etc. Windows GUI apps usually inherit the
# user PATH, but installs to non-default npm prefixes can still miss it.
def _native_bin_dir() -> Path:
    """Where the official native installer puts the launcher, every platform."""
    return Path.home() / ".local" / "bin"


def _fallback_claude_paths() -> tuple[Path, ...]:
    home = Path.home()
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA", str(home / "AppData" / "Roaming"))
        local_appdata = os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))
        candidates: list[Path] = []
        for name in ("claude.exe", "claude.cmd", "claude.ps1"):
            candidates.extend(
                [
                    # Native installer (install.ps1 / install.cmd) — the default.
                    _native_bin_dir() / name,
                    # WinGet's shim directory.
                    Path(local_appdata) / "Microsoft" / "WinGet" / "Links" / name,
                    Path(appdata) / "npm" / name,
                    home / ".claude" / "local" / name,
                    home / ".volta" / "bin" / name,
                    Path(local_appdata) / "Programs" / "claude" / name,
                ]
            )
        return tuple(candidates)
    return (
        _native_bin_dir() / "claude",
        home / ".claude" / "local" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
        home / ".npm-global" / "bin" / "claude",
        home / ".volta" / "bin" / "claude",
    )


def _login_shell() -> Optional[str]:
    """The user's real login shell, if there is one (POSIX only)."""
    if platform.system() == "Windows":
        return None
    shell = os.environ.get("SHELL")
    return shell if shell and os.path.isfile(shell) else None


def _login_shell_which(name: str) -> Optional[str]:
    """Resolve *name* the way a fresh Terminal window would.

    A GUI app launched from Finder or the Dock inherits a minimal PATH, so this
    is both how we find a CLI the user can run and how we tell whether their
    shell startup files would find it. `command -v` is POSIX and also works in
    fish, so this covers zsh, bash and fish alike.
    """
    shell = _login_shell()
    if shell is None:
        return None
    try:
        result = subprocess.run(
            [shell, "-l", "-c", f"command -v {name}"],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    path = result.stdout.strip().splitlines()[-1].strip()
    if path and os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    return None


def _find_claude() -> Optional[str]:
    """Locate the `claude` binary even when launched from a GUI app.

    Order: PATH, well-known install locations, then (POSIX only) the user's
    login shell so any custom PATH from .zprofile / .bash_profile is honored.
    """
    binary = shutil.which(CLAUDE_BIN)
    if binary:
        return binary

    for candidate in _fallback_claude_paths():
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    return _login_shell_which(CLAUDE_BIN)


# ---------------------------------------------------------------------------
# Installing Claude Code, and making it visible to every shell.
#
# Both official native installers are documented at code.claude.com/docs/en/setup.
# Neither needs administrator rights or Node.js: they drop a real binary in
# ~/.local/bin and self-update from then on.
# ---------------------------------------------------------------------------

WINDOWS_INSTALL_PS1_URL = "https://claude.ai/install.ps1"
POSIX_INSTALL_SH_URL = "https://claude.ai/install.sh"

# CREATE_NO_WINDOW: without it every helper process flashes a console window,
# which also steals focus away from the screen reader mid-install.
_NO_WINDOW = 0x08000000 if platform.system() == "Windows" else 0


def _no_window_kwargs() -> dict:
    return {"creationflags": _NO_WINDOW} if _NO_WINDOW else {}


def _same_dir(a: str, b: str) -> bool:
    """Compare two directory strings the way the platform resolves them.

    ``normcase`` folds case and slashes on Windows and is a no-op on POSIX,
    which is what we want: PATH is case-insensitive on one and not the other.
    ``$HOME`` / ``%USERPROFILE%`` style references are expanded, since PATH
    entries are routinely written that way and comparing them literally would
    append a duplicate entry on every launch.
    """

    def norm(p: str) -> str:
        p = os.path.expandvars(os.path.expanduser(p.strip().strip('"')))
        return os.path.normcase(os.path.normpath(p))

    return bool(a.strip()) and norm(a) == norm(b)


def _bundle_dir() -> Optional[str]:
    """The folder a packaged build keeps its own libraries in, if this is one."""
    if not getattr(sys, "frozen", False):
        return None
    return getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(sys.executable))


def path_without_bundle_entries(current: str, bundle: str) -> str:
    """*current* PATH with every entry inside the packaged folder removed."""

    def inside(entry: str) -> bool:
        try:
            candidate = os.path.normcase(
                os.path.normpath(os.path.abspath(entry.strip().strip('"')))
            )
        except (OSError, ValueError):
            return False
        root = os.path.normcase(os.path.normpath(os.path.abspath(bundle)))
        return candidate == root or candidate.startswith(root + os.sep)

    kept = [entry for entry in current.split(os.pathsep) if entry.strip() and not inside(entry)]
    return os.pathsep.join(kept)


def keep_bundle_off_child_path() -> None:
    """Stop BlindPilot's private DLL folder from reaching child processes.

    PyInstaller's pywin32 hook puts ``_internal\\pywin32_system32`` on this
    process's PATH. It registers the same folder with ``os.add_dll_directory``,
    which is what actually makes pywin32 load; the PATH entry is only a
    fallback for Anaconda builds where that call does nothing. Unlike the DLL
    directory, PATH is inherited — by the agent CLI, by the terminal, and by
    everything those start in turn, for as long as any of them live.

    Those processes then resolve ordinary libraries (the Visual C++ runtime,
    pythoncom) out of BlindPilot's install folder and hold them open. The next
    update finds its own files in use by programs it has no business closing,
    and the installer gives up rather than replace them — the silent "installer
    exited with code 5" this used to end in.
    """
    bundle = _bundle_dir()
    if not bundle:
        return
    current = os.environ.get("PATH", "")
    cleaned = path_without_bundle_entries(current, bundle)
    if cleaned != current:
        os.environ["PATH"] = cleaned


def _windows_persistent_path_dirs() -> List[str]:
    """Every directory on the *persistent* PATH — user PATH plus system PATH.

    This is what a freshly opened cmd, PowerShell 5, pwsh or Windows Terminal
    tab composes its PATH from, which is not necessarily what this process
    inherited. Checking the registry rather than ``os.environ`` is the only way
    to know whether `claude` will actually resolve in a new terminal.
    """
    import winreg

    dirs: List[str] = []
    for root, subkey in (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
    ):
        try:
            with winreg.OpenKey(root, subkey) as key:
                value, _type = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        if isinstance(value, str):
            dirs.extend(p for p in value.split(os.pathsep) if p.strip())
    return dirs


def _posix_persistent_path_dirs() -> List[str]:
    """The PATH a fresh Terminal window would have.

    Asks the user's own login shell, so whatever their .zprofile / .zshrc /
    .bash_profile / fish config builds up is what we see — the same PATH they
    would get by opening Terminal or iTerm and typing `claude`. Printed one per
    line because in fish ``$PATH`` is a list, not a colon-joined string.
    """
    shell = _login_shell()
    if shell is None:
        return []
    if os.path.basename(shell) == "fish":
        # fish's $PATH is a real list, so this is already space-safe.
        script = "for p in $PATH; echo $p; end"
    else:
        # Split on the colon with tr rather than by word-splitting: PATH
        # entries containing spaces are normal on macOS (/Applications/...)
        # and word-splitting would shred them into fragments.
        script = 'printf \'%s\\n\' "$PATH" | tr ":" "\\n"'
    try:
        result = subprocess.run(
            [shell, "-l", "-c", script],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return [p for p in os.environ.get("PATH", "").split(":") if p.strip()]
    if result.returncode != 0:
        # A configured shell can be temporarily unusable (for example a stale
        # WSL launcher on Windows while exercising the macOS code path).  The
        # inherited POSIX PATH is still better evidence than an empty list.
        return [p for p in os.environ.get("PATH", "").split(":") if p.strip()]
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _is_on_persistent_path(directory: Path) -> bool:
    """Would a newly opened terminal find things in *directory*?

    Deliberately not a check of ``os.environ``: this process may have inherited
    a PATH that a fresh terminal will not have, or vice versa.
    """
    try:
        if platform.system() == "Windows":
            dirs = _windows_persistent_path_dirs()
        else:
            dirs = _posix_persistent_path_dirs()
            if not dirs:
                return True  # No usable login shell to ask — don't cry wolf.
        return any(_same_dir(p, str(directory)) for p in dirs)
    except Exception:
        return True  # Never block the user on a check we couldn't run.


def _broadcast_environment_change() -> None:
    """Tell Explorer (and everything else) that the environment changed.

    Without this broadcast a newly opened terminal still inherits Explorer's
    stale copy of the environment, so a PATH edit appears to do nothing until
    the user signs out and back in.
    """
    try:
        import ctypes
        from ctypes import wintypes

        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002

        send = ctypes.windll.user32.SendMessageTimeoutW
        send.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            ctypes.c_wchar_p,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(wintypes.DWORD),
        ]
        send.restype = wintypes.LPARAM
        result = wintypes.DWORD()
        send(
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            "Environment",
            SMTO_ABORTIFHUNG,
            5000,
            ctypes.byref(result),
        )
    except Exception:
        pass


def _add_to_process_path(directory: Path) -> None:
    """Make the directory usable in *this* process without a restart."""
    entry = str(directory)
    current = os.environ.get("PATH", "")
    if not any(_same_dir(p, entry) for p in current.split(os.pathsep) if p.strip()):
        os.environ["PATH"] = entry + os.pathsep + current


def _path_with_entry(current: str, directory: str) -> Optional[str]:
    """The PATH string *current* with *directory* appended, or None if present.

    Kept separate from the registry write so the string surgery — the part that
    can wreck someone's PATH — is testable on its own.
    """
    entries = [p for p in current.split(os.pathsep) if p.strip()]
    if any(_same_dir(p, directory) for p in entries):
        return None
    entries.append(directory)
    return os.pathsep.join(entries)


def _shell_profile_file() -> Path:
    """The startup file to extend for the user's login shell.

    zsh is the default on macOS since Catalina; ``.zshrc`` is read by both
    interactive login and non-login shells, so it is the one place that covers
    Terminal, iTerm and a shell opened inside an editor. Bash on macOS reads
    ``.bash_profile`` for login shells (which is what Terminal opens) while
    Linux desktops open non-login shells that read ``.bashrc``.
    """
    home = Path.home()
    shell = os.path.basename(_login_shell() or "")
    if shell == "zsh":
        return home / ".zshrc"
    if shell == "fish":
        return home / ".config" / "fish" / "config.fish"
    if shell == "bash":
        if platform.system() == "Darwin":
            return home / ".bash_profile"
        return home / ".bashrc"
    return home / ".profile"


PATH_STANZA_MARKER = "# Added by BlindPilot"
LEGACY_PATH_STANZA_MARKER = "# Added by Claude Code Reader"


def _path_export_line(directory: Path, shell: str) -> str:
    """The one line that puts *directory* on PATH for the given shell.

    Separators are forced to POSIX form — the line we are composing is shell
    script, not a host path, so it must read the same whatever built it.
    """
    # Written against $HOME rather than the expanded path, so the profile stays
    # portable and reads the way a person would have written it by hand.
    home = Path.home().as_posix()
    text = directory.as_posix()
    if text == home or text.startswith(home + "/"):
        text = "$HOME" + text[len(home) :]
    if shell == "fish":
        return f'fish_add_path "{text}"'
    return f'export PATH="{text}:$PATH"'


def ensure_on_posix_path(directory: Path) -> Optional[str]:
    """Add *directory* to PATH for future terminal sessions on macOS / Linux.

    Appends an export line to the login shell's startup file — the equivalent
    of the registry write on Windows, and the only way to affect terminals the
    user opens later. Returns the file that was changed, or None if nothing
    needed changing. Raises OSError if the write fails.
    """
    if _is_on_persistent_path(directory):
        return None

    profile = _shell_profile_file()
    line = _path_export_line(directory, os.path.basename(_login_shell() or ""))
    try:
        existing = profile.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    # Appending twice would leave a duplicate stanza in a file the user owns.
    if line in existing:
        return None

    profile.parent.mkdir(parents=True, exist_ok=True)
    with open(profile, "a", encoding="utf-8") as fh:
        # Lead with a newline: the file may not end with one, and appending to
        # a half-finished last line would corrupt it.
        fh.write(f"\n{PATH_STANZA_MARKER}\n{line}\n")
    return str(profile)


def ensure_on_path(directory: Path) -> Optional[str]:
    """Make *directory* reachable from a terminal, persistently.

    Returns a description of what was changed, or None if nothing needed
    changing. Raises OSError if the change could not be written.
    """
    _add_to_process_path(directory)
    if platform.system() == "Windows":
        return "your user PATH" if ensure_on_windows_path(directory) else None
    return ensure_on_posix_path(directory)


def ensure_on_windows_path(directory: Path) -> bool:
    """Append *directory* to the user's persistent PATH if it isn't there.

    Writes ``HKCU\\Environment``, which cmd, PowerShell 5.1, pwsh 7 and Windows
    Terminal all read when they start, so one entry covers every shell. `setx`
    is deliberately not used — it silently truncates PATH at 1024 characters.

    Returns True when an entry was added, False when it was already present.
    Raises OSError if the registry write fails.
    """
    _add_to_process_path(directory)
    if platform.system() != "Windows":
        return False

    import winreg  # after the platform check — the module is Windows-only

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Environment",
        0,
        winreg.KEY_READ | winreg.KEY_WRITE,
    ) as key:
        try:
            current, regtype = winreg.QueryValueEx(key, "Path")
        except OSError:
            current, regtype = "", winreg.REG_EXPAND_SZ
        if not isinstance(current, str):
            current, regtype = "", winreg.REG_EXPAND_SZ
        updated = _path_with_entry(current, str(directory))
        if updated is None:
            return False
        # Preserve REG_EXPAND_SZ when that's what's there: existing entries may
        # contain %USERPROFILE% and rewriting the value as REG_SZ would leave
        # those literal, breaking the rest of the user's PATH.
        if regtype not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
            regtype = winreg.REG_EXPAND_SZ
        winreg.SetValueEx(key, "Path", 0, regtype, updated)

    _broadcast_environment_change()
    return True


def _powershell_exe() -> Optional[str]:
    """Windows PowerShell first — it ships with Windows 11, pwsh may not."""
    for name in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _install_argv() -> Optional[List[str]]:
    """The command that runs the official native installer for this platform.

    None when the prerequisites aren't there — no PowerShell on Windows, no
    curl or shell on macOS / Linux.
    """
    if platform.system() == "Windows":
        shell = _powershell_exe()
        if shell is None:
            return None
        return [
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            f"irm {WINDOWS_INSTALL_PS1_URL} | iex",
        ]

    # macOS ships both curl and bash; a Linux box without curl is possible.
    if shutil.which("curl") is None:
        return None
    shell = shutil.which("bash") or shutil.which("sh")
    if shell is None:
        return None
    return [shell, "-c", f"curl -fsSL {POSIX_INSTALL_SH_URL} | bash"]


def _missing_prereq_message() -> str:
    if platform.system() == "Windows":
        return (
            "Could not find PowerShell on this computer, so the installer "
            "cannot be run automatically."
        )
    return (
        "Could not find curl and bash on this computer, so the installer "
        "cannot be run automatically."
    )


def _path_shells() -> str:
    """The shells worth naming when telling the user to open a new terminal."""
    if platform.system() == "Windows":
        return "cmd, PowerShell, pwsh and Windows Terminal"
    return "Terminal and iTerm"


def install_claude(log: Callable[[str], None]) -> Optional[str]:
    """Run the official native installer for this platform and put it on PATH.

    Streams installer output line by line to *log* (so the caller can show and
    speak progress) and returns the path to the installed binary, or None if
    the install did not produce a working `claude`.
    """
    argv = _install_argv()
    if argv is None:
        log(_missing_prereq_message())
        return None

    log("Downloading and running the Claude Code installer. This usually takes under a minute.")
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_no_window_kwargs(),
        )
    except OSError as exc:
        log(f"The installer could not be started: {exc}")
        return None

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log(line)
    rc = proc.wait()

    # The installer's own exit code is advisory — what matters is whether a
    # working binary exists afterwards, so look before reporting failure.
    _add_to_process_path(_native_bin_dir())
    binary = _find_claude()
    if binary is None:
        log(f"The installer finished with exit code {rc} but `claude` was not found afterwards.")
        return None

    log(f"Installed: {binary}")
    folder = Path(binary).parent
    try:
        changed = ensure_on_path(folder)
        if changed:
            log(
                f"Added {folder} to {changed}. Open a new terminal window for "
                f"{_path_shells()} to see it."
            )
        else:
            log(f"Already on your PATH — `claude` will work in {_path_shells()}.")
    except OSError as exc:
        log(f"Installed, but adding it to PATH failed: {exc}")
    return binary


_NPM_BACKEND_PACKAGES = {
    BACKEND_CODEX: "@openai/codex",
    BACKEND_FREEBUFF: "freebuff",
}


def _npm_install_argv(backend: str) -> Optional[List[str]]:
    """Return the npm command for a backend, or None if npm is unavailable."""
    package = _NPM_BACKEND_PACKAGES.get(normalize_backend(backend))
    npm = shutil.which("npm")
    if not package or not npm:
        return None
    return [npm, "install", "--global", package]


def _npm_update_argv(backend: str) -> Optional[List[str]]:
    """Return an npm update command pinned to the package's latest tag."""
    package = _NPM_BACKEND_PACKAGES.get(normalize_backend(backend))
    npm = shutil.which("npm")
    if not package or not npm:
        return None
    return [npm, "install", "--global", f"{package}@latest"]


def install_backend(backend: str, log: Callable[[str], None]) -> Optional[str]:
    """Install one selected backend and return its discovered executable."""
    backend = normalize_backend(backend)
    if backend == BACKEND_CLAUDE:
        return install_claude(log)
    argv = _npm_install_argv(backend)
    label = backend_label(backend)
    if argv is None:
        log(f"npm was not found, so BlindPilot cannot install {label} automatically.")
        return None
    log(f"Installing {label} with npm. This can take a minute.")
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_no_window_kwargs(),
        )
    except OSError as exc:
        log(f"The installer could not be started: {exc}")
        return None
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log(line)
    rc = proc.wait()
    binary = find_backend_cli(backend)
    if binary is None:
        log(f"npm finished with exit code {rc}, but {label} was not found afterwards.")
        return None
    log(f"Installed: {binary}")
    return binary


def _executable_version(binary: str) -> str:
    """Return one provider executable's own version text."""
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            **_no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return ((result.stdout or "") + (result.stderr or "")).strip()


def _version_tuple(text: str) -> tuple[int, ...]:
    match = re.search(r"\b(\d+(?:\.\d+)+)\b", text)
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def _repair_claude_native_update(binary: str, log: Callable[[str], None]) -> bool:
    """Make the Windows launcher use the newest downloaded Claude version."""
    if platform.system() != "Windows":
        return True
    versions = Path.home() / ".local" / "share" / "claude" / "versions"
    try:
        candidates = [
            path for path in versions.iterdir() if path.is_file() and _version_tuple(path.name)
        ]
    except OSError:
        return True
    if not candidates:
        return True
    newest = max(candidates, key=lambda path: _version_tuple(path.name))
    current_version = _version_tuple(_executable_version(binary))
    newest_version = _version_tuple(newest.name)
    if not newest_version or newest_version <= current_version:
        return True
    try:
        shutil.copy2(newest, binary)
    except OSError as exc:
        log(f"Claude downloaded {newest.name}, but its launcher could not be updated: {exc}")
        return False
    verified = _version_tuple(_executable_version(binary))
    if verified < newest_version:
        log("Claude's launcher still reports an older version after updating.")
        return False
    log(f"Activated Claude Code {newest.name} in the launcher.")
    return True


def update_backend(backend: str, log: Callable[[str], None]) -> bool:
    """Update an installed provider CLI and stream accessible progress."""
    backend = normalize_backend(backend)
    label = backend_label(backend)
    binary = _find_claude() if backend == BACKEND_CLAUDE else find_backend_cli(backend)
    if binary is None:
        log(f"{label} is not installed yet.")
        return False
    previous_freebuff_model = ""
    if backend == BACKEND_FREEBUFF:
        _models, _efforts, previous_freebuff_model, _effort, _error = freebuff_model_options()
    if backend == BACKEND_CLAUDE:
        argv = [binary, "update"]
    else:
        argv = _npm_update_argv(backend)
        if argv is None:
            log(f"npm was not found, so BlindPilot cannot update {label} automatically.")
            return False
    log(f"Checking for {label} updates...")
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_no_window_kwargs(),
        )
    except OSError as exc:
        log(f"The updater could not be started: {exc}")
        return False
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log(line)
    rc = proc.wait()
    if rc != 0:
        log(f"{label} update exited with code {rc}.")
        return False
    if backend == BACKEND_CLAUDE and not _repair_claude_native_update(binary, log):
        return False
    if backend == BACKEND_FREEBUFF:
        invalidate_backend_cache(BACKEND_FREEBUFF)
        models, _efforts, _current, _effort, _error = freebuff_model_options()
        selected = (
            previous_freebuff_model
            if previous_freebuff_model in models
            else FREEBUFF_PREFERRED_MODEL
            if FREEBUFF_PREFERRED_MODEL in models
            else models[0]
            if models
            else FREEBUFF_PREFERRED_MODEL
        )
        try:
            set_freebuff_model(selected)
        except OSError as exc:
            log(f"{label} updated, but its model selection could not be restored: {exc}")
            return False
    log(f"{label} is up to date.")
    return True


AUTH_ERROR_MARKERS = (
    "not logged in",
    "not authenticated",
    "please log in",
    "please login",
    "run /login",
    "run `claude /login`",
    "invalid api key",
    "unauthorized",
    "401",
    "no credentials",
    "missing credentials",
    "authentication required",
    "auth required",
    "oauth token",
)
AUTH_HINT = "Not signed in — run `claude /login` in a terminal, then try again."


def _check_auth_quick(binary: str) -> bool:
    """Returns True if authenticated (or timed out = probably working), False on auth error."""
    try:
        result = subprocess.run(
            [binary, "-p", "x", "--output-format", "stream-json"],
            capture_output=True,
            text=True,
            timeout=12,
            stdin=subprocess.DEVNULL,
            **_no_window_kwargs(),
        )
        combined = (result.stdout + result.stderr).lower()
        return not any(m in combined for m in AUTH_ERROR_MARKERS)
    except subprocess.TimeoutExpired:
        return True
    except OSError:
        return False


# ----- /model: what the CLI currently offers -----
# Nothing here is hard-coded as truth: the model aliases and effort levels are
# read back from the installed CLI every time the dialog opens, because both
# lists change as Claude Code ships new models. These constants are only the
# last-resort fallback for when the probe fails (offline, CLI missing, output
# format changed).
_FALLBACK_MODELS = ["default", "opus", "sonnet", "haiku", "fable", "opusplan"]
_FALLBACK_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
# Shown first in both combo boxes: leave the flag off and let the CLI decide.
DEFAULT_CHOICE = "(CLI default)"
# Probing costs a CLI start-up, so results are reused for a while. Catalogs are
# deliberately loaded only when /model or /models is opened, keeping normal
# application startup fast and quiet.
PROBE_TTL_SECONDS = 900


def _keep_choice(current: str) -> str:
    """First combo-box entry: pass no flag, and say what that currently means."""
    return f"{DEFAULT_CHOICE} — currently {current}" if current else DEFAULT_CHOICE


@dataclass
class ModelOptions:
    """What `claude` reports it can be asked for, plus what it is using now."""

    models: List[str]
    efforts: List[str]
    current_model: str = ""  # display name, e.g. "Opus 5"
    current_effort: str = ""  # e.g. "medium"
    error: str = ""  # non-empty when the probe fell back to defaults
    from_cache: bool = False  # served from a recent probe, not a fresh one


def _parse_model_aliases(text: str) -> List[str]:
    """Model names out of the CLI's `/model` usage line.

    The line looks like::

        Usage: /model <name>. Available: sonnet, opus, ..., or a full model ID.
    """
    match = re.search(r"Available:\s*(.+)", text, re.I)
    if not match:
        return []
    tail = match.group(1).strip().rstrip(".")
    names: List[str] = []
    for part in tail.split(","):
        name = part.strip().rstrip(".")
        # Drop the trailing prose ("or a full model ID") and any stray blanks;
        # every real alias or model ID is a single word.
        if not name or " " in name:
            continue
        if name not in names:
            names.append(name)
    return names


def _parse_current_model(text: str) -> tuple[str, str]:
    """(display name, effort) from the CLI's `Current model:` status line."""
    match = re.search(r"Current model:\s*([^\n(]+)(?:\(effort:\s*([^)]*)\))?", text, re.I)
    if not match:
        return "", ""
    return match.group(1).strip(), (match.group(2) or "").strip()


def _parse_effort_levels(help_text: str) -> List[str]:
    """Effort levels out of the `--effort <level>` entry in `claude --help`.

    The help text is hard-wrapped, so it is flattened before matching:
    ``--effort <level> Effort level for the current session (low, medium, …)``.
    """
    flat = " ".join(help_text.split())
    match = re.search(r"--effort <level>(.*?)(?=\s--\w|$)", flat)
    if not match:
        return []
    inner = re.search(r"\(([^)]*)\)", match.group(1))
    if not inner:
        return []
    levels = [p.strip() for p in inner.group(1).split(",")]
    return [lv for lv in levels if lv and " " not in lv]


def _run_claude(binary: str, args: List[str], cwd: Optional[str], timeout: int) -> str:
    """Run the CLI and return stdout+stderr, or "" if it could not be run."""
    try:
        result = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            **_no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout or "") + (result.stderr or "")


_probe_lock = threading.Lock()
# (cwd, cli stamp) -> (when it was probed, what came back). Keyed by the CLI's
# path+mtime+size so upgrading Claude Code invalidates everything at once.
_probe_cache: dict[tuple[str, str], tuple[float, ModelOptions]] = {}


def invalidate_model_options(backend: str | None = None) -> None:
    """Clear model catalogs after an update or an explicit `/models` refresh."""
    selected = normalize_backend(backend) if backend is not None else None
    with _probe_lock:
        if selected is None:
            _probe_cache.clear()
        else:
            prefix = f"{selected}:"
            for key in [key for key in _probe_cache if key[0].startswith(prefix)]:
                _probe_cache.pop(key, None)
    invalidate_backend_cache(selected)


def _cli_stamp(binary: str) -> str:
    try:
        st = os.stat(binary)
        return f"{binary}|{int(st.st_mtime)}|{st.st_size}"
    except OSError:
        return binary


def cached_model_options(
    cwd: Optional[str], max_age: float, backend: str = BACKEND_CLAUDE
) -> Optional[ModelOptions]:
    """A probe result no older than `max_age` seconds, or None. Never blocks."""
    backend = normalize_backend(backend)
    binary = _find_claude() if backend == BACKEND_CLAUDE else find_backend_cli(backend)
    if binary is None or max_age <= 0:
        return None
    key = (f"{backend}:{cwd or ''}", _cli_stamp(binary))
    with _probe_lock:
        entry = _probe_cache.get(key)
    if entry is None or (time.time() - entry[0]) > max_age:
        return None
    return replace(entry[1], from_cache=True)


def probe_model_options(
    cwd: Optional[str] = None,
    max_age: float = 0,
    backend: str = BACKEND_CLAUDE,
) -> ModelOptions:
    """Ask the installed CLI which models and effort levels it accepts.

    Also reports the model and effort the CLI says it is using right now. Pass
    `max_age` to accept a recent cached answer instead of shelling out — with
    0 (the default) it always asks the CLI, which costs a CLI start-up, so this
    is blocking: call it off the GUI thread.
    """
    backend = normalize_backend(backend)
    binary = _find_claude() if backend == BACKEND_CLAUDE else find_backend_cli(backend)
    if binary is None:
        label = backend_label(backend)
        return ModelOptions(
            list(_FALLBACK_MODELS) if backend == BACKEND_CLAUDE else [],
            list(_FALLBACK_EFFORTS) if backend != BACKEND_FREEBUFF else [],
            error=f"{label} was not found.",
        )

    fresh = cached_model_options(cwd, max_age, backend)
    if fresh is not None:
        return fresh

    if backend == BACKEND_CODEX:
        models, efforts, current_model, current_effort, error = codex_model_options(cwd)
        options = ModelOptions(models, efforts, current_model, current_effort, error)
        if models:
            with _probe_lock:
                _probe_cache[(f"{backend}:{cwd or ''}", _cli_stamp(binary))] = (
                    time.time(),
                    options,
                )
        return options

    if backend == BACKEND_FREEBUFF:
        models, efforts, current_model, current_effort, error = freebuff_model_options()
        return ModelOptions(models, efforts, current_model, current_effort, error)

    # The two probes are independent, so the help text is fetched while the
    # slower `/model` status call is still running.
    help_text: List[str] = []
    help_thread = threading.Thread(
        target=lambda: help_text.append(_run_claude(binary, ["--help"], None, 30)),
        daemon=True,
    )
    help_thread.start()
    # `/model` with no argument only prints status — it does not start a turn.
    status = _run_claude(binary, ["-p", "/model", "--output-format", "text"], cwd, 45)
    models = _parse_model_aliases(status)
    current_model, current_effort = _parse_current_model(status)
    help_thread.join(30)
    efforts = _parse_effort_levels(help_text[0] if help_text else "")

    problems = []
    if not models:
        models = list(_FALLBACK_MODELS)
        problems.append("model list")
    if not efforts:
        efforts = list(_FALLBACK_EFFORTS)
        problems.append("effort levels")
    error = ""
    if problems:
        error = f"Could not read the {' and '.join(problems)} from Claude Code; showing the built-in list."
    options = ModelOptions(models, efforts, current_model, current_effort, error)
    if not problems:
        # Only a clean answer is worth reusing; a failed probe should be retried.
        with _probe_lock:
            _probe_cache[(f"{backend}:{cwd or ''}", _cli_stamp(binary))] = (
                time.time(),
                options,
            )
    return options


# BlindPilot's provider-neutral permission choices. Adapters translate these
# values to each backend's native approval and sandbox controls.
PERMISSION_MODES = [
    (
        "default",
        "Default",
        "Default mode. The selected backend uses its normal approval policy.",
    ),
    (
        "acceptEdits",
        "Accept edits",
        "Accept edits mode. File edits are accepted, while other actions keep "
        "the backend's normal safeguards.",
    ),
    (
        "plan",
        "Plan",
        "Plan mode. The backend can read and explore, but cannot edit your code.",
    ),
    (
        "auto",
        "Auto",
        "Auto mode. The backend works inside its workspace sandbox without "
        "stopping for routine approvals.",
    ),
    (
        "dontAsk",
        "Don't ask",
        "Don't ask mode. Approval prompts are declined instead of interrupting the run.",
    ),
    (
        "bypassPermissions",
        "Bypass permissions",
        "Bypass permissions mode. The backend runs without approval or sandbox "
        "checks. Use only in an isolated environment.",
    ),
]
# The quick-cycle chord steps through the everyday subset; the rest stay
# reachable via the dropdown.
# File extension to suggest when saving a code row, keyed by its display name.
_LANG_EXT = {
    "Python": ".py",
    "JavaScript": ".js",
    "TypeScript": ".ts",
    "Shell": ".sh",
    "Bash": ".sh",
    "Zsh": ".sh",
    "JSON": ".json",
    "YAML": ".yaml",
    "HTML": ".html",
    "CSS": ".css",
    "SQL": ".sql",
    "C": ".c",
    "C++": ".cpp",
    "C#": ".cs",
    "Go": ".go",
    "Rust": ".rs",
    "Java": ".java",
    "Ruby": ".rb",
    "PHP": ".php",
    "Swift": ".swift",
    "Kotlin": ".kt",
    "Markdown": ".md",
    "XML": ".xml",
    "TOML": ".toml",
    "Diff": ".diff",
    "Plain text": ".txt",
}

# Slash commands the user can pick from the slash-command picker. Commands
# marked [BlindPilot] are handled by the frontend; the rest are provider-only.
_BLINDPILOT_SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/btw [message]", "Open a side-chat tab in this directory [BlindPilot]"),
    ("/clear", "Start a fresh conversation in this tab [BlindPilot]"),
    ("/compact", "Summarise this conversation to free up context [BlindPilot]"),
    ("/exit", "Close this session tab [BlindPilot]"),
    ("/model", "Pick the model and effort level in a dialog [BlindPilot]"),
    ("/models", "Refresh and pick a model in a dialog [BlindPilot]"),
    ("/model [model-id]", "Switch straight to a model [BlindPilot]"),
    ("/resume", "Reopen a past conversation in a new tab [BlindPilot]"),
]

_CLAUDE_SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/compact [instructions]", "Compact with custom summary instructions"),
    ("/cost", "Show token usage and cost for this session"),
    ("/init", "Create or update CLAUDE.md in the current directory"),
    ("/login", "Switch Claude account or re-authenticate"),
    ("/logout", "Sign out of Claude"),
    ("/memory", "Open memory files in the editor"),
    ("/pr_comments", "View pull request comments"),
    ("/release-notes", "Show Claude Code release notes"),
    ("/review", "Review a file or directory"),
    ("/status", "Show account and subscription status"),
]

_FREEBUFF_SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/new", "Start a new FreeBuff conversation [BlindPilot]"),
    ("/history", "Open FreeBuff conversation history"),
    ("/diagnostics", "Show FreeBuff's resource usage and tool processes"),
    ("/init", "Create project instructions"),
    ("/usage", "Show FreeBuff credit usage"),
    ("/review", "Review the current changes"),
    ("/plan", "Plan before making changes"),
    ("/theme:toggle", "Toggle FreeBuff's terminal theme"),
    ("/logout", "Sign out of FreeBuff"),
]


def _slash_commands_for_backend(backend: str) -> list[tuple[str, str]]:
    commands = list(_BLINDPILOT_SLASH_COMMANDS)
    backend = normalize_backend(backend)
    if backend == BACKEND_CLAUDE:
        commands.extend(_CLAUDE_SLASH_COMMANDS)
    elif backend == BACKEND_FREEBUFF:
        commands.extend(_FREEBUFF_SLASH_COMMANDS)
    return commands


_CYCLE_VALUES = ["default", "acceptEdits", "plan"]
_MODE_LABELS = [label for _v, label, _d in PERMISSION_MODES]
_MODE_VALUES = [value for value, _l, _d in PERMISSION_MODES]
_MODE_DESCRIPTIONS = {value: desc for value, _l, desc in PERMISSION_MODES}


def _claude_settings_files(cwd: str) -> List[Path]:
    """Claude Code's own settings files, lowest precedence first.

    Same order the CLI itself uses: your user settings, then the project's
    checked-in settings, then the project's local (git-ignored) settings.
    """
    user_dir = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    project = Path(cwd) / ".claude"
    return [
        Path(user_dir) / "settings.json",
        project / "settings.json",
        project / "settings.local.json",
    ]


def _claude_config_permission_mode(cwd: str) -> str:
    """``permissions.defaultMode`` from Claude Code's settings, or "" if unset."""
    found = ""
    for path in _claude_settings_files(cwd):
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        perms = data.get("permissions")
        if not isinstance(perms, dict):
            continue
        mode = perms.get("defaultMode")
        if isinstance(mode, str) and mode in _MODE_VALUES:
            found = mode  # later file wins
    return found


def _default_permission_mode(cwd: str, backend: str = BACKEND_CLAUDE) -> str:
    """The mode a new session tab starts in.

    Your last choice in this app wins, because it was made deliberately.
    Failing that we use whatever Claude Code itself is configured to use, so
    the app matches the CLI instead of always starting at "default".
    """
    saved = _load_config().get("permission_mode")
    if isinstance(saved, str) and saved in _MODE_VALUES:
        return saved
    if normalize_backend(backend) == BACKEND_CLAUDE:
        return _claude_config_permission_mode(cwd) or "default"
    return "default"


def _remember_permission_mode(value: str) -> None:
    """Persist a mode change so it survives restarts and new tabs."""
    if value not in _MODE_VALUES:
        return
    cfg = _load_config()
    if cfg.get("permission_mode") == value:
        return
    cfg["permission_mode"] = value
    _save_config(cfg)


def _looks_like_auth_error(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in AUTH_ERROR_MARKERS)


def _short_label(path: str) -> str:
    """Tab label: directory basename, or full path if at the filesystem root."""
    name = Path(path).name
    return name or path


def _tab_title(text: str, limit: int = 32) -> str:
    """Tab label for a resumed conversation: its first message, cut to fit."""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def _tool_use_label(name: str, params: dict) -> str:
    """One spoken line describing the tool Claude just invoked.

    This is the narration that answers "what is it doing right now" — the CLI
    does not forward thinking blocks in print mode, so the tool calls are the
    live signal. Phrased as an action ("Reading foo.py") rather than a raw tool
    name and JSON blob.
    """

    def first(*keys: str) -> str:
        for key in keys:
            value = params.get(key)
            if isinstance(value, str) and value.strip():
                return " ".join(value.split())
        return ""

    target = first("file_path", "path", "notebook_path")
    short = os.path.basename(target) if target else ""

    if name == "Read":
        return f"Reading {short}" if short else "Reading a file"
    if name in ("Edit", "NotebookEdit"):
        return f"Editing {short}" if short else "Editing a file"
    if name == "Write":
        return f"Writing {short}" if short else "Writing a file"
    if name in ("Bash", "PowerShell"):
        cmd = first("command")
        return f"Running: {cmd}" if cmd else f"Running a {name} command"
    if name in ("Grep", "Glob"):
        pattern = first("pattern")
        return f"Searching for {pattern}" if pattern else "Searching"
    if name in ("WebFetch", "WebSearch"):
        what = first("url", "query")
        return f"Fetching {what}" if what else "Searching the web"
    if name == "Task":
        return f"Delegating: {first('description') or 'a subtask'}"
    if name == "TodoWrite":
        return "Updating the task list"
    detail = first("description", "command", "query", "prompt")
    return f"Using {name}: {detail}" if detail else f"Using {name}"


def _tool_result_text(content: object) -> str:
    """Plain text of a tool's result, whatever shape the CLI delivers it in.

    ``tool_result`` content is sometimes a bare string (most tools) and sometimes
    a list of typed blocks (``{"type": "text", "text": …}`` plus images). We keep
    the text and note any image so the actual *output* of the tool can be shown.
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif block.get("type") == "image":
                parts.append("[image]")
        return "\n".join(p for p in parts if p).strip()
    return ""


def create_desktop_shortcut() -> str:
    """Put a BlindPilot shortcut on the desktop. Returns where it was written.

    An unpacked copy never went through an installer, so nothing has offered it
    a shortcut; this is how it gets one. Raises OSError with a readable reason.
    """
    if platform.system() != "Windows":
        raise OSError("Desktop shortcuts are created on Windows only.")
    target = Path(sys.executable).resolve()
    if not getattr(sys, "frozen", False):
        raise OSError("A shortcut can only point at a packaged BlindPilot.")
    desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
    if not desktop.is_dir():
        raise OSError("The desktop folder could not be found.")
    link = desktop / f"{APP_NAME}.lnk"
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut("
        f"'{str(link).replace(chr(39), chr(39) * 2)}'); "
        f"$s.TargetPath = '{str(target).replace(chr(39), chr(39) * 2)}'; "
        f"$s.WorkingDirectory = '{str(target.parent).replace(chr(39), chr(39) * 2)}'; "
        f"$s.Description = '{APP_NAME}'; $s.Save()"
    )
    powershell = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        **_no_window_kwargs(),
    )
    if result.returncode != 0 or not link.exists():
        raise OSError((result.stderr or "The shortcut could not be created.").strip())
    return str(link)


def _flatten(text: str) -> str:
    """Reduce to letters and digits, for comparing two copies of one answer.

    Backends assemble their final text from the same pieces they streamed, but
    not always with the same joins, and one that streams from a rendered
    terminal streams the text without its Markdown, so neither the whitespace
    nor the punctuation can be relied on to match.
    """
    return "".join(character.casefold() for character in text if character.isalnum())


def _result_label(text: str) -> str:
    """Short, screen-reader-friendly preview line for a result row."""
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    first = " ".join(first.split())
    if len(first) > 100:
        first = first[:99] + "…"
    return f"Result: {first}" if first else "Result"


def _config_dir() -> Path:
    return blindpilot_config_dir()


def _legacy_config_path() -> Path:
    """Original Claude Code Reader config, read-only for one-way migration."""
    if platform.system() == "Windows":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "claude-reader" / "config.json"
    return Path.home() / ".config" / "claude-reader" / "config.json"


def _config_path() -> Path:
    return _config_dir() / "config.json"


def _load_config() -> dict:
    for path in (_config_path(), _legacy_config_path()):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            continue
    return {}


def _save_config(cfg: dict) -> None:
    try:
        _config_dir().mkdir(parents=True, exist_ok=True)
        with open(_config_path(), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except OSError:
        pass


class _Settings:
    """User preferences that change how a run is presented, saved to config.

    ``live_rows`` and ``speak_live`` default to on, so activity appears in the
    list and is spoken automatically through NVDA, JAWS, or VoiceOver. Turning both off restores the
    pre-live-narration behaviour: nothing appears until the turn ends, and
    nothing is spoken. ``text_view`` swaps the responses list for a read-only
    edit field and defaults to off.
    """

    def __init__(self) -> None:
        cfg = _load_config()
        self.live_rows = bool(cfg.get("live_rows", True))
        self.speak_live = bool(cfg.get("speak_live", True))
        self.text_view = bool(cfg.get("text_view", False))
        self.show_thinking = bool(cfg.get("show_thinking", False))

    def save(self) -> None:
        cfg = _load_config()
        cfg["live_rows"] = self.live_rows
        cfg["speak_live"] = self.speak_live
        cfg["text_view"] = self.text_view
        cfg["show_thinking"] = self.show_thinking
        _save_config(cfg)


SETTINGS = _Settings()


def _resource_dir() -> str:
    """Directory holding bundled resources (EarCons, etc.).

    PyInstaller unpacks data files to ``sys._MEIPASS`` at runtime; from source
    it's just the script's own directory.
    """
    base = getattr(sys, "_MEIPASS", None)
    return base if base else os.path.dirname(os.path.abspath(__file__))


class Earcons:
    """Non-speech audio cues.

    Three cues: a one-shot when a prompt is sent, a looping cue while a request
    is in flight, and a one-shot when the response arrives. Uses only the
    platform's built-in player so there's no third-party audio dependency:
    ``winsound`` on Windows (native async + loop), ``afplay`` on macOS (looped
    by re-spawning in a daemon thread). Missing files are silently ignored.
    """

    def __init__(self, folder: str):
        self._folder = folder
        self._system = platform.system()
        self.send = self._resolve("send")
        self.received = self._resolve("received", "Recieved")
        self.in_progress = self._resolve("in-progress", "in_progress")
        self._loop_stop = threading.Event()
        self._loop_thread: Optional[threading.Thread] = None
        self._loop_proc: Optional[subprocess.Popen] = None

    def _resolve(self, *basenames: str) -> Optional[str]:
        for name in basenames:
            for ext in (".wav", ".ogg", ".aiff", ".aif", ".mp3"):
                path = os.path.join(self._folder, name + ext)
                if os.path.isfile(path):
                    return path
        return None

    def _unix_player(self) -> Optional[list]:
        if self._system == "Darwin":
            return ["afplay"]
        for player in ("paplay", "aplay", "ffplay"):
            found = shutil.which(player)
            if found:
                return [found] + (["-nodisp", "-autoexit"] if player == "ffplay" else [])
        return None

    def _play_once(self, path: Optional[str]) -> None:
        if not path:
            return
        try:
            if self._system == "Windows":
                import winsound

                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            else:
                player = self._unix_player()
                if player:
                    subprocess.Popen(
                        player + [path],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
        except Exception:
            pass

    def play_send(self) -> None:
        self._play_once(self.send)

    def play_received(self) -> None:
        self.stop_progress()
        self._play_once(self.received)

    def start_progress(self) -> None:
        self.stop_progress()
        if not self.in_progress:
            return
        if self._system == "Windows":
            try:
                import winsound

                winsound.PlaySound(
                    self.in_progress,
                    winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP,
                )
            except Exception:
                pass
            return
        self._loop_stop.clear()
        self._loop_thread = threading.Thread(target=self._loop_unix, daemon=True)
        self._loop_thread.start()

    def _loop_unix(self) -> None:
        player = self._unix_player()
        if not player:
            return
        while not self._loop_stop.is_set():
            try:
                self._loop_proc = subprocess.Popen(
                    player + [self.in_progress],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._loop_proc.wait()
            except Exception:
                break

    def stop_progress(self) -> None:
        if self._system == "Windows":
            try:
                import winsound

                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
            return
        self._loop_stop.set()
        proc = self._loop_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        self._loop_proc = None


def _copy_to_clipboard(text: str) -> bool:
    if wx.TheClipboard.Open():
        try:
            wx.TheClipboard.SetData(wx.TextDataObject(text))
            return True
        finally:
            wx.TheClipboard.Close()
    return False


@dataclass
class Turn:
    prompt: str
    response: str = ""


class ClaudeWorker(threading.Thread):
    """Runs the Claude Code CLI subprocess and delivers results via callbacks.

    All callbacks are invoked from this worker thread; the caller is
    responsible for marshalling them back to the GUI thread (wx.CallAfter).
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
        on_session: Callable[[str], None],
        on_started: Callable[[], None],
        on_activity: Callable[[str, str], None],
        on_complete: Callable[[str], None],
        on_failed: Callable[[str], None],
        on_done: Callable[[], None],
    ):
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
        self._proc: Optional[subprocess.Popen] = None
        self._cancelled = False
        # Set once the process is up and the opening prompt has gone in, cleared
        # when the turn ends. Guards `steer()` against writing to a pipe that is
        # not there yet (or is already gone).
        self._accepting_input = threading.Event()
        self._write_lock = threading.Lock()

    def accepting_input(self) -> bool:
        """Whether the active Claude turn can accept a steering message."""
        return self._accepting_input.is_set() and not self._cancelled

    def _write_message(self, text: str) -> bool:
        """Push one user message into the running process. False if it failed."""
        proc = self._proc
        if proc is None or proc.stdin is None:
            return False
        payload = json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                },
            }
        )
        try:
            with self._write_lock:
                proc.stdin.write(payload + "\n")
                proc.stdin.flush()
        except (OSError, ValueError):
            # Pipe closed underneath us — the turn finished as we wrote.
            return False
        return True

    def steer(self, text: str) -> bool:
        """Send a follow-up message into the turn that is already running.

        Returns False if the run is no longer listening, so the caller can put
        the text back in the prompt box rather than silently dropping it.
        """
        if not self.accepting_input():
            return False
        return self._write_message(text)

    def _close_stdin(self) -> None:
        self._accepting_input.clear()
        proc = self._proc
        if proc is not None and proc.stdin is not None:
            try:
                with self._write_lock:
                    proc.stdin.close()
            except (OSError, ValueError):
                pass

    def cancel(self) -> None:
        self._accepting_input.clear()
        self._cancelled = True
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    def run(self) -> None:
        try:
            self._do_run()
        finally:
            self._close_stdin()
            self._on_done()

    def _do_run(self) -> None:
        binary = _find_claude()
        if binary is None:
            self._on_failed("Claude Code not installed. Install from claude.com/claude-code")
            return

        # Streaming *input* mode: the prompt goes in over stdin as a JSON message
        # and stdin stays open, so further messages can be pushed into the run
        # while it is still working. That is what makes steering possible — the
        # CLI picks the new message up mid-turn and changes course.
        cmd = [
            binary,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if self._permission_mode:
            cmd.extend(["--permission-mode", self._permission_mode])
        # Left off entirely when unset, so the CLI's own default applies.
        if self._model:
            cmd.extend(["--model", self._model])
        if self._effort:
            cmd.extend(["--effort", self._effort])
        if self._session_id:
            cmd.extend(["--resume", self._session_id])

        # Make sure the binary's directory is on PATH for the subprocess —
        # `claude` is typically a shim that needs to find `node` (often a
        # sibling in the same Homebrew/npm bin dir).
        env = os.environ.copy()
        bin_dir = os.path.dirname(binary)
        if bin_dir and bin_dir not in env.get("PATH", "").split(os.pathsep):
            env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")

        try:
            self._proc = subprocess.Popen(
                cmd,
                cwd=self._cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                encoding="utf-8",
                env=env,
                **_no_window_kwargs(),
            )
        except OSError as exc:
            self._on_failed(f"Failed to launch Claude Code: {exc}")
            return

        if not self._write_message(self._prompt):
            self._on_failed("Could not send the prompt to Claude Code")
            return
        self._accepting_input.set()

        text_parts: list[str] = []
        first_assistant_seen = False
        complete = False

        assert self._proc.stdout is not None
        for raw_line in self._proc.stdout:
            if self._cancelled:
                break
            line = raw_line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                print(
                    f"[blindpilot] malformed Claude JSON line: {line!r}",
                    file=sys.stderr,
                )
                continue

            etype = event.get("type")

            if etype == "system" and event.get("subtype") == "init":
                sid = event.get("session_id")
                if sid:
                    self._on_session(sid)

            elif etype == "assistant":
                if not first_assistant_seen:
                    first_assistant_seen = True
                    self._on_started()
                message = event.get("message") or {}
                for block in message.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        # Claude's own words, as it writes them — streamed live to
                        # the list so the user reads the narration as it happens.
                        text = (block.get("text") or "").strip()
                        if text:
                            text_parts.append(text)
                            self._on_activity("assistant", text)
                    elif btype == "thinking":
                        # Extended-thinking blocks: Claude reasoning about what to
                        # do next. Surfaced live so the user hears the plan while
                        # the work happens, but kept out of `text_parts` — it is
                        # not part of the answer.
                        thought = (block.get("thinking") or "").strip()
                        if thought:
                            self._on_activity("thinking", thought)
                    elif btype == "redacted_thinking":
                        self._on_activity("thinking", "[redacted thinking]")
                    elif btype == "tool_use":
                        # The live "what is it doing" signal: announced when the
                        # tool is called, with its result following separately.
                        params = block.get("input")
                        self._on_activity(
                            "tool",
                            _tool_use_label(
                                str(block.get("name") or "tool"),
                                params if isinstance(params, dict) else {},
                            ),
                        )

            elif etype == "user":
                # Tool results come back as user-role messages. Surface the actual
                # output (file contents, command output, …) as its own live row.
                message = event.get("message") or {}
                for block in message.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_result":
                        result = _tool_result_text(block.get("content"))
                        if result:
                            self._on_activity("result", result)

            elif etype == "result":
                complete = True
                if event.get("is_error"):
                    detail = (event.get("result") or "").strip()
                    if _looks_like_auth_error(detail):
                        self._on_failed(AUTH_HINT)
                    else:
                        self._on_failed(detail or "Claude Code returned an error")
                    return
                # In streaming-input mode the process waits for more messages
                # rather than ending at EOF, so the turn's own result event is
                # what tells us to stop reading and let it shut down.
                self._close_stdin()
                break

        self._close_stdin()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()

        if self._cancelled:
            return

        rc = self._proc.returncode
        if rc != 0:
            stderr_text = ""
            if self._proc.stderr is not None:
                try:
                    stderr_text = self._proc.stderr.read().strip()
                except Exception:
                    pass
            if _looks_like_auth_error(stderr_text):
                self._on_failed(AUTH_HINT)
                return
            detail = f": {stderr_text}" if stderr_text else ""
            self._on_failed(f"Claude Code exited with code {rc}{detail}")
            return

        if not complete and not text_parts:
            self._on_failed("No response received")
            return

        # Blank line between blocks: a turn now usually has several (the running
        # narration, then the answer), and they are separate paragraphs.
        self._on_complete("\n\n".join(text_parts).strip())


class ReadView(wx.Dialog):
    """Modal read-only viewer for a single row's payload. Esc closes.

    Focus moves into the text area so the user can review line by line, spell
    words, and select / copy normally with Ctrl+A / Ctrl+C.
    """

    def __init__(self, parent: wx.Window, text: str, title: str):
        super().__init__(
            parent,
            title=title,
            size=(700, 500),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )

        viewer = wx.TextCtrl(
            self,
            value=text,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP | wx.TE_RICH2,
        )
        viewer.SetName(title)
        viewer.SetInsertionPoint(0)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(viewer, 1, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)

        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        viewer.SetFocus()

    def _on_key(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        event.Skip()


class ModelDialog(wx.Dialog):
    """Model picker for /model: one combo box for the model, one for effort.

    Both lists come from the CLI probe that ran just before this opened, so the
    choices are whatever the installed Claude Code actually accepts. The first
    entry in each box names what Claude Code is using right now — picking it
    passes no flag at all, so it keeps that. The model box is editable so a
    full model ID can be typed; the effort box is a fixed list. Esc cancels.
    """

    def __init__(
        self,
        parent: wx.Window,
        options: "ModelOptions",
        selected_model: str,
        selected_effort: str,
        backend_name: str = "CLI",
    ):
        super().__init__(parent, title="Model")

        current = options.current_model or "unknown"
        if options.current_effort:
            current = f"{current}, effort {options.current_effort}"
        lines = [f"{backend_name} reports the current model as: {current}."]
        if selected_model or selected_effort:
            lines.append(
                "This tab overrides that with: "
                f"model {selected_model or 'unchanged'}, "
                f"effort {selected_effort or 'unchanged'}."
            )
        if options.error:
            lines.append(options.error)
        summary = wx.StaticText(self, label="\n".join(lines))
        summary.Wrap(520)

        # "Leave it alone" is the first entry in both boxes, and it says what
        # leaving it alone actually means rather than just "(CLI default)".
        self._model_keep = _keep_choice(options.current_model)
        self._effort_keep = _keep_choice(options.current_effort)

        model_label = wx.StaticText(self, label="&Model:")
        self.model_box = wx.ComboBox(
            self,
            choices=[self._model_keep, *options.models],
            style=wx.CB_DROPDOWN,
        )
        self.model_box.SetName("Model")
        self.model_box.SetValue(selected_model or self._model_keep)

        effort_label = wx.StaticText(self, label="&Effort:")
        self.effort_box = wx.ComboBox(
            self,
            choices=[self._effort_keep, *options.efforts],
            style=wx.CB_DROPDOWN | wx.CB_READONLY,
        )
        self.effort_box.SetName("Effort")
        self.effort_box.SetStringSelection(selected_effort or self._effort_keep)

        grid = wx.FlexGridSizer(2, 2, 8, 8)
        grid.AddGrowableCol(1, 1)
        grid.Add(model_label, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.model_box, 1, wx.EXPAND)
        grid.Add(effort_label, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.effort_box, 1, wx.EXPAND)

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(summary, 0, wx.ALL, 12)
        sizer.Add(grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
        if buttons is not None:
            sizer.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 12)
        self.SetSizerAndFit(sizer)

        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.model_box.SetFocus()

    def _on_key(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        event.Skip()

    def selection(self) -> tuple[str, str]:
        """(model, effort) — "" for either one left as Claude Code has it."""
        model = self.model_box.GetValue().strip()
        effort = self.effort_box.GetValue().strip()
        return (
            "" if model in (DEFAULT_CHOICE, self._model_keep) else model,
            "" if effort in (DEFAULT_CHOICE, self._effort_keep) else effort,
        )


class NewSessionDialog(wx.Dialog):
    """New Session: a blank folder field, a Browse button, OK and Cancel.

    The field starts empty so a path can simply be typed or pasted; Browse
    fills it in from a folder picker. OK is refused (with a spoken message)
    until the field names a real folder, so the dialog never opens a session
    on a path that does not exist. Esc cancels.
    """

    def __init__(self, parent: wx.Window, default_dir: Optional[str] = None):
        super().__init__(parent, title="New Session")
        self._default_dir = default_dir or os.path.expanduser("~")
        self.path = ""

        label = wx.StaticText(self, label="&Folder for the new session:")
        self.folder_box = wx.TextCtrl(self, value="")
        self.folder_box.SetName("Folder for the new session")
        self.folder_box.SetMinSize((420, -1))
        browse_btn = wx.Button(self, label="&Browse…")
        browse_btn.Bind(wx.EVT_BUTTON, lambda _e: self._browse())

        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(self.folder_box, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        row.Add(browse_btn, 0, wx.ALIGN_CENTER_VERTICAL)

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 12)
        if buttons is not None:
            sizer.Add(buttons, 0, wx.ALIGN_RIGHT | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        self.SetSizerAndFit(sizer)

        # Validate before the dialog closes, so a bad path can be corrected
        # in place instead of failing after the session is created.
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.folder_box.SetFocus()

    def _browse(self) -> None:
        typed = self.folder_box.GetValue().strip().strip('"')
        start = typed if typed and os.path.isdir(os.path.expanduser(typed)) else self._default_dir
        with wx.DirDialog(
            self,
            "Choose a folder for the new session",
            defaultPath=os.path.expanduser(start),
            style=wx.DD_DEFAULT_STYLE,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = dlg.GetPath()
        self.folder_box.SetValue(path)
        self.folder_box.SetInsertionPointEnd()
        self.folder_box.SetFocus()
        announce(f"Folder set to {path}")

    def _on_ok(self, event: wx.CommandEvent) -> None:
        # Quotes are stripped because a path copied from Explorer often has them.
        typed = self.folder_box.GetValue().strip().strip('"')
        if not typed:
            self._reject("Type a folder path, or use the Browse button.")
            return
        path = os.path.abspath(os.path.expanduser(os.path.expandvars(typed)))
        if not os.path.isdir(path):
            self._reject(f"That folder does not exist:\n{path}")
            return
        self.path = path
        event.Skip()

    def _reject(self, message: str) -> None:
        announce(message)
        with wx.MessageDialog(self, message, "New Session", style=wx.OK | wx.ICON_WARNING) as warn:
            warn.ShowModal()
        self.folder_box.SetFocus()

    def _on_key(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        event.Skip()


# The two scopes the history picker can list, in the order they are offered.
_HISTORY_SCOPES = ("folder", "all")
_HISTORY_SCOPE_LABELS = ("This folder", "All folders")

# "All backends" sits first in the backend list; the rest follow BACKEND_IDS.
_HISTORY_ANY_BACKEND = "All backends"


class HistoryDialog(wx.Dialog):
    """Recent Conversations: pick a past conversation and carry on with it.

    The list is every conversation the chosen backend has stored, newest first,
    each one named by the message that started it — which is the only thing
    that reliably tells two of them apart when they are read out. Typing in the
    filter narrows the list by title; the backend and folder pickers widen it.

    Enter (or Open) resumes the selected conversation in a new tab. Esc cancels.
    """

    def __init__(self, parent: wx.Window, backend: str, cwd: str):
        super().__init__(
            parent,
            title="Recent Conversations",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._cwd = cwd
        self._entries: List[HistoryEntry] = []
        self._shown: List[HistoryEntry] = []
        self.entry: Optional[HistoryEntry] = None

        backend_label_text = wx.StaticText(self, label="&Backend:")
        self._backend_values = [""] + list(BACKEND_IDS)
        self.backend_picker = wx.Choice(
            self,
            choices=[_HISTORY_ANY_BACKEND] + [BACKEND_LABELS[b] for b in BACKEND_IDS],
        )
        self.backend_picker.SetName("Backend")
        self.backend_picker.SetSelection(self._backend_values.index(normalize_backend(backend)))
        self.backend_picker.Bind(wx.EVT_CHOICE, lambda _e: self._reload())

        scope_label = wx.StaticText(self, label="&Show:")
        self.scope_picker = wx.Choice(self, choices=list(_HISTORY_SCOPE_LABELS))
        self.scope_picker.SetName("Show")
        self.scope_picker.SetSelection(0)
        self.scope_picker.Bind(wx.EVT_CHOICE, lambda _e: self._reload())

        filter_label = wx.StaticText(self, label="&Filter:")
        self.filter_box = wx.TextCtrl(self)
        self.filter_box.SetName("Filter conversations")
        self.filter_box.SetHint("Type part of a conversation's first message")
        self.filter_box.Bind(wx.EVT_TEXT, lambda _e: self._refresh())

        list_label = wx.StaticText(self, label="&Conversations:")
        self.list_box = wx.ListBox(self, style=wx.LB_SINGLE | wx.LB_NEEDED_SB)
        self.list_box.SetName("Conversations")
        self.list_box.Bind(wx.EVT_LISTBOX_DCLICK, lambda _e: self._accept())

        self.summary = wx.StaticText(self, label="")
        self.summary.SetName("Summary")

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        open_button = self.FindWindowById(wx.ID_OK)
        if open_button is not None:
            open_button.SetLabel("&Open")

        pickers = wx.FlexGridSizer(2, 2, 8, 8)
        pickers.AddGrowableCol(1, 1)
        pickers.Add(backend_label_text, 0, wx.ALIGN_CENTER_VERTICAL)
        pickers.Add(self.backend_picker, 0)
        pickers.Add(scope_label, 0, wx.ALIGN_CENTER_VERTICAL)
        pickers.Add(self.scope_picker, 0)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(pickers, 0, wx.EXPAND | wx.ALL, 12)
        sizer.Add(filter_label, 0, wx.LEFT | wx.RIGHT, 12)
        sizer.Add(self.filter_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
        sizer.Add(list_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
        sizer.Add(self.list_box, 1, wx.EXPAND | wx.ALL, 12)
        sizer.Add(self.summary, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        if buttons is not None:
            sizer.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 12)
        self.SetSizerAndFit(sizer)
        self.SetSize(wx.Size(620, 460))

        self.Bind(wx.EVT_BUTTON, lambda _e: self._accept(), id=wx.ID_OK)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self._reload()
        self.filter_box.SetFocus()

    # ----- Loading and filtering -----
    def _selected_backend(self) -> Optional[str]:
        value = self._backend_values[max(0, self.backend_picker.GetSelection())]
        return value or None

    def _selected_cwd(self) -> Optional[str]:
        scope = _HISTORY_SCOPES[max(0, self.scope_picker.GetSelection())]
        return self._cwd if scope == "folder" else None

    def _reload(self) -> None:
        """Re-scan the history stores for the chosen backend and scope."""
        with wx.BusyCursor():
            self._entries = list_history(self._selected_backend(), self._selected_cwd())
        self._refresh()

    def _label_for(self, entry: HistoryEntry) -> str:
        parts = [entry.title or "(untitled)", describe_age(entry.modified)]
        if self._selected_cwd() is None and entry.folder:
            parts.append(entry.folder)
        if self._selected_backend() is None:
            parts.append(backend_label(entry.backend))
        return " — ".join(parts)

    def _refresh(self) -> None:
        term = self.filter_box.GetValue().strip().lower()
        self._shown = [entry for entry in self._entries if not term or term in entry.title.lower()]
        self.list_box.Set([self._label_for(entry) for entry in self._shown])
        if self._shown:
            self.list_box.SetSelection(0)
        count = len(self._shown)
        if not self._entries:
            message = "No past conversations found here"
        elif count == 1:
            message = "1 conversation"
        else:
            message = f"{count} conversations"
        self.summary.SetLabel(message)
        self._set_open_enabled(bool(self._shown))

    def _set_open_enabled(self, enabled: bool) -> None:
        button = self.FindWindowById(wx.ID_OK)
        if button is not None:
            button.Enable(enabled)

    # ----- Choosing -----
    def _accept(self) -> None:
        selection = self.list_box.GetSelection()
        if selection == wx.NOT_FOUND or selection >= len(self._shown):
            announce("Error: Choose a conversation first")
            return
        self.entry = self._shown[selection]
        self.EndModal(wx.ID_OK)

    def _on_key(self, event: wx.KeyEvent) -> None:
        key = event.GetKeyCode()
        if key == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and self._shown:
            self._accept()
            return
        # Down from the filter box drops straight into the list, so a filter
        # can be typed and its first result reached without hunting for Tab.
        if key == wx.WXK_DOWN and self.filter_box.HasFocus() and self._shown:
            self.list_box.SetFocus()
            self.list_box.SetSelection(0)
            return
        event.Skip()


class SessionPanel(wx.Panel):
    """One conversation tab — owns its session_id, rows, and worker.

    Layout, top to bottom: working-directory label, search box, the flat list of
    rows (oldest at top, newest at bottom), the multi-line prompt box, the Send
    button. Focus starts in the prompt; Up from the prompt enters the newest
    row. Arrow keys remain within the responses, including at the first and last
    rows; Tab is the way to move between the list and the prompt.

    `on_status(panel, text)` lets the frame show only the active tab's status.
    """

    def __init__(
        self,
        parent: wx.Window,
        cwd: str,
        on_status: Callable[["SessionPanel", str], None],
        earcons: "Earcons",
        on_side_chat: Callable[[str, str], None],
        get_backend: Callable[[], str],
    ):
        super().__init__(parent)
        self.cwd = cwd
        self._on_status = on_status
        self._earcons = earcons
        self._on_side_chat = on_side_chat
        self._get_backend = get_backend
        self.last_status = "Ready"

        self._turns: List[Turn] = []
        self._rows: List[Row] = []  # every row across every response, in order
        self._displayed: List[Row] = []  # rows currently shown (after search)
        self._search_term = ""
        self._response_count = 0
        # Response number of the turn currently streaming in (None between turns).
        self._stream_response: Optional[int] = None
        self._assistant_narrated_this_turn = False
        # Answer text already put into the list for the turn in flight, so the
        # finished answer can be checked against it rather than assumed shown.
        self._streamed_assistant = ""
        # Set while the user's Stop is being carried out, so the backend's own
        # "cancelled" report is not announced to them as an error.
        self._stopping = False
        self._session_id: Optional[str] = None
        self._session_backend = normalize_backend(self._get_backend())
        self._worker: Optional[threading.Thread] = None
        # Worker callbacks arrive on a background thread. Keep them in one
        # ordered mailbox with at most one pending GUI callback; otherwise a
        # long, chatty job can flood wx's event queue and starve NVDA/key input.
        self._worker_event_lock = threading.Lock()
        self._worker_events: deque[tuple[str, tuple[object, ...]]] = deque()
        self._worker_events_scheduled = False
        # Starts at your remembered choice, or the active provider's default
        # mode for this directory.
        self.mode = _default_permission_mode(cwd, self._session_backend)
        # Empty means "don't pass the flag" — the CLI picks its own default.
        self.model = ""
        self.effort = ""
        # What the CLI last reported it is using, for when we pass no flag.
        self._cli_model = ""
        self._cli_effort = ""
        self._attachments: List[str] = []

        self.backend_status = wx.StaticText(
            self, label=f"Backend: {backend_label(self._session_backend)}"
        )
        self.backend_status.SetName("Backend")

        cwd_label = wx.StaticText(self, label=f"Working directory: {cwd}")
        cwd_label.SetName("Working directory")

        responses_label = wx.StaticText(self, label="Responses:")
        self.responses = wx.ListBox(self, style=wx.LB_SINGLE | wx.LB_NEEDED_SB)
        self.responses.SetName("Responses")
        self.responses.Bind(wx.EVT_LISTBOX_DCLICK, self._on_list_activate)
        self.responses.Bind(wx.EVT_KEY_DOWN, self._on_list_key)
        self.responses.Bind(wx.EVT_CONTEXT_MENU, lambda _e: self._show_row_menu())

        # Same rows, one per line, in a read-only edit field — NVDA (and any
        # screen reader) can then browse them with its own review/say-all
        # commands, select across rows, and copy with Ctrl+C. Options decides
        # which of the two controls is shown; only the visible one is filled.
        self.responses_text = wx.TextCtrl(
            self,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP | wx.TE_RICH2,
        )
        self.responses_text.SetName("Responses")
        self.responses_text.Bind(wx.EVT_KEY_DOWN, self._on_list_key)
        self.responses_text.Bind(wx.EVT_CONTEXT_MENU, lambda _e: self._show_row_menu())
        self.responses_text.Bind(wx.EVT_SET_FOCUS, self._on_text_view_focus)

        prompt_label = wx.StaticText(self, label="Prompt:")
        self.prompt = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_PROCESS_ENTER)
        self.prompt.SetName("Prompt")
        self.prompt.SetHint(
            "Type your prompt. Enter to send, Shift+Enter for newline, Up to enter responses; "
            "Tab returns here from responses."
        )
        self.prompt.Bind(wx.EVT_KEY_DOWN, self._on_prompt_key)
        self.prompt.Bind(wx.EVT_SET_FOCUS, self._on_prompt_focus)
        self.prompt.Bind(wx.EVT_TEXT, self._on_prompt_text_changed)
        self._dictation_timer = None
        char_h = self.prompt.GetCharHeight()
        self.prompt.SetMinSize((-1, char_h * 5 + 8))

        # Bottom row: Send, Attach, then the Permission mode picker — one line.
        self.send_btn = wx.Button(self, label="Send")
        self.send_btn.SetName("Send")
        self.send_btn.Bind(wx.EVT_BUTTON, lambda _e: self._on_send())
        self.send_btn.Bind(wx.EVT_KEY_DOWN, self._on_send_key)

        # Steer sits right after Send in the tab order, so during a run you can
        # type a correction, press Tab once, and press it. Enabled only while a
        # run is actually listening.
        self.steer_btn = wx.Button(self, label="Steer")
        self.steer_btn.SetName("Steer the running task")
        self.steer_btn.SetToolTip("Send this message into the task that is already running")
        self.steer_btn.Bind(wx.EVT_BUTTON, lambda _e: self._on_steer())
        self.steer_btn.Disable()

        # Stop follows Steer: the two things you can do to a run in progress sit
        # together, one Tab apart from the prompt. Enabled only while one is.
        self.stop_btn = wx.Button(self, label="Stop")
        self.stop_btn.SetName("Stop the running task")
        self.stop_btn.SetToolTip("Stop the task that is running now")
        self.stop_btn.Bind(wx.EVT_BUTTON, lambda _e: self._on_stop())
        self.stop_btn.Disable()

        self.attach_btn = wx.Button(self, label="Attach")
        self.attach_btn.SetName("Attach files")
        self.attach_btn.Bind(wx.EVT_BUTTON, lambda _e: self.attach_files())

        self.slash_btn = wx.Button(self, label="Slash…")
        self.slash_btn.SetName("Slash command picker")
        self.slash_btn.Bind(wx.EVT_BUTTON, lambda _e: self._pick_slash_command())

        mode_label = wx.StaticText(self, label="Permission mode:")
        self.mode_picker = wx.Choice(self, choices=_MODE_LABELS)
        self.mode_picker.SetName("Permission mode")
        self.mode_picker.SetSelection(_MODE_VALUES.index(self.mode))
        self.mode_picker.Bind(wx.EVT_CHOICE, self._on_mode_choice)
        self.mode_picker.Bind(wx.EVT_SET_FOCUS, self._on_mode_picker_focus)

        bottom_row = wx.BoxSizer(wx.HORIZONTAL)
        bottom_row.Add(self.send_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        bottom_row.Add(self.steer_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        bottom_row.Add(self.stop_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        bottom_row.Add(self.attach_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        bottom_row.Add(self.slash_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 16)
        bottom_row.Add(mode_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        bottom_row.Add(self.mode_picker, 0, wx.ALIGN_CENTER_VERTICAL)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.backend_status, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        sizer.Add(cwd_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        sizer.Add(responses_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        sizer.Add(self.responses, 1, wx.EXPAND | wx.ALL, 6)
        sizer.Add(self.responses_text, 1, wx.EXPAND | wx.ALL, 6)
        sizer.Add(prompt_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        sizer.Add(self.prompt, 0, wx.EXPAND | wx.ALL, 6)
        sizer.Add(bottom_row, 0, wx.ALL, 6)
        self.SetSizer(sizer)
        self.apply_view_mode()
        self.backend_changed()

    # ----- Responses view (list box or read-only edit field) -----
    def apply_view_mode(self) -> None:
        """Show whichever responses control Options currently asks for.

        Keeps the row the user was on, so flipping the setting mid-read does not
        lose their place, and hands focus to the new control if the old one had
        it.
        """
        was_on = self._selected_row()
        had_focus = self._responses_ctrl().HasFocus()
        text_mode = SETTINGS.text_view
        sizer = self.GetSizer()
        sizer.Show(self.responses, not text_mode)
        sizer.Show(self.responses_text, text_mode)
        self.Layout()
        self._refresh_list()
        if was_on == wx.NOT_FOUND:
            return
        if had_focus:
            self._focus_row(was_on)
        else:
            self._select_row(was_on)

    def _responses_ctrl(self) -> wx.Window:
        return self.responses_text if SETTINGS.text_view else self.responses

    def _row_count(self) -> int:
        return len(self._displayed)

    def _selected_row(self) -> int:
        """Index into ``self._displayed`` of the row the user is on."""
        if not self._displayed:
            return wx.NOT_FOUND
        if SETTINGS.text_view:
            ok, _col, line = self.responses_text.PositionToXY(
                self.responses_text.GetInsertionPoint()
            )
            if not ok or not (0 <= line < len(self._displayed)):
                return wx.NOT_FOUND
            return line
        sel = self.responses.GetSelection()
        return sel if 0 <= sel < len(self._displayed) else wx.NOT_FOUND

    def _select_row(self, index: int) -> None:
        """Move to a row without stealing focus."""
        count = self._row_count()
        if count == 0:
            return
        index = max(0, min(index, count - 1))
        if SETTINGS.text_view:
            self.responses_text.SetInsertionPoint(self.responses_text.XYToPosition(0, index))
        else:
            self.responses.SetSelection(index)

    # ----- Focus helpers -----
    def focus_prompt(self) -> None:
        self.prompt.SetFocus()

    def _focus_row(self, index: int) -> None:
        if self._row_count() == 0:
            return
        self._select_row(index)
        self._responses_ctrl().SetFocus()

    def _on_text_view_focus(self, event: wx.FocusEvent) -> None:
        event.Skip()
        wx.CallAfter(announce, "Responses, read only edit")

    # ----- Permission mode -----
    def _set_mode(self, value: str, speak: bool = True) -> None:
        if value not in _MODE_VALUES:
            return
        self.mode = value
        self.mode_picker.SetSelection(_MODE_VALUES.index(value))
        # Remembered globally, so new tabs and the next launch start here.
        _remember_permission_mode(value)
        if speak:
            self._announce(_MODE_DESCRIPTIONS[value])

    def _on_mode_choice(self, event: wx.CommandEvent) -> None:
        self._set_mode(_MODE_VALUES[self.mode_picker.GetSelection()])

    def _on_mode_picker_focus(self, event: wx.FocusEvent) -> None:
        event.Skip()
        label = _MODE_LABELS[_MODE_VALUES.index(self.mode)]
        wx.CallAfter(announce, f"Permission mode: {label}")

    # ----- Backend -----
    def selected_backend(self) -> str:
        return normalize_backend(self._get_backend())

    def backend_changed(self) -> None:
        """Refresh the visible provider label after File → Backend changes."""
        selected = self.selected_backend()
        suffix = ""
        if selected != self._session_backend and self._session_id:
            suffix = " — new conversation on next send"
        self.backend_status.SetLabel(f"Backend: {backend_label(selected)}{suffix}")
        if selected == BACKEND_FREEBUFF:
            # FreeBuff's terminal takes seconds to reach the point where it can
            # be given a message. Start one now, so the first message of the
            # conversation does not spend that wait in silence.
            prewarm_freebuff(self.cwd, self._session_id, self.model)
        supports_permissions = selected != BACKEND_FREEBUFF
        self.mode_picker.Enable(supports_permissions)
        if supports_permissions:
            self.mode_picker.SetToolTip(
                "Choose how the backend handles sandbox and approval requests"
            )
        else:
            self.mode_picker.SetToolTip(
                "FreeBuff does not expose permission modes through its command-line interface"
            )
        self.Layout()

    # ----- Model and effort -----
    def _model_summary(self) -> str:
        """What the next message will run as: this tab's override where it has
        one, otherwise whatever the selected backend last reported."""
        model = self.model or self._cli_model or "CLI default"
        effort = self.effort or self._cli_effort or "CLI default"
        return f"model {model}, effort {effort}"

    def warm_model_probe(self) -> None:
        """Ask the CLI about models in the background, so /model opens fast.

        The answer also tells us which model is in use, which is what the
        status line reports whenever this tab passes no --model flag.
        """

        backend = self.selected_backend()

        def work() -> None:
            options = probe_model_options(self.cwd, PROBE_TTL_SECONDS, backend)
            wx.CallAfter(self._remember_cli_model, options, backend)

        threading.Thread(target=work, daemon=True).start()

    def _remember_cli_model(self, options: "ModelOptions", backend: Optional[str] = None) -> None:
        if not self:  # tab closed while the probe was running
            return
        if backend is not None and normalize_backend(backend) != self.selected_backend():
            return
        self._cli_model = options.current_model
        self._cli_effort = options.current_effort

    def open_model_dialog(self, force_refresh: bool = False) -> None:
        """/model — offer the two combo boxes, filled from the CLI.

        A recent probe opens the dialog immediately; only a cold cache waits on
        the CLI, and that wait is announced so nothing looks frozen. Either way
        a background refresh runs, so the next open is both fast and current.
        """
        backend = self.selected_backend()
        if force_refresh:
            invalidate_model_options(backend)
        cached = (
            None if force_refresh else cached_model_options(self.cwd, PROBE_TTL_SECONDS, backend)
        )
        if cached is not None:
            self.warm_model_probe()
            self._show_model_dialog(cached, backend)
            return

        self._announce(f"Reading the model list from {backend_label(backend)}…")

        def work() -> None:
            options = probe_model_options(self.cwd, backend=backend)
            wx.CallAfter(self._show_model_dialog, options, backend)

        threading.Thread(target=work, daemon=True).start()

    def _show_model_dialog(self, options: "ModelOptions", backend: Optional[str] = None) -> None:
        if not self:  # tab closed while the probe was running
            return
        provider = normalize_backend(backend or self.selected_backend())
        self._remember_cli_model(options, provider)
        dlg = ModelDialog(self, options, self.model, self.effort, backend_label(provider))
        try:
            if dlg.ShowModal() != wx.ID_OK:
                self._announce(f"Model unchanged. Still using {self._model_summary()}.")
                return
            model, effort = dlg.selection()
        finally:
            dlg.Destroy()
        self.set_model(model, effort)

    def set_model(self, model: str, effort: str = "") -> None:
        """Apply the model / effort to every message sent from here on."""
        if self.selected_backend() == BACKEND_FREEBUFF and model != self.model:
            self._session_id = None
            self._announce("FreeBuff model changed; the next message starts a new conversation.")
            # Whatever terminal was waiting was started on the old model, and
            # FreeBuff reads that at launch, so it cannot serve the new one.
            discard_freebuff_prewarm()
            prewarm_freebuff(self.cwd, None, model)
        self.model = model
        self.effort = effort
        self._announce(f"Using {self._model_summary()} from your next message.")
        self.prompt.SetFocus()

    def cycle_mode(self) -> None:
        """Quick-cycle the everyday subset (default → accept edits → plan)."""
        if self.mode in _CYCLE_VALUES:
            nxt = _CYCLE_VALUES[(_CYCLE_VALUES.index(self.mode) + 1) % len(_CYCLE_VALUES)]
        else:
            nxt = _CYCLE_VALUES[0]
        self._set_mode(nxt)

    # ----- Attachments -----
    def attach_files(self) -> None:
        """Pick files to attach (Attach button / Cmd-Ctrl+Shift+A)."""
        with wx.FileDialog(
            self,
            "Attach files",
            defaultDir=self.cwd,
            style=wx.FD_OPEN | wx.FD_MULTIPLE | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            self._add_attachments(dlg.GetPaths())

    def _pick_slash_command(self) -> None:
        """Slash-command picker: choose a command to insert into the prompt."""
        commands = _slash_commands_for_backend(self.selected_backend())
        labels = [f"{cmd}  —  {desc}" for cmd, desc in commands]
        dlg = wx.SingleChoiceDialog(
            self,
            "Choose a slash command. It will be placed in the prompt ready to send.",
            "Slash Commands",
            labels,
        )
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            idx = dlg.GetSelection()
        finally:
            dlg.Destroy()
        if not (0 <= idx < len(commands)):
            return
        cmd_text = commands[idx][0]
        # Strip the placeholder hint (e.g. "[message]", "[model-id]") so the
        # inserted text is the raw command; user can append arguments if needed.
        cmd_text = cmd_text.split(" [")[0]
        self.prompt.SetValue(cmd_text)
        self.prompt.SetInsertionPointEnd()
        self.prompt.SetFocus()
        self._announce(f"Slash command: {cmd_text}. Edit if needed, then press Enter to send.")

    def _add_attachments(self, paths) -> None:
        added = 0
        for path in paths:
            ap = os.path.abspath(path)
            if ap not in self._attachments:
                self._attachments.append(ap)
                added += 1
        if not added:
            return
        names = ", ".join(os.path.basename(p) for p in self._attachments)
        count = len(self._attachments)
        self._announce(
            f"Attached {count} file{'' if count == 1 else 's'}: {names}. Send to upload."
        )

    def _try_paste_attachment(self) -> bool:
        """If the clipboard holds files or an image, attach them and report True.

        Files copied in Finder/Explorer arrive as filenames; a screenshot (or any
        copied image) arrives as a bitmap, which we save to a temp PNG and attach.
        Plain text returns False so the normal paste proceeds.
        """
        if not wx.TheClipboard.Open():
            return False
        try:
            if wx.TheClipboard.IsSupported(wx.DataFormat(wx.DF_FILENAME)):
                file_data = wx.FileDataObject()
                if wx.TheClipboard.GetData(file_data):
                    files = [f for f in file_data.GetFilenames() if os.path.isfile(f)]
                    if files:
                        self._add_attachments(files)
                        return True
            if wx.TheClipboard.IsSupported(wx.DataFormat(wx.DF_BITMAP)):
                bmp_data = wx.BitmapDataObject()
                if wx.TheClipboard.GetData(bmp_data):
                    bmp = bmp_data.GetBitmap()
                    if bmp.IsOk():
                        path = self._save_clipboard_image(bmp)
                        if path:
                            self._add_attachments([path])
                            return True
        finally:
            wx.TheClipboard.Close()
        return False

    @staticmethod
    def _save_clipboard_image(bmp: wx.Bitmap) -> Optional[str]:
        fd, path = tempfile.mkstemp(prefix="blindpilot-paste-", suffix=".png")
        os.close(fd)
        if bmp.ConvertToImage().SaveFile(path, wx.BITMAP_TYPE_PNG):
            return path
        try:
            os.remove(path)
        except OSError:
            pass
        return None

    # ----- Status forwarding -----
    def _set_status(self, text: str) -> None:
        self.last_status = text
        self._on_status(self, text)

    def _announce(self, text: str) -> None:
        """Speak a confirmation and mirror it to the status bar as a fallback."""
        announce(text)
        self._set_status(text)

    # ----- Prompt focus / key handling -----
    def _on_prompt_focus(self, event: wx.FocusEvent) -> None:
        event.Skip()
        wx.CallAfter(announce, "Prompt, edit text")

    def _on_prompt_text_changed(self, event: wx.CommandEvent) -> None:
        event.Skip()
        if self._dictation_timer is not None:
            self._dictation_timer.Stop()
        self._dictation_timer = wx.CallLater(1500, self._read_prompt_text)

    def _read_prompt_text(self) -> None:
        self._dictation_timer = None
        text = self.prompt.GetValue().strip()
        if text:
            announce(text)

    def _on_prompt_key(self, event: wx.KeyEvent) -> None:
        key = event.GetKeyCode()
        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            if event.ShiftDown():
                event.Skip()  # default: insert newline
                return
            self._on_send()
            return
        if key == wx.WXK_UP:
            # Enter the newest row, but only when the caret is on the first line
            # so ordinary multi-line cursor movement still works.
            ip = self.prompt.GetInsertionPoint()
            on_first_line = "\n" not in self.prompt.GetRange(0, ip)
            if on_first_line and self._row_count() > 0:
                self._focus_row(self._row_count() - 1)
                return
        if key == ord("V") and (event.CmdDown() or event.ControlDown()) and not event.AltDown():
            # Paste of a file or image becomes an attachment; plain text pastes
            # normally.
            if self._try_paste_attachment():
                return
        event.Skip()

    def _on_send_key(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._on_send()
            return
        event.Skip()

    # ----- Worker-to-GUI event mailbox -----
    def _queue_worker_event(self, name: str, *args: object) -> None:
        """Queue one worker callback without flooding wx's event loop.

        Every backend invokes callbacks from its worker thread. A single
        scheduled drain preserves their order while allowing a long stream to
        accumulate in this mailbox instead of as thousands of native GUI
        events.
        """
        with self._worker_event_lock:
            self._worker_events.append((name, args))
            if self._worker_events_scheduled:
                return
            self._worker_events_scheduled = True
        wx.CallAfter(self._drain_worker_events)

    def _drain_worker_events(self) -> None:
        """Apply a short batch, then yield to keyboard and accessibility events."""
        if not self:
            with self._worker_event_lock:
                self._worker_events.clear()
                self._worker_events_scheduled = False
            return

        started = time.monotonic()
        handled = 0
        rows_changed = False
        while handled < _WORKER_EVENT_BATCH_SIZE:
            if handled and time.monotonic() - started >= _WORKER_EVENT_BUDGET_SECONDS:
                break
            with self._worker_event_lock:
                if not self._worker_events:
                    break
                name, args = self._worker_events.popleft()

            if name == "activity":
                self._on_activity(str(args[0]), str(args[1]), refresh=False)
                rows_changed = True
            else:
                # Make preceding streamed rows visible before a status or
                # terminal event that logically follows them.
                if rows_changed:
                    self._refresh_list()
                    rows_changed = False
                if name == "session":
                    self._on_session_started(str(args[0]))
                elif name == "started":
                    self._announce("Receiving response")
                elif name == "complete":
                    self._on_response_complete(str(args[0]))
                elif name == "failed":
                    self._on_failed(str(args[0]))
                elif name == "done":
                    self._on_worker_finished()
            handled += 1

        if rows_changed:
            self._refresh_list()

        with self._worker_event_lock:
            pending = bool(self._worker_events)
            if not pending:
                self._worker_events_scheduled = False
        if pending:
            # Posting at the back of the native queue lets arrow, Tab, paint,
            # and screen-reader events already waiting run before the next batch.
            wx.CallAfter(self._drain_worker_events)

    # ----- Send flow -----
    def send_now(self) -> None:
        """Public entry point so the frame can fire a seeded side-chat prompt."""
        self._on_send()

    def _on_send(self, worker_extra: Optional[dict] = None) -> None:
        # ``worker_extra`` carries per-turn arguments only some backends take —
        # compaction, at present. Ordinary sends pass nothing.
        #
        # "/btw [message]" opens a new side-chat tab in the same directory
        # instead of sending to this conversation.
        raw = self.prompt.GetValue().strip()
        low = raw.lower()
        if low == "/btw" or low.startswith("/btw "):
            self.prompt.SetValue("")
            self._on_side_chat(self.cwd, raw[4:].strip())
            return
        if low in ("/clear", "/new"):
            self.prompt.SetValue("")
            self.clear_conversation()
            return
        if low == "/compact":
            self.prompt.SetValue("")
            self.compact_conversation()
            return
        # "/model" opens the picker; "/model <name>" sets it straight away.
        if low in ("/model", "/models") or low.startswith(("/model ", "/models ")):
            self.prompt.SetValue("")
            argument = raw.split(maxsplit=1)[1].strip() if " " in raw else ""
            if argument:
                parts = argument.split()
                effort = parts[1] if len(parts) > 1 else self.effort
                self.set_model(parts[0], effort)
            else:
                self.open_model_dialog(force_refresh=low == "/models")
            return
        if low == "/exit":
            self.prompt.SetValue("")
            frame = wx.GetTopLevelParent(self)
            if hasattr(frame, "_close_current_session"):
                wx.CallAfter(frame._close_current_session)
            return
        if low == "/resume":
            self.prompt.SetValue("")
            frame = wx.GetTopLevelParent(self)
            if hasattr(frame, "_open_history"):
                wx.CallAfter(frame._open_history)
            return

        if (
            self._worker is not None
            and self._worker.is_alive()
            and getattr(self._worker, "accepting_input", lambda: True)()
        ):
            # A run is already going, so Enter steers it rather than failing —
            # same thing the Steer button does.
            self._on_steer()
            return

        if self._worker is not None and self._worker.is_alive():
            self._announce("Error: The current backend is still finishing the previous turn")
            return

        prompt = self.prompt.GetValue().strip()
        if not prompt and not self._attachments:
            self._announce("Error: Prompt is empty")
            return

        selected_backend = self.selected_backend()
        if selected_backend != self._session_backend:
            self._session_id = None
            self._session_backend = selected_backend
            self.model = ""
            self.effort = ""
            self._cli_model = ""
            self._cli_effort = ""
            self.backend_status.SetLabel(f"Backend: {backend_label(selected_backend)}")
            self._announce(
                f"Starting a new {backend_label(selected_backend)} conversation in this tab"
            )

        send_text = self._build_send_text(prompt)
        self._turns.append(Turn(prompt=prompt))
        self._assistant_narrated_this_turn = False
        self._streamed_assistant = ""
        self._stopping = False
        self._add_your_message(send_text)
        self.prompt.SetValue("")
        self._attachments = []

        self._announce("Sending")
        self.send_btn.Disable()
        # Earcons: a one-shot "send", then loop "in progress" until the
        # response arrives (or the request fails).
        self._earcons.play_send()
        self._earcons.start_progress()

        worker_type = worker_class(selected_backend, ClaudeWorker)
        self._worker = worker_type(
            send_text,
            self._session_id,
            self.cwd,
            self.mode,
            model=self.model,
            effort=self.effort,
            on_session=lambda sid: self._queue_worker_event("session", sid),
            on_started=lambda: self._queue_worker_event("started"),
            on_activity=lambda kind, text: self._queue_worker_event("activity", kind, text),
            on_complete=lambda txt: self._queue_worker_event("complete", txt),
            on_failed=lambda msg: self._queue_worker_event("failed", msg),
            on_done=lambda: self._queue_worker_event("done"),
            **(worker_extra or {}),
        )
        self._worker.start()
        self.steer_btn.Enable()
        self.stop_btn.Enable()

    def _add_your_message(self, text: str, steering: bool = False) -> None:
        """Put the user's own message in the list, ahead of the answer to it.

        Carries the number of the response it belongs to, so both group together
        for jump-to-response and copy-whole-response. Skipped in silent-until-response mode,
        where nothing is shown until the response is finished.
        """
        if not SETTINGS.live_rows:
            return
        n = self._stream_response or self._response_count + 1
        prefix = "You, steering:" if steering else "You:"
        self._rows.append(
            Row(
                kind="you",
                label=f"{prefix} {' '.join(text.split())}",
                payload=text,
                response_number=n,
            )
        )
        self._refresh_list()

    def _on_steer(self) -> None:
        """Send what is typed into the run that is already going."""
        worker = self._worker
        text = self.prompt.GetValue().strip()
        if worker is None or not worker.is_alive():
            self._set_status("Error: Nothing is running to steer")
            return
        if not text:
            self._set_status("Error: Type a message first, then steer")
            return
        if not getattr(worker, "steer")(text):
            # The turn finished between typing and pressing. Leave the text in
            # place so it can just be sent as the next prompt.
            self._set_status("Error: The run already finished. Press Send to ask it now.")
            return
        self.prompt.SetValue("")
        self._earcons.play_send()
        self._add_your_message(text, steering=True)
        self._announce(f"Steered: {text}")

    def _on_stop(self) -> None:
        """Stop the run in progress, keeping whatever it produced first.

        Cancelling kills the backend process, so the rows and text already
        streamed are all there will be — they stay in the list, and the turn
        keeps them as its response so the transcript is not left with a
        question and no answer.
        """
        worker = self._worker
        if worker is None or not worker.is_alive():
            self._set_status("Error: Nothing is running to stop")
            return
        self.stop_btn.Disable()
        self.steer_btn.Disable()
        self._stopping = True
        self._announce("Stopping")
        # cancel() waits on the process, so it must not run on the UI thread.
        threading.Thread(target=worker.cancel, daemon=True).start()

    def _finish_stopped_turn(self) -> None:
        """Close out a turn the user stopped, without reporting it as failed."""
        self._earcons.stop_progress()
        partial = self._streamed_assistant.strip()
        if self._turns and not self._turns[-1].response:
            self._turns[-1].response = partial
        if self._stream_response is not None:
            for row in self._rows:
                if row.response_number == self._stream_response and row.kind == "header":
                    row.payload = _strip_noise(partial)
                    break
        self._stream_response = None
        self._refresh_list()
        self._announce("Stopped")

    def _build_send_text(self, prompt: str) -> str:
        """Combine the prompt with file paths for the selected coding agent."""
        parts = [prompt] if prompt else []
        if self._attachments:
            listing = "\n".join(self._attachments)
            parts.append("Attached files (please read them):\n" + listing)
        return "\n\n".join(parts)

    def clear_conversation(self) -> None:
        """Forget this conversation and start a fresh one in the same tab.

        The backend is not told anything: dropping its session id is what makes
        the next message the first of a new conversation. The old conversation
        is still on disk, and Recent Conversations can bring it back.
        """
        if self._worker is not None and self._worker.is_alive():
            self._announce("Error: Stop the running task before starting a new conversation")
            return
        self._session_id = None
        self._turns = []
        self._rows = []
        self._displayed = []
        self._response_count = 0
        self._stream_response = None
        self._streamed_assistant = ""
        self._refresh_list()
        self._announce("New conversation started. The previous one is in Recent Conversations")

    def compact_conversation(self) -> None:
        """Ask the backend to summarise this conversation in place.

        Compaction replaces the conversation so far with a summary of it, which
        is how a long session keeps going once its context window fills up.
        Claude Code takes it as a message; Codex has a request of its own for
        it; FreeBuff's CLI cannot do it at all.
        """
        backend = self.selected_backend()
        request = compaction_request(backend)
        if request is None:
            self._announce(
                f"Error: {backend_label(backend)} cannot compact a conversation. "
                "Start a new conversation instead"
            )
            return
        if self._worker is not None and self._worker.is_alive():
            self._announce("Error: Wait for the running task to finish before compacting")
            return
        if not self._session_id or backend != self._session_backend:
            self._announce("Error: There is no conversation to compact yet")
            return
        text, extra = request
        self.prompt.SetValue(text)
        self._announce("Compacting the conversation")
        self._on_send(worker_extra=extra)

    def restore_history(self, entry: HistoryEntry, turns: List[HistoryTurn]) -> None:
        """Put a past conversation back in this tab, ready to be continued.

        Rows are rebuilt the way a live turn builds them — the user's own
        message, then the answer segmented into navigable rows — so a
        conversation from last week reads exactly like one that just finished.
        Adopting the backend's own session id is what makes the next message a
        continuation of it rather than the start of something new.
        """
        self._session_id = entry.session_id
        self._session_backend = normalize_backend(entry.backend)
        self._turns = [Turn(prompt=turn.prompt, response=turn.response) for turn in turns]
        self._rows = []
        self._displayed = []
        self._search_term = ""
        self._response_count = 0
        self._stream_response = None
        self._streamed_assistant = ""
        self._assistant_narrated_this_turn = False
        for turn in turns:
            self._response_count += 1
            number = self._response_count
            if turn.prompt.strip():
                self._rows.append(
                    Row(
                        kind="you",
                        label=f"You: {' '.join(turn.prompt.split())}",
                        payload=turn.prompt,
                        response_number=number,
                    )
                )
            self._rows.extend(parse_response(turn.response, number))
        self._refresh_list()
        # Picks up the restored session: relabels the backend line, and gives
        # FreeBuff's terminal a head start on the conversation being resumed.
        self.backend_changed()
        responses = (
            "1 response" if self._response_count == 1 else f"{self._response_count} responses"
        )
        self._set_status(f"Resumed: {entry.title} — {responses}")

    def _on_session_started(self, session_id: str) -> None:
        if not self._session_id:
            self._session_id = session_id

    def _begin_stream_response(self) -> int:
        """Open a new response (header row) the first time a turn produces output.

        Returns the response number so the streamed rows group under it.
        """
        if self._stream_response is None:
            self._response_count += 1
            self._stream_response = self._response_count
            self._rows.append(
                Row(
                    kind="header",
                    label=f"Response {self._response_count}",
                    payload="",
                    response_number=self._response_count,
                )
            )
        return self._stream_response

    def _on_activity(self, kind: str, text: str, *, refresh: bool = True) -> None:
        """Stream real content into the list as it arrives during a turn.

        ``kind == "assistant"`` is the backend's narration/answer text (segmented
        into prose and code rows). ``kind == "thinking"`` is its reasoning about
        what to do next. ``kind == "tool"`` is an action line for a tool it just
        invoked. ``kind == "result"`` is that tool's actual output (file
        contents, command output), shown as one row whose payload is the full
        result.

        Prose, thinking, and tool steps are spoken as they arrive so the user
        follows the work by ear; a tool result only speaks its short preview
        line, since results run to hundreds of lines.

        With live activity switched off in Options, none of this happens and the
        whole response lands at the end instead.
        """
        if not SETTINGS.live_rows:
            return
        n = self._begin_stream_response()
        if kind == "result":
            self._rows.append(
                Row(
                    kind="result",
                    label=_result_label(text),
                    payload=text,
                    response_number=n,
                )
            )
            self._say(_result_label(text))
        elif kind == "tool":
            self._rows.append(Row(kind="tool", label=text, payload=text, response_number=n))
            self._say(text)
        elif kind == "thinking":
            # Reasoning is the backend talking to itself. It is off by default:
            # it roughly doubles what has to be listened through before the
            # answer, and it is not the answer.
            if not SETTINGS.show_thinking:
                return
            # Read as plain text: the word "Thinking" in front of every one of
            # these lines is repeated far more often than it is informative.
            flat = " ".join(text.split())
            self._rows.append(
                Row(
                    kind="thinking",
                    label=flat,
                    payload=text,
                    response_number=n,
                )
            )
            self._say(flat)
        else:
            # Reuse the Markdown segmenter; drop its header (index 0) since this
            # turn already has one. The first row of each incoming message is
            # Mark the first row with the active backend, the way "You:" marks
            # the user's own messages.
            speaker = backend_label(self._session_backend)
            segments = parse_response(text, n)[1:]
            for i, row in enumerate(segments):
                if i == 0 and row.kind != "code":
                    row.label = f"{speaker}: {row.label}"
                self._rows.append(row)
            self._streamed_assistant += ("\n\n" if self._streamed_assistant else "") + text
            if self._say(f"{speaker}. {' '.join(text.split())}"):
                self._assistant_narrated_this_turn = True
        if refresh:
            self._refresh_list()

    def _say(self, text: str) -> bool:
        """Speak live activity, and mirror a short form to the status bar.

        Only the visible tab narrates — a background session talking over the
        one being read would be unusable.
        """
        self._set_status(text[:99] + "…" if len(text) > 100 else text)
        if not SETTINGS.speak_live:
            return False
        notebook = self.GetParent()
        if isinstance(notebook, wx.Notebook) and notebook.GetCurrentPage() is not self:
            return False
        announce(text)
        return True

    def _narrate_completed_response(self, text: str) -> None:
        """Speak a final answer when no assistant activity was narrated live."""
        if not SETTINGS.speak_live or self._assistant_narrated_this_turn or not text.strip():
            return
        speaker = backend_label(self._session_backend)
        if self._say(f"{speaker}. {' '.join(text.split())}"):
            self._assistant_narrated_this_turn = True

    def _on_response_complete(self, text: str) -> None:
        # The turn beat the cancellation, so it is a normal response.
        self._stopping = False
        # Stop the in-progress loop and play the "received" cue.
        self._earcons.play_received()
        self._narrate_completed_response(text)
        if self._turns:
            self._turns[-1].response = text
        if self._stream_response is None:
            # Silent-until-response mode, or no streamed output arrived — parse the final text
            # into a fresh response so nothing is lost.
            self._response_count += 1
            new_rows = parse_response(text, self._response_count)
            self._rows.extend(new_rows)
            self._stream_response = None
            self._refresh_list()
            self._set_status(
                f"Response {self._response_count} received, {len(new_rows) - 1} segments"
            )
            return
        else:
            # Fill the header payload so 'copy whole response' yields Claude's
            # full answer text (the streamed rows are already in the list).
            for row in self._rows:
                if row.response_number == self._stream_response and row.kind == "header":
                    row.payload = _strip_noise(text)
                    break
            # Streaming is best-effort: a backend can finish with text that
            # never arrived as activity. Without this the answer would exist
            # only in the header payload, and the list would end on whatever
            # the last streamed row happened to be.
            if text.strip() and _flatten(text) not in _flatten(self._streamed_assistant):
                speaker = backend_label(self._session_backend)
                segments = parse_response(text, self._stream_response)[1:]
                for i, row in enumerate(segments):
                    if i == 0 and row.kind != "code":
                        row.label = f"{speaker}: {row.label}"
                    self._rows.append(row)
        n = self._response_count
        self._stream_response = None
        self._refresh_list()
        self._set_status(f"Response {n} received")

    def _on_failed(self, message: str) -> None:
        if self._stopping:
            # A cancelled backend reports its own interruption. The user asked
            # for it, so it is not news, and it is not an error.
            return
        self._earcons.stop_progress()
        if self._turns and not self._turns[-1].response:
            self._turns.pop()
        self._stream_response = None
        self._announce(f"Error: {message}")

    def _on_worker_finished(self) -> None:
        # Safety net: make sure the loop is never left running.
        self._earcons.stop_progress()
        if self._stopping:
            self._stopping = False
            self._finish_stopped_turn()
        if self.send_btn:
            self.send_btn.Enable()
        if self.steer_btn:
            self.steer_btn.Disable()
        if self.stop_btn:
            self.stop_btn.Disable()
        self._worker = None

    # ----- List + find -----
    def _refresh_list(self) -> None:
        # Replacing a native ListBox's contents clears its selection. Preserve
        # the row first so incoming output never disrupts someone who is
        # reading older rows with NVDA.
        keep = self._selected_row()
        term = self._search_term.lower()
        labels: List[str] = []
        self._displayed = []
        for row in self._rows:
            if term and term not in row.payload.lower() and term not in row.label.lower():
                continue
            labels.append(row.label)
            self._displayed.append(row)
        if SETTINGS.text_view:
            # One row per line, so a line number is a row number. Labels are
            # already flattened, but a stray newline would break that mapping.
            text = "\n".join(" ".join(label.split()) for label in labels)
            self.responses_text.ChangeValue(text)
        else:
            self.responses.Set(labels)
        if keep != wx.NOT_FOUND and labels:
            self._select_row(keep)

    def open_find(self) -> None:
        """Find-in-responses popup (File menu / Cmd-Ctrl+F). Blank clears it."""
        with wx.TextEntryDialog(
            self,
            "Search responses (leave blank to show all):",
            "Find in Responses",
            self._search_term,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            self._search_term = dlg.GetValue().strip()
        self._refresh_list()
        if self._search_term:
            self._set_status(
                f"Showing {len(self._displayed)} of {len(self._rows)} rows for '{self._search_term}'"
            )
            if self._row_count() > 0:
                self._focus_row(0)
        else:
            self._set_status("Search cleared")

    def _on_list_key(self, event: wx.KeyEvent) -> None:
        """Row keys for both responses controls — the list box and the
        read-only edit field, where a line is a row."""
        key = event.GetKeyCode()
        sel = self._selected_row()

        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            if sel != wx.NOT_FOUND:
                self._open_row(sel)
            return

        if key == wx.WXK_WINDOWS_MENU:
            self._show_row_menu()
            return

        if key == wx.WXK_DOWN:
            if event.CmdDown():
                if sel != wx.NOT_FOUND:
                    self._jump_to_next_response(sel)
                return
            # The responses are one focus region. At the bottom, consume Down
            # and remain on the final row; only Tab may enter the prompt.
            if sel != wx.NOT_FOUND and sel == self._row_count() - 1:
                return
            event.Skip()
            return

        if key == wx.WXK_UP and event.CmdDown():
            if sel != wx.NOT_FOUND:
                self._jump_to_prev_response(sel)
            return

        # Plain 'c' copies the row; Shift+C copies the whole response. Modifier
        # combos (Cmd/Ctrl/Alt + C) fall through to the platform default.
        if (
            key == ord("C")
            and not event.CmdDown()
            and not event.ControlDown()
            and not event.AltDown()
        ):
            if sel != wx.NOT_FOUND:
                if event.ShiftDown():
                    self._copy_response(sel)
                else:
                    self._copy_row(sel)
            return

        event.Skip()

    def _on_list_activate(self, event: wx.CommandEvent) -> None:
        self._open_row(event.GetSelection())

    # ----- Row actions -----
    def _open_row(self, sel: int) -> None:
        if not (0 <= sel < len(self._displayed)):
            return
        row = self._displayed[sel]
        title = row.label if row.kind != "header" else f"Response {row.response_number}"
        dlg = ReadView(self, row.payload, title)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()
        self._focus_row(sel)

    def _copy_row(self, sel: int) -> None:
        if not (0 <= sel < len(self._displayed)):
            return
        row = self._displayed[sel]
        if not _copy_to_clipboard(row.payload):
            self._set_status("Error: Could not access clipboard")
            return
        self._announce(self._copy_message(row))

    def _copy_response(self, sel: int) -> None:
        if not (0 <= sel < len(self._displayed)):
            return
        row = self._displayed[sel]
        text = reassemble(self._rows, row.response_number)
        if not _copy_to_clipboard(text):
            self._set_status("Error: Could not access clipboard")
            return
        self._announce(f"Copied whole response {row.response_number}")

    @staticmethod
    def _copy_message(row: Row) -> str:
        if row.kind == "code":
            n = row.payload.count("\n") + 1 if row.payload else 0
            unit = "line" if n == 1 else "lines"
            if row.language:
                return f"Copied {n} {unit} of {row.language}"
            return f"Copied {n} {unit} of code"
        if row.kind == "header":
            return f"Copied response {row.response_number}"
        if row.kind == "you":
            return "Copied your message"
        names = {
            "heading": "heading",
            "list": "list",
            "quote": "quote",
            "result": "result",
            "thinking": "thinking",
            "tool": "tool step",
        }
        return f"Copied {names.get(row.kind, 'paragraph')}"

    # ----- Per-row actions menu -----
    def _show_row_menu(self) -> None:
        """Arrowable actions for the focused row (Menu key / context gesture)."""
        sel = self._selected_row()
        if not (0 <= sel < len(self._displayed)):
            return
        row = self._displayed[sel]
        menu = wx.Menu()
        if row.kind == "code":
            item = menu.Append(wx.ID_ANY, "Save code to file…")
            self.Bind(wx.EVT_MENU, lambda _e, r=row: self._action_save_code(r), item)
        insert_item = menu.Append(wx.ID_ANY, "Insert into prompt")
        self.Bind(wx.EVT_MENU, lambda _e, r=row: self._action_insert(r), insert_item)
        copy_item = menu.Append(wx.ID_ANY, "Copy whole response")
        self.Bind(wx.EVT_MENU, lambda _e, r=row: self._action_copy_response(r), copy_item)
        copy_all_item = menu.Append(wx.ID_ANY, "Copy whole conversation")
        self.Bind(wx.EVT_MENU, lambda _e: self._action_copy_conversation(), copy_all_item)
        self._responses_ctrl().PopupMenu(menu)
        menu.Destroy()

    def _action_save_code(self, row: Row) -> None:
        ext = _LANG_EXT.get(row.language or "", ".txt")
        with wx.FileDialog(
            self,
            "Save code to file",
            defaultDir=self.cwd,
            defaultFile="snippet" + ext,
            wildcard="All files (*.*)|*.*",
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = dlg.GetPath()
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(row.payload)
        except OSError as exc:
            self._set_status(f"Error saving file: {exc}")
            return
        self._announce(f"Saved code to {os.path.basename(path)}")

    def _action_insert(self, row: Row) -> None:
        current = self.prompt.GetValue()
        sep = "\n" if current and not current.endswith("\n") else ""
        self.prompt.SetValue(current + sep + row.payload)
        self.prompt.SetInsertionPointEnd()
        self.prompt.SetFocus()
        self._announce("Inserted into prompt")

    def _action_copy_response(self, row: Row) -> None:
        text = reassemble(self._rows, row.response_number)
        if not _copy_to_clipboard(text):
            self._set_status("Error: Could not access clipboard")
            return
        self._announce(f"Copied whole response {row.response_number}")

    def _action_copy_conversation(self) -> None:
        """Every row in the list, first to last, on the clipboard."""
        if not self._rows:
            self._set_status("Error: Nothing to copy yet")
            return
        text = reassemble_all(self._rows)
        if not _copy_to_clipboard(text):
            self._set_status("Error: Could not access clipboard")
            return
        n = len(self._rows)
        self._announce(f"Copied whole conversation, {n} {'row' if n == 1 else 'rows'}")

    # ----- Response navigation -----
    def jump_to_latest_response(self) -> None:
        """Cycle through response headers on each Cmd+R press.

        First press goes to the latest response. Subsequent presses cycle
        backwards through older responses, wrapping from the first back to
        the latest. This lets the user step through every response with the
        same key without touching arrow keys.
        """
        headers = [i for i, r in enumerate(self._displayed) if r.kind == "header"]
        if not headers:
            return
        cur = self._selected_row()
        # Find which header slot we're currently on (if any)
        if cur in headers:
            pos = headers.index(cur)
            # Step backwards; wrap from first header back to last (latest)
            nxt = headers[(pos - 1) % len(headers)]
        else:
            # Not on a header — jump to latest first
            nxt = headers[-1]
        self._focus_row(nxt)
        announce(self._displayed[nxt].label)

    def _jump_to_prev_response(self, current_sel: int) -> None:
        for i in range(current_sel - 1, -1, -1):
            if self._displayed[i].kind == "header":
                self._focus_row(i)
                announce(self._displayed[i].label)
                return

    def _jump_to_next_response(self, current_sel: int) -> None:
        for i in range(current_sel + 1, len(self._displayed)):
            if self._displayed[i].kind == "header":
                self._focus_row(i)
                announce(self._displayed[i].label)
                return

    # ----- Cleanup hook -----
    def cancel_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            self._worker.cancel()
            self._worker.join(timeout=3)


class SetupWizard(wx.Dialog):
    """Choose, install, and authenticate a BlindPilot backend."""

    _STEPS = ["Welcome", "Coding Agent CLI", "Sign In", "Projects Folder", "All Done"]

    def __init__(
        self,
        parent: Optional[wx.Window],
        initial_projects_folder: Optional[str] = None,
        initial_backend: str = BACKEND_CLAUDE,
    ):
        super().__init__(
            parent,
            title="BlindPilot — Setup",
            size=(580, 400),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.projects_folder: Optional[str] = initial_projects_folder
        self.backend = normalize_backend(initial_backend)
        self._step = 0
        self._backend_path: Optional[str] = None
        self._login_thread: Optional[threading.Thread] = None

        self._step_label = wx.StaticText(self, label="")
        f = self._step_label.GetFont()
        f.SetWeight(wx.FONTWEIGHT_BOLD)
        f.SetPointSize(f.GetPointSize() + 2)
        self._step_label.SetFont(f)

        self._book = wx.Simplebook(self)
        self._pages = [
            self._make_welcome(),
            self._make_cli(),
            self._make_signin(),
            self._make_projects(),
            self._make_done(),
        ]
        for page in self._pages:
            self._book.AddPage(page, "")
        self._refresh_backend_copy()

        self._back_btn = wx.Button(self, label="Back")
        self._next_btn = wx.Button(self, label="Next")
        self._cancel_btn = wx.Button(self, wx.ID_CANCEL, "Cancel")
        self._back_btn.Bind(wx.EVT_BUTTON, lambda _e: self._go(-1))
        self._next_btn.Bind(wx.EVT_BUTTON, lambda _e: self._go(+1))

        nav = wx.BoxSizer(wx.HORIZONTAL)
        nav.Add(self._cancel_btn, 0)
        nav.AddStretchSpacer()
        nav.Add(self._back_btn, 0, wx.RIGHT, 8)
        nav.Add(self._next_btn, 0)

        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(self._step_label, 0, wx.ALL, 14)
        root.Add(self._book, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 14)
        root.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.TOP, 8)
        root.Add(nav, 0, wx.EXPAND | wx.ALL, 14)
        self.SetSizer(root)

        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self._show_step(0)

    # ---- page builders ----

    def _make_welcome(self) -> wx.Panel:
        p = wx.Panel(self._book)
        self._welcome_text = wx.StaticText(
            p,
            label=(
                "Welcome to BlindPilot.\n\n"
                "Claude Code is the default backend. This wizard checks that its CLI is installed and that "
                "you are signed in, then optionally points the app at your projects folder.\n\n"
                "You can choose Codex or FreeBuff later from File, Backend. "
                "The whole process takes about a minute."
            ),
        )
        self._welcome_text.Wrap(520)
        backend_label_widget = wx.StaticText(p, label="&Backend:")
        self._setup_backend_picker = wx.Choice(
            p, choices=[BACKEND_LABELS[value] for value in BACKEND_IDS]
        )
        self._setup_backend_picker.SetName("Backend")
        self._setup_backend_picker.SetSelection(BACKEND_IDS.index(self.backend))
        self._setup_backend_picker.Bind(wx.EVT_CHOICE, self._on_backend_choice)
        picker_row = wx.BoxSizer(wx.HORIZONTAL)
        picker_row.Add(backend_label_widget, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        picker_row.Add(self._setup_backend_picker, 1)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(self._welcome_text, 0, wx.ALL, 8)
        s.Add(picker_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        p.SetSizer(s)
        return p

    def _make_cli(self) -> wx.Panel:
        p = wx.Panel(self._book)
        self._cli_status = wx.StaticText(p, label="Checking for Claude Code…")
        self._cli_status.Wrap(520)
        self._cli_detail = wx.StaticText(p, label="")
        self._cli_detail.Wrap(520)

        self._cli_install_btn = wx.Button(p, label="Install backend")
        self._cli_install_btn.Bind(wx.EVT_BUTTON, lambda _e: self._install_cli())
        self._cli_install_btn.Hide()
        self._cli_update_btn = wx.Button(p, label="Update backend")
        self._cli_update_btn.Bind(wx.EVT_BUTTON, lambda _e: self._update_cli())
        self._cli_update_btn.Hide()
        self._cli_path_btn = wx.Button(p, label="Add to PATH")
        self._cli_path_btn.Bind(wx.EVT_BUTTON, lambda _e: self._repair_path())
        self._cli_path_btn.Hide()
        self._cli_check_btn = wx.Button(p, label="Check Again")
        self._cli_check_btn.Bind(wx.EVT_BUTTON, lambda _e: self._check_cli())
        self._cli_check_btn.Hide()

        # Read-only multiline field rather than a label: NVDA can review the
        # installer's output line by line, and it stays reachable by Tab.
        self._cli_log = wx.TextCtrl(
            p,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP,
        )
        self._cli_log.SetName("Installer output")
        self._cli_log.Hide()

        btns = wx.BoxSizer(wx.HORIZONTAL)
        btns.Add(self._cli_install_btn, 0, wx.RIGHT, 8)
        btns.Add(self._cli_update_btn, 0, wx.RIGHT, 8)
        btns.Add(self._cli_path_btn, 0, wx.RIGHT, 8)
        btns.Add(self._cli_check_btn, 0)

        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(self._cli_status, 0, wx.ALL, 8)
        s.Add(self._cli_detail, 0, wx.LEFT | wx.BOTTOM, 8)
        s.Add(btns, 0, wx.LEFT | wx.BOTTOM, 8)
        s.Add(self._cli_log, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        p.SetSizer(s)
        return p

    def _make_signin(self) -> wx.Panel:
        p = wx.Panel(self._book)
        self._signin_intro = wx.StaticText(
            p,
            label=(
                "BlindPilot needs you to be signed in to use the Claude Code backend.\n\n"
                "If you have already run 'claude /login' in your terminal and it worked, "
                "click Already Signed In to skip this step.\n\n"
                "Otherwise click Sign In — your browser will open to complete authentication."
            ),
        )
        self._signin_intro.Wrap(520)
        self._signin_status = wx.StaticText(p, label="")
        self._signin_status.Wrap(520)
        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self._signin_btn = wx.Button(p, label="Sign In")
        self._signin_btn.Bind(wx.EVT_BUTTON, lambda _e: self._do_login())
        self._already_btn = wx.Button(p, label="Already Signed In")
        self._already_btn.Bind(wx.EVT_BUTTON, lambda _e: self._go(+1))
        btn_row.Add(self._signin_btn, 0, wx.RIGHT, 12)
        btn_row.Add(self._already_btn, 0)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(self._signin_intro, 0, wx.ALL, 8)
        s.Add(self._signin_status, 0, wx.LEFT | wx.BOTTOM, 8)
        s.Add(btn_row, 0, wx.LEFT, 8)
        p.SetSizer(s)
        return p

    def _make_projects(self) -> wx.Panel:
        p = wx.Panel(self._book)
        intro = wx.StaticText(
            p,
            label=(
                "Optionally choose the folder that contains all your projects "
                "(for example your 'development' or 'repos' folder). "
                "New Session starts its Browse button there.\n\n"
                "You can skip this and set it later from the File menu."
            ),
        )
        intro.Wrap(520)
        self._proj_label = wx.StaticText(p, label=self._proj_display())
        choose_btn = wx.Button(p, label="Choose Folder…")
        choose_btn.Bind(wx.EVT_BUTTON, lambda _e: self._pick_folder())
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(intro, 0, wx.ALL, 8)
        s.Add(self._proj_label, 0, wx.LEFT | wx.BOTTOM, 8)
        s.Add(choose_btn, 0, wx.LEFT, 8)
        p.SetSizer(s)
        return p

    def _make_done(self) -> wx.Panel:
        p = wx.Panel(self._book)
        self._done_text = wx.StaticText(
            p,
            label=(
                "All done! BlindPilot is ready.\n\n"
                "Type in the Prompt field and press Enter to send.\n"
                "Press Cmd+R to jump to the latest response.\n"
                "Press Cmd+/ to pick a slash command.\n"
                "Press Cmd+Shift+M to cycle permission modes.\n"
                "Type /model to choose the model and effort level.\n\n"
                "Click Finish to open the app."
            ),
        )
        self._done_text.Wrap(520)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(self._done_text, 0, wx.ALL, 8)
        p.SetSizer(s)
        return p

    def _refresh_backend_copy(self) -> None:
        """Update every wizard page for the backend chosen on Welcome."""
        info = BACKENDS[self.backend]
        label = info.label
        login = " ".join((info.executable, *info.login_args))
        self._welcome_text.SetLabel(
            "Welcome to BlindPilot.\n\n"
            "Choose the coding-agent backend you want to use first. This wizard "
            "checks its CLI, helps install or update it, checks sign-in, and optionally "
            "points BlindPilot at your projects folder.\n\n"
            "You can switch or manage backends later from the File menu."
        )
        self._welcome_text.Wrap(520)
        self._cli_install_btn.SetLabel(f"Install {label}")
        self._cli_update_btn.SetLabel(f"Update {label}")
        self._signin_intro.SetLabel(
            f"BlindPilot needs you to be signed in to use {label}.\n\n"
            f"If you have already run '{login}' in a terminal, choose Already "
            "Signed In. Otherwise choose Sign In and complete any browser or "
            "terminal authentication that opens."
        )
        self._signin_intro.Wrap(520)
        limitations = ""
        if not info.supports_model:
            limitations += "\nFreeBuff manages model selection in its own terminal UI."
        if not info.supports_permissions:
            limitations += "\nFreeBuff manages permissions internally."
        self._done_text.SetLabel(
            f"All done! BlindPilot is ready to use {label}.\n\n"
            "Type in the Prompt field and press Enter to send.\n"
            "Press Ctrl+R to jump to the latest response.\n"
            "Press Ctrl+/ to pick a slash command.\n"
            "Press Ctrl+period to stop a task that is running.\n"
            "Press Ctrl+Shift+M to cycle permission modes when supported.\n"
            "Type /model to choose the model and effort level when supported."
            f"{limitations}\n\nChoose Finish to open the app."
        )
        self._done_text.Wrap(520)
        for page in self._pages:
            page.Layout()

    def _on_backend_choice(self, _event: wx.CommandEvent) -> None:
        selection = self._setup_backend_picker.GetSelection()
        if not (0 <= selection < len(BACKEND_IDS)):
            return
        self.backend = BACKEND_IDS[selection]
        self._backend_path = None
        self._signin_status.SetLabel("")
        self._refresh_backend_copy()
        self.Layout()
        announce(f"Backend selected: {backend_label(self.backend)}")

    def _find_selected_cli(self) -> Optional[str]:
        if self.backend == BACKEND_CLAUDE:
            return _find_claude()
        return find_backend_cli(self.backend)

    def _selected_install_argv(self) -> Optional[List[str]]:
        if self.backend == BACKEND_CLAUDE:
            return _install_argv()
        return _npm_install_argv(self.backend)

    # ---- navigation ----

    def _show_step(self, step: int) -> None:
        self._step = step
        self._book.SetSelection(step)
        n = len(self._STEPS)
        title = f"{backend_label(self.backend)} CLI" if step == 1 else self._STEPS[step]
        self._step_label.SetLabel(f"Step {step + 1} of {n}: {title}")
        self._back_btn.Enable(step > 0)
        if step == n - 1:
            self._next_btn.SetLabel("Finish")
        else:
            self._next_btn.SetLabel("Next")
        self._next_btn.Enable(True)
        if step == 1:
            wx.CallAfter(self._check_cli)
        elif step == 2:
            wx.CallAfter(self._check_signin)
        self.Layout()
        announce(f"Step {step + 1} of {n}: {title}")

    def _go(self, direction: int) -> None:
        target = self._step + direction
        if target < 0:
            return
        if target >= len(self._STEPS):
            self.EndModal(wx.ID_OK)
            return
        self._show_step(target)

    def _on_key(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        event.Skip()

    # ---- CLI step ----

    def _check_cli(self) -> None:
        if self.backend != BACKEND_CLAUDE:
            self._check_npm_backend_cli()
            return
        self._backend_path = self._find_selected_cli()
        windows = platform.system() == "Windows"
        # Spoken after the status line — the labels are long, and what the user
        # needs to hear is which button to Tab to.
        hint = ""

        if self._backend_path:
            folder = Path(self._backend_path).parent
            on_path = _is_on_persistent_path(folder)
            self._cli_status.SetLabel("Claude Code found:")
            if on_path:
                self._cli_detail.SetLabel(self._backend_path)
                self._cli_path_btn.Hide()
            else:
                # Reachable from this app but not from a terminal — worth
                # fixing, since /login and everything else assume a shell.
                self._cli_detail.SetLabel(
                    f"{self._backend_path}\n\n"
                    f"{folder} is not on your PATH, so typing 'claude' in "
                    f"{_path_shells()} will not work. "
                    "Click Add to PATH to fix that."
                )
                self._cli_path_btn.Show()
                hint = (
                    f"But {folder} is not on your PATH, so 'claude' will not "
                    "work in a terminal. Tab to the Add to PATH button to fix it."
                )
            self._cli_install_btn.Hide()
            self._cli_update_btn.Show()
            self._cli_check_btn.Hide()
            self._next_btn.Enable(True)
        elif _install_argv() is not None:
            flavour = "native Windows version" if windows else "native version"
            self._cli_status.SetLabel("Claude Code is not installed on this computer.")
            self._cli_detail.SetLabel(
                "BlindPilot's default backend needs it. Click Install Claude Code and it "
                f"will be installed for you — the {flavour}, no administrator "
                "rights and no Node.js needed. It is put on your PATH so "
                f"'claude' also works in {_path_shells()}.\n\n"
                "You can also install it yourself from claude.com/claude-code "
                "and click Check Again. To use Codex or FreeBuff instead, press "
                "Escape and choose it from File, Backend in the main window."
            )
            self._cli_install_btn.Show()
            self._cli_update_btn.Hide()
            self._cli_path_btn.Hide()
            self._cli_check_btn.Show()
            self._next_btn.Enable(False)
            hint = (
                "Tab to the Install Claude Code button to install it now. "
                "It needs no administrator rights and is put on your PATH. "
                "Or press Escape to use another backend."
            )
        else:
            # No PowerShell, or no curl — nothing to drive an install with.
            command = (
                f"irm {WINDOWS_INSTALL_PS1_URL} | iex"
                if windows
                else f"curl -fsSL {POSIX_INSTALL_SH_URL} | bash"
            )
            self._cli_status.SetLabel("Claude Code CLI was not found on this computer.")
            self._cli_detail.SetLabel(
                f"{_missing_prereq_message()}\n\n"
                f"Install Claude Code by running this in a terminal:\n\n"
                f"{command}\n\n"
                "then click Check Again. To use Codex or FreeBuff instead, press "
                "Escape and choose it from File, Backend in the main window."
            )
            self._cli_install_btn.Hide()
            self._cli_update_btn.Hide()
            self._cli_path_btn.Hide()
            self._cli_check_btn.Show()
            self._next_btn.Enable(False)

        self._cli_detail.Wrap(520)
        self._pages[1].Layout()
        self.Layout()
        announce(" ".join(filter(None, (self._cli_status.GetLabel(), hint))))

    def _check_npm_backend_cli(self) -> None:
        """Check Codex or FreeBuff without showing Claude-specific guidance."""
        info = BACKENDS[self.backend]
        self._backend_path = self._find_selected_cli()
        hint = ""
        if self._backend_path:
            folder = Path(self._backend_path).parent
            on_path = _is_on_persistent_path(folder)
            self._cli_status.SetLabel(f"{info.label} found:")
            self._cli_detail.SetLabel(self._backend_path)
            self._cli_install_btn.Hide()
            self._cli_update_btn.Show()
            self._cli_check_btn.Hide()
            if on_path:
                self._cli_path_btn.Hide()
            else:
                self._cli_detail.SetLabel(
                    f"{self._backend_path}\n\n{folder} is not on your persistent "
                    f"PATH. Choose Add to PATH so '{info.executable}' also works "
                    f"in {_path_shells()}."
                )
                self._cli_path_btn.Show()
                hint = "Tab to Add to PATH to make the CLI available in new terminals."
            self._next_btn.Enable(True)
        elif self._selected_install_argv() is not None:
            self._cli_status.SetLabel(f"{info.label} is not installed.")
            self._cli_detail.SetLabel(
                f"Choose Install {info.label} to run:\n\n{info.install_command}\n\n"
                "You can also run that command in a terminal and choose Check Again."
            )
            self._cli_install_btn.Show()
            self._cli_update_btn.Hide()
            self._cli_path_btn.Hide()
            self._cli_check_btn.Show()
            self._next_btn.Enable(False)
            hint = f"Tab to Install {info.label}."
        else:
            self._cli_status.SetLabel(f"{info.label} was not found.")
            self._cli_detail.SetLabel(
                f"npm is required for automatic installation. Install Node.js and npm, "
                f"then run:\n\n{info.install_command}\n\nThen choose Check Again, "
                "or go Back and select another backend."
            )
            self._cli_install_btn.Hide()
            self._cli_update_btn.Hide()
            self._cli_path_btn.Hide()
            self._cli_check_btn.Show()
            self._next_btn.Enable(False)
        self._cli_detail.Wrap(520)
        self._pages[1].Layout()
        self.Layout()
        announce(" ".join(filter(None, (self._cli_status.GetLabel(), hint))))

    def _cli_log_line(self, text: str) -> None:
        """Append a line of installer output and speak it."""
        if not self._cli_log.IsShown():
            self._cli_log.Show()
            self._pages[1].Layout()
        self._cli_log.AppendText(text + "\n")
        announce(text)

    def _repair_path(self) -> None:
        if not self._backend_path:
            return
        folder = Path(self._backend_path).parent
        try:
            changed = ensure_on_path(folder)
        except OSError as exc:
            self._cli_log_line(f"Could not update your PATH: {exc}")
            return
        self._cli_log_line(
            f"Added {folder} to {changed}. Open a new terminal window to use it."
            if changed
            else f"{folder} was already on your PATH."
        )
        self._cli_path_btn.Hide()
        self._check_cli()

    # ---- Installing the CLI (Windows) ----

    def _install_cli(self) -> None:
        label = backend_label(self.backend)
        self._cli_install_btn.Disable()
        self._cli_update_btn.Disable()
        self._cli_check_btn.Disable()
        self._back_btn.Disable()
        self._next_btn.Disable()
        self._cli_status.SetLabel(f"Installing {label}...")
        self._cli_log.Show()
        self._cli_log.SetValue("")
        self._pages[1].Layout()
        self.Layout()
        announce(f"Installing {label}. This usually takes under a minute.")
        threading.Thread(target=self._run_install, daemon=True).start()

    def _run_install(self) -> None:
        def log(text: str) -> None:
            wx.CallAfter(self._cli_log_line, text)

        try:
            binary = install_backend(self.backend, log)
        except Exception as exc:  # never leave the wizard wedged on a crash
            log(f"The install failed: {exc}")
            binary = None
        wx.CallAfter(self._on_install_done, binary)

    def _on_install_done(self, binary: Optional[str]) -> None:
        label = backend_label(self.backend)
        self._cli_install_btn.Enable()
        self._cli_update_btn.Enable()
        self._cli_check_btn.Enable()
        self._back_btn.Enable(self._step > 0)
        self._next_btn.Enable(True)
        if binary:
            self._backend_path = binary
            announce(f"{label} installed.")
        else:
            self._cli_status.SetLabel("The install did not complete.")
            announce(
                "The install did not complete. Read the installer output, or "
                f"install {label} yourself using {BACKENDS[self.backend].install_command} and "
                "click Check Again."
            )
        self._check_cli()

    def _update_cli(self) -> None:
        """Update the selected installed backend without blocking the dialog."""
        label = backend_label(self.backend)
        self._cli_install_btn.Disable()
        self._cli_update_btn.Disable()
        self._cli_check_btn.Disable()
        self._back_btn.Disable()
        self._next_btn.Disable()
        self._cli_status.SetLabel(f"Updating {label}...")
        self._cli_log.Show()
        self._cli_log.SetValue("")
        self._pages[1].Layout()
        self.Layout()
        announce(f"Updating {label}. Progress will be announced.")
        threading.Thread(target=self._run_update, daemon=True).start()

    def _run_update(self) -> None:
        def log(text: str) -> None:
            wx.CallAfter(self._cli_log_line, text)

        try:
            updated = update_backend(self.backend, log)
        except Exception as exc:  # never leave the wizard wedged on a crash
            log(f"The update failed: {exc}")
            updated = False
        wx.CallAfter(self._on_update_done, updated)

    def _on_update_done(self, updated: bool) -> None:
        label = backend_label(self.backend)
        self._cli_install_btn.Enable()
        self._cli_update_btn.Enable()
        self._cli_check_btn.Enable()
        self._back_btn.Enable(self._step > 0)
        self._next_btn.Enable(True)
        if updated:
            invalidate_model_options(self.backend)
            announce(f"{label} is up to date. Its model list will refresh at runtime.")
        else:
            self._cli_status.SetLabel(f"The {label} update did not complete.")
            announce(f"The {label} update did not complete. Review the updater output.")
        self._check_cli()

    # ---- Sign-in step ----

    def _check_signin(self) -> None:
        label = backend_label(self.backend)
        self._backend_path = self._find_selected_cli()
        if not self._backend_path:
            self._signin_status.SetLabel(
                f"{label} is not installed. Go Back and complete the CLI step first."
            )
        elif backend_auth_ok(self.backend):
            self._signin_status.SetLabel(f"{label} reports that you are signed in.")
        else:
            self._signin_status.SetLabel(f"BlindPilot could not confirm a {label} sign-in yet.")
        self._pages[2].Layout()
        self.Layout()
        announce(self._signin_status.GetLabel())

    def _do_login(self) -> None:
        if not self._backend_path:
            self._backend_path = self._find_selected_cli()
        if not self._backend_path:
            self._signin_status.SetLabel(
                f"{backend_label(self.backend)} CLI not found. "
                "Please complete the previous step first."
            )
            announce(self._signin_status.GetLabel())
            return
        self._signin_btn.Disable()
        self._already_btn.Disable()
        self._next_btn.Disable()
        self._signin_status.SetLabel(
            "Waiting for sign-in… Complete authentication in your browser, then return here."
        )
        self._pages[2].Layout()
        self.Layout()
        announce(self._signin_status.GetLabel())
        self._login_thread = threading.Thread(target=self._run_login, daemon=True)
        self._login_thread.start()

    def _run_login(self) -> None:
        binary = self._backend_path
        rc = -1
        try:
            assert binary is not None
            args = [binary, *BACKENDS[self.backend].login_args]
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                **_no_window_kwargs(),
            )
            _out, _ = proc.communicate(timeout=300)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            rc = -1
        except Exception:
            rc = -2
        wx.CallAfter(self._on_login_done, rc)

    def _on_login_done(self, rc: int) -> None:
        self._signin_btn.Enable()
        self._already_btn.Enable()
        self._next_btn.Enable()
        if rc == 0:
            self._signin_status.SetLabel("Signed in successfully.")
            wx.CallAfter(self._go, +1)
        else:
            self._signin_status.SetLabel(
                "Sign-in did not complete (or timed out). "
                "Try again, or click Already Signed In if you are authenticated."
            )
        self._pages[2].Layout()
        self.Layout()
        announce(self._signin_status.GetLabel())

    # ---- Projects step ----

    def _proj_display(self) -> str:
        return (
            f"Selected: {self.projects_folder}"
            if self.projects_folder
            else "None selected yet (optional)."
        )

    def _pick_folder(self) -> None:
        with wx.DirDialog(
            self,
            "Choose your Projects folder",
            defaultPath=self.projects_folder or os.path.expanduser("~"),
            style=wx.DD_DEFAULT_STYLE,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            self.projects_folder = dlg.GetPath()
        self._proj_label.SetLabel(self._proj_display())
        self._pages[3].Layout()
        announce(f"Projects folder: {self.projects_folder}")


class MainFrame(wx.Frame):
    def __init__(self, initial_cwd: str):
        super().__init__(None, title=APP_NAME, size=(900, 760))

        # Shared audio cues (send / in-progress loop / received).
        self.earcons = Earcons(os.path.join(_resource_dir(), "EarCons"))
        self._update_checking = False

        # Remembered "Projects folder" — the parent folder that holds the
        # user's project directories. New Session browses from there.
        cfg = _load_config()
        self._backend = normalize_backend(cfg.get("backend"))
        pf = cfg.get("projects_folder")
        self._projects_folder: Optional[str] = pf if pf and os.path.isdir(pf) else None

        # ----- Menu bar (gives us standard Cmd+T / Cmd+W on Mac) -----
        menubar = wx.MenuBar()
        file_menu = wx.Menu()
        new_item = file_menu.Append(
            wx.ID_NEW,
            "&New Session…\tCtrl+T",
            "Type or browse to a folder and open a session in it",
        )
        history_item = file_menu.Append(
            wx.ID_ANY,
            "&Recent Conversations…\tCtrl+H",
            "Reopen a past conversation and carry on with it",
        )
        backend_menu = wx.Menu()
        self._backend_items: dict[str, wx.MenuItem] = {}
        for backend in BACKEND_IDS:
            item = backend_menu.AppendRadioItem(wx.ID_ANY, BACKEND_LABELS[backend])
            item.Check(backend == self._backend)
            self._backend_items[backend] = item
            self.Bind(
                wx.EVT_MENU,
                lambda _e, chosen=backend: self._set_backend(chosen),
                item,
            )
        file_menu.AppendSubMenu(
            backend_menu,
            "&Backend",
            "Choose which coding-agent CLI BlindPilot uses",
        )
        manage_backends_item = file_menu.Append(
            wx.ID_ANY,
            "&Manage Backends...",
            "Install, update, or sign in to Claude Code, Codex, or FreeBuff",
        )
        set_pf_item = file_menu.Append(
            wx.ID_ANY,
            "Set &Projects Folder…",
            "Choose the folder that contains your projects",
        )
        desktop_item = file_menu.Append(
            wx.ID_ANY,
            "Create &Desktop Shortcut",
            "Put a BlindPilot shortcut on the desktop",
        )
        file_menu.AppendSeparator()
        self._compact_item = file_menu.Append(
            wx.ID_ANY,
            "Co&mpact Conversation\tCtrl+Shift+K",
            "Summarise this conversation so the backend has room to keep going",
        )
        new_convo_item = file_menu.Append(
            wx.ID_ANY,
            "Start N&ew Conversation\tCtrl+Shift+N",
            "Forget this conversation and start a fresh one in this tab",
        )
        file_menu.AppendSeparator()
        stop_item = file_menu.Append(
            wx.ID_STOP,
            "S&top Task\tCtrl+.",
            "Stop the task running in this session",
        )
        file_menu.AppendSeparator()
        find_item = file_menu.Append(
            wx.ID_FIND,
            "&Find in Responses…\tCtrl+F",
            "Search the responses in this session",
        )
        file_menu.AppendSeparator()
        close_item = file_menu.Append(
            wx.ID_CLOSE, "&Close Session\tCtrl+W", "Close the current session tab"
        )
        file_menu.AppendSeparator()
        quit_item = file_menu.Append(wx.ID_EXIT, "&Quit\tCtrl+Q")
        menubar.Append(file_menu, "&File")

        # ----- Options: how much of a run is narrated -----
        options_menu = wx.Menu()
        self._rows_item = options_menu.AppendCheckItem(
            wx.ID_ANY,
            "Show &live activity in the list",
            "Add rows for your message, thinking, tool steps and results while a run is working",
        )
        self._speak_item = options_menu.AppendCheckItem(
            wx.ID_ANY,
            "&Speak activity aloud",
            "Read each activity row out as it arrives",
        )
        self._thinking_item = options_menu.AppendCheckItem(
            wx.ID_ANY,
            "Include the backend's &reasoning",
            "Add the backend's own thinking to the activity. Off by default, "
            "so only its actions and its answer are shown",
        )
        options_menu.AppendSeparator()
        self._text_view_item = options_menu.AppendCheckItem(
            wx.ID_ANY,
            "Responses as a read-o&nly text field",
            "Show the responses as a read-only edit field, one row per line, "
            "so NVDA can review and select across them",
        )
        options_menu.AppendSeparator()
        silent_response_item = options_menu.Append(
            wx.ID_ANY,
            "&Silent until the response mode",
            "Turn both off: nothing appears or is spoken until the whole response is ready",
        )
        self._rows_item.Check(SETTINGS.live_rows)
        self._speak_item.Check(SETTINGS.speak_live)
        self._thinking_item.Check(SETTINGS.show_thinking)
        self._text_view_item.Check(SETTINGS.text_view)
        menubar.Append(options_menu, "&Options")

        help_menu = wx.Menu()
        update_item = help_menu.Append(
            wx.ID_ANY,
            "Check for &Updates...",
            "Check GitHub for a newer BlindPilot release",
        )
        help_menu.AppendSeparator()
        about_item = help_menu.Append(
            wx.ID_ABOUT,
            "&About BlindPilot",
            "BlindPilot version, license, and original application credit",
        )
        menubar.Append(help_menu, "&Help")

        self.SetMenuBar(menubar)
        self._refresh_compact_item()
        self.Bind(wx.EVT_MENU, lambda _e: self._toggle_live_rows(), self._rows_item)
        self.Bind(wx.EVT_MENU, lambda _e: self._toggle_speak_live(), self._speak_item)
        self.Bind(wx.EVT_MENU, lambda _e: self._toggle_show_thinking(), self._thinking_item)
        self.Bind(wx.EVT_MENU, lambda _e: self._toggle_text_view(), self._text_view_item)
        self.Bind(
            wx.EVT_MENU,
            lambda _e: self._use_silent_until_response_mode(),
            silent_response_item,
        )
        self.Bind(wx.EVT_MENU, lambda _e: self._new_session(), new_item)
        self.Bind(wx.EVT_MENU, lambda _e: self._open_history(), history_item)
        self.Bind(wx.EVT_MENU, lambda _e: self._compact_active(), self._compact_item)
        self.Bind(wx.EVT_MENU, lambda _e: self._new_conversation_active(), new_convo_item)
        self.Bind(wx.EVT_MENU, lambda _e: self._manage_backends(), manage_backends_item)
        self.Bind(wx.EVT_MENU, lambda _e: self._set_projects_folder(), set_pf_item)
        self.Bind(wx.EVT_MENU, lambda _e: self._create_desktop_shortcut(), desktop_item)
        self.Bind(wx.EVT_MENU, lambda _e: self._stop_active(), stop_item)
        self.Bind(wx.EVT_MENU, lambda _e: self._find_active(), find_item)
        self.Bind(wx.EVT_MENU, lambda _e: self._close_current_session(), close_item)
        self.Bind(wx.EVT_MENU, lambda _e: self.Close(), quit_item)
        self.Bind(wx.EVT_MENU, lambda _e: self._show_about(), about_item)
        self.Bind(wx.EVT_MENU, lambda _e: self._check_for_updates(), update_item)

        # ----- Top-level layout: session picker + notebook -----
        root = wx.Panel(self)
        root_sizer = wx.BoxSizer(wx.VERTICAL)

        picker_row = wx.BoxSizer(wx.HORIZONTAL)
        session_label = wx.StaticText(root, label="Session:")
        self.session_picker = wx.Choice(root, choices=[])
        self.session_picker.SetName("Session")
        self.session_picker.Bind(wx.EVT_CHOICE, self._on_picker_change)
        self.session_picker.Bind(wx.EVT_SET_FOCUS, self._on_picker_focus)
        picker_row.Add(session_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        picker_row.Add(self.session_picker, 1, wx.ALIGN_CENTER_VERTICAL)

        # Simplebook = a notebook with no visible tab strip; the dropdown is
        # the user-facing way to switch sessions.
        self.notebook = wx.Simplebook(root)
        self.notebook.SetName("Sessions")
        self.notebook.Bind(wx.EVT_BOOKCTRL_PAGE_CHANGED, self._on_tab_changed)

        root_sizer.Add(picker_row, 0, wx.EXPAND | wx.ALL, 8)
        root_sizer.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 4)
        root.SetSizer(root_sizer)

        self.statusbar = self.CreateStatusBar()
        self._set_status_text("Ready")

        # Shortcuts. Cmd+L / Cmd+F focus the active tab's prompt / search.
        # Cmd+Shift+] and Cmd+Shift+[ move between tabs (Mac-standard).
        # Cmd+1..9 jump directly to tab N.
        id_focus_prompt = wx.NewIdRef()
        id_next_tab = wx.NewIdRef()
        id_prev_tab = wx.NewIdRef()
        id_cycle_mode = wx.NewIdRef()
        id_attach = wx.NewIdRef()
        id_jump_response = wx.NewIdRef()
        id_slash = wx.NewIdRef()
        self.Bind(wx.EVT_MENU, lambda _e: self._focus_active("prompt"), id=id_focus_prompt)
        self.Bind(wx.EVT_MENU, lambda _e: self._cycle_tab(+1), id=id_next_tab)
        self.Bind(wx.EVT_MENU, lambda _e: self._cycle_tab(-1), id=id_prev_tab)
        self.Bind(wx.EVT_MENU, lambda _e: self._cycle_mode_active(), id=id_cycle_mode)
        self.Bind(wx.EVT_MENU, lambda _e: self._attach_active(), id=id_attach)
        self.Bind(wx.EVT_MENU, lambda _e: self._jump_to_latest_response(), id=id_jump_response)
        self.Bind(wx.EVT_MENU, lambda _e: self._slash_active(), id=id_slash)

        accel_entries = [
            wx.AcceleratorEntry(wx.ACCEL_CMD, ord("L"), id_focus_prompt),
            wx.AcceleratorEntry(wx.ACCEL_CMD | wx.ACCEL_SHIFT, ord("]"), id_next_tab),
            wx.AcceleratorEntry(wx.ACCEL_CMD | wx.ACCEL_SHIFT, ord("["), id_prev_tab),
            wx.AcceleratorEntry(wx.ACCEL_CMD | wx.ACCEL_SHIFT, ord("M"), id_cycle_mode),
            wx.AcceleratorEntry(wx.ACCEL_CMD | wx.ACCEL_SHIFT, ord("A"), id_attach),
            wx.AcceleratorEntry(wx.ACCEL_CMD, ord("R"), id_jump_response),
            wx.AcceleratorEntry(wx.ACCEL_CMD, ord("/"), id_slash),
        ]
        self._tab_jump_ids: list[wx.WindowIDRef] = []
        for n in range(1, 10):
            tid = wx.NewIdRef()
            self._tab_jump_ids.append(tid)
            self.Bind(wx.EVT_MENU, lambda _e, idx=n - 1: self._jump_to_tab(idx), id=tid)
            accel_entries.append(wx.AcceleratorEntry(wx.ACCEL_CMD, ord(str(n)), tid))

        self.SetAcceleratorTable(wx.AcceleratorTable(accel_entries))

        self._add_session(initial_cwd)

        self.Bind(wx.EVT_CLOSE, self._on_close)

    # ----- Tab management -----
    def current_backend(self) -> str:
        return self._backend

    def _set_backend(self, backend: str) -> None:
        backend = normalize_backend(backend)
        if backend == self._backend:
            return
        self._backend = backend
        for key, item in self._backend_items.items():
            item.Check(key == backend)
        cfg = _load_config()
        cfg["backend"] = backend
        _save_config(cfg)
        for index in range(self.notebook.GetPageCount()):
            page = self.notebook.GetPage(index)
            if isinstance(page, SessionPanel):
                page.backend_changed()
        self._refresh_compact_item()
        message = (
            f"Backend changed to {backend_label(backend)}. It will be used for the next new turn."
        )
        self._announce_setting(message)

    def _manage_backends(self) -> None:
        """Open the accessible setup flow for the current provider."""
        dlg = SetupWizard(
            self,
            initial_projects_folder=self._projects_folder,
            initial_backend=self._backend,
        )
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            chosen = dlg.backend
            projects_folder = dlg.projects_folder
        finally:
            dlg.Destroy()
        if projects_folder:
            self._projects_folder = projects_folder
            cfg = _load_config()
            cfg["projects_folder"] = projects_folder
            _save_config(cfg)
        self._set_backend(chosen)

    def _show_about(self) -> None:
        wx.MessageBox(
            f"{APP_NAME} {APP_VERSION}\n\n"
            "An accessible desktop frontend for Claude Code, Codex, and FreeBuff.\n\n"
            f"{ORIGINAL_APP_CREDIT}\n"
            "BlindPilot preserves and extends its accessibility-first work.\n\n"
            "Licensed under the MIT License. See LICENSE and CREDITS.md.",
            f"About {APP_NAME}",
            wx.OK | wx.ICON_INFORMATION,
            self,
        )

    def _check_for_updates(self, silent: bool = False) -> None:
        """Query GitHub off the GUI thread and present an accessible result."""
        if self._update_checking:
            if not silent:
                self._announce_setting("An update check is already running")
            return
        self._update_checking = True
        if not silent:
            self._announce_setting("Checking GitHub for BlindPilot updates")

        def work() -> None:
            release: Optional[ReleaseInfo] = None
            error = ""
            try:
                release = fetch_latest_release(APP_VERSION)
            except UpdateError as exc:
                error = str(exc)
            wx.CallAfter(self._on_update_checked, release, error, silent)

        threading.Thread(target=work, daemon=True).start()

    def check_for_updates_silently(self) -> None:
        """Startup entry point: report only an available update, never network noise."""
        self._check_for_updates(silent=True)

    def report_failed_update(self) -> None:
        """Say why the last update did not install, if it did not.

        An update finishes after BlindPilot has closed, so a failure has no
        window to report to. The helper writes the reason down and this is the
        one place it gets read out — otherwise a failed update is silent, which
        is exactly how a broken updater went unnoticed for nine releases.
        """
        reason, log = pending_failure()
        clear_pending_failure()
        if not reason:
            return
        message = f"The last update did not install: {reason}"
        if log:
            message += f"\n\nWhat happened is written down in:\n{log}"
        announce(message)
        with wx.MessageDialog(
            self, message, "BlindPilot Update", style=wx.OK | wx.ICON_WARNING
        ) as dialog:
            dialog.ShowModal()

    def _on_update_checked(self, release: Optional[ReleaseInfo], error: str, silent: bool) -> None:
        self._update_checking = False
        if error:
            if not silent:
                self._show_update_error(error)
            return
        if release is None or not release.is_newer_than(APP_VERSION):
            if not silent:
                self._announce_setting(f"BlindPilot {APP_VERSION} is the newest available version")
            return
        notes = release.notes[:1500]
        message = (
            f"BlindPilot {release.version} is available. You have {APP_VERSION}.\n\n"
            f"{notes}\n\nDownload and install this update now?"
        )
        with wx.MessageDialog(
            self,
            message,
            "BlindPilot update available",
            style=wx.YES_NO | wx.NO_DEFAULT | wx.ICON_INFORMATION,
        ) as dialog:
            if dialog.ShowModal() != wx.ID_YES:
                self._announce_setting("Update postponed")
                return
        if not getattr(sys, "frozen", False):
            wx.LaunchDefaultBrowser(release.page_url)
            self._announce_setting(
                "The release page opened. Automatic installation is used by packaged builds."
            )
            return
        self._download_release(release)

    def _download_release(self, release: ReleaseInfo) -> None:
        self._announce_setting(f"Downloading and verifying BlindPilot {release.version}")

        def work() -> None:
            last_bucket = -1

            def progress(received: int, total: int) -> None:
                nonlocal last_bucket
                percent = int(received * 100 / total) if total else 0
                bucket = percent // 25
                if bucket > last_bucket:
                    last_bucket = bucket
                    wx.CallAfter(
                        self._set_status_text,
                        f"Downloading BlindPilot update: {min(percent, 100)} percent",
                    )

            archive = None
            error = ""
            try:
                archive = download_update(release, APP_VERSION, progress=progress)
            except UpdateError as exc:
                error = str(exc)
            wx.CallAfter(self._on_update_downloaded, archive, error, release)

        threading.Thread(target=work, daemon=True).start()

    def _on_update_downloaded(
        self, archive: Optional[Path], error: str, release: ReleaseInfo
    ) -> None:
        if error or archive is None:
            self._show_update_error(error or "The update download failed.")
            return
        try:
            schedule_install(archive)
        except UpdateError as exc:
            archive.unlink(missing_ok=True)
            self._show_update_error(str(exc))
            return
        self._announce_setting(
            f"BlindPilot {release.version} is verified. Restarting to install it."
        )
        # Force the top-level frame through its normal close handler now. The
        # detached installer waits for this process and has a bounded forced
        # shutdown fallback before it replaces any application files.
        self.Close(force=True)

    def _show_update_error(self, message: str) -> None:
        self._announce_setting(f"Update error: {message}")
        wx.MessageBox(
            message,
            "BlindPilot update error",
            wx.OK | wx.ICON_ERROR,
            self,
        )

    def _add_session(self, cwd: str, initial_prompt: str = "") -> "SessionPanel":
        panel = SessionPanel(
            self.notebook,
            cwd,
            on_status=self._panel_status_changed,
            earcons=self.earcons,
            on_side_chat=self._open_side_chat,
            get_backend=self.current_backend,
        )
        self.notebook.AddPage(panel, _short_label(cwd), select=True)
        self._refresh_picker()
        # Model catalogs are intentionally lazy. FreeBuff's installed catalog
        # is embedded in a large executable, and scanning it here caused a
        # noticeable CPU spike every time BlindPilot started or opened a tab.
        # /model and /models perform the runtime refresh only when requested.
        if initial_prompt:
            panel.prompt.SetValue(initial_prompt)
            # Defer so the page is shown before the request fires.
            wx.CallAfter(panel.send_now)
        else:
            # Defer initial focus so VoiceOver picks it up after the page is shown.
            wx.CallAfter(panel.focus_prompt)
        return panel

    def _open_side_chat(self, cwd: str, message: str) -> None:
        """Open a /btw side chat as a new tab in the same directory."""
        self._add_session(cwd, initial_prompt=message)
        wx.CallAfter(announce, f"Side chat opened in {_short_label(cwd)}")

    def _refresh_picker(self) -> None:
        labels: list[str] = []
        for i in range(self.notebook.GetPageCount()):
            labels.append(f"{i + 1}. {self.notebook.GetPageText(i)}")
        self.session_picker.Set(labels)
        sel = self.notebook.GetSelection()
        if sel != wx.NOT_FOUND:
            self.session_picker.SetSelection(sel)

    def _on_picker_change(self, event: wx.CommandEvent) -> None:
        sel = self.session_picker.GetSelection()
        if sel != wx.NOT_FOUND and sel != self.notebook.GetSelection():
            self.notebook.SetSelection(sel)

    def _on_picker_focus(self, event: wx.FocusEvent) -> None:
        event.Skip()
        wx.CallAfter(announce, "Session picker, pop up button")

    # ----- Options menu -----
    def _toggle_live_rows(self) -> None:
        SETTINGS.live_rows = self._rows_item.IsChecked()
        SETTINGS.save()
        state = "on" if SETTINGS.live_rows else "off"
        self._announce_setting(f"Live activity in the list {state}")

    def _toggle_speak_live(self) -> None:
        SETTINGS.speak_live = self._speak_item.IsChecked()
        SETTINGS.save()
        state = "on" if SETTINGS.speak_live else "off"
        self._announce_setting(f"Speaking activity aloud {state}")

    def _toggle_show_thinking(self) -> None:
        SETTINGS.show_thinking = self._thinking_item.IsChecked()
        SETTINGS.save()
        state = "shown" if SETTINGS.show_thinking else "hidden"
        self._announce_setting(f"The backend's reasoning is {state}")

    def _toggle_text_view(self) -> None:
        SETTINGS.text_view = self._text_view_item.IsChecked()
        SETTINGS.save()
        for i in range(self.notebook.GetPageCount()):
            page = self.notebook.GetPage(i)
            if isinstance(page, SessionPanel):
                page.apply_view_mode()
        if SETTINGS.text_view:
            self._announce_setting("Responses are now a read-only text field, one row per line")
        else:
            self._announce_setting("Responses are now a list")

    def _use_silent_until_response_mode(self) -> None:
        """One action to remain silent until the complete response arrives."""
        SETTINGS.live_rows = False
        SETTINGS.speak_live = False
        SETTINGS.save()
        self._rows_item.Check(False)
        self._speak_item.Check(False)
        self._announce_setting(
            "Silent until the response mode on. Nothing is shown or spoken until the whole response is ready."
        )

    def _announce_setting(self, text: str) -> None:
        announce(text)
        self._set_status_text(text)

    def _cycle_tab(self, direction: int) -> None:
        count = self.notebook.GetPageCount()
        if count <= 1:
            return
        cur = self.notebook.GetSelection()
        nxt = (cur + direction) % count
        self.notebook.SetSelection(nxt)

    def _jump_to_tab(self, idx: int) -> None:
        if 0 <= idx < self.notebook.GetPageCount():
            self.notebook.SetSelection(idx)

    def _new_session(self) -> None:
        """Open a session in a folder that is typed in or browsed to."""
        dlg = NewSessionDialog(self, default_dir=self._projects_folder)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            cwd = dlg.path
        finally:
            dlg.Destroy()
        if not cwd:
            return
        self._add_session(cwd)
        wx.CallAfter(announce, f"New session: {_short_label(cwd)}")

    def _history_cwd(self) -> str:
        """The directory the history picker starts out scoped to."""
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            return page.cwd
        return self._projects_folder or os.getcwd()

    def _open_history(self) -> None:
        """Reopen a past conversation in a new tab (Ctrl+H)."""
        dlg = HistoryDialog(self, backend=self._backend, cwd=self._history_cwd())
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            entry = dlg.entry
        finally:
            dlg.Destroy()
        if entry is None:
            return
        self._resume_history(entry)

    def _resume_history(self, entry: HistoryEntry) -> None:
        """Open one past conversation in its own tab, ready to be continued."""
        with wx.BusyCursor():
            turns = load_turns(entry)
        if not turns:
            announce(f"Error: {entry.title} could not be read back")
            return
        # A tab only continues a conversation while the app-wide backend still
        # matches the one that conversation belongs to — a mismatch starts a
        # new conversation on the next send — so resuming switches to it.
        if normalize_backend(entry.backend) != self._backend:
            self._set_backend(entry.backend)
        cwd = entry.cwd if entry.cwd and os.path.isdir(entry.cwd) else self._history_cwd()
        panel = self._add_session(cwd)
        panel.restore_history(entry, turns)
        # The first message names the tab, because that is what tells this
        # conversation apart from the others open in the same folder.
        # _add_session selects the page it adds, so that is the one to rename.
        self.notebook.SetPageText(
            self.notebook.GetSelection(), _tab_title(entry.title) or _short_label(cwd)
        )
        self._refresh_picker()
        responses = "1 response" if len(turns) == 1 else f"{len(turns)} responses"
        wx.CallAfter(announce, f"Resumed {entry.title}, {responses}")

    def _compact_active(self) -> None:
        """Compact the conversation in the active tab (Ctrl+Shift+K)."""
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page.compact_conversation()

    def _new_conversation_active(self) -> None:
        """Start a fresh conversation in the active tab (Ctrl+Shift+N)."""
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page.clear_conversation()

    def _refresh_compact_item(self) -> None:
        """Grey out Compact for a provider whose CLI has no such command."""
        item = getattr(self, "_compact_item", None)
        if item is None:
            return
        supported = BACKENDS[self._backend].supports_compaction
        item.Enable(supported)
        if supported:
            item.SetHelp("Summarise this conversation so the backend has room to keep going")
        else:
            item.SetHelp(
                f"{backend_label(self._backend)} cannot compact a conversation — "
                "start a new conversation instead"
            )

    def _set_projects_folder(self) -> Optional[str]:
        """Choose and remember the parent folder that holds the projects."""
        with wx.DirDialog(
            self,
            "Choose your Projects folder (the folder that contains your project directories)",
            defaultPath=self._projects_folder or os.path.expanduser("~"),
            style=wx.DD_DEFAULT_STYLE,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return None
            path = dlg.GetPath()
        self._projects_folder = path
        cfg = _load_config()
        cfg["projects_folder"] = path
        _save_config(cfg)
        wx.CallAfter(announce, f"Projects folder set to {_short_label(path)}")
        return path

    def _close_current_session(self) -> None:
        if self.notebook.GetPageCount() <= 1:
            self._set_status_text("Cannot close the last session")
            return
        sel = self.notebook.GetSelection()
        if sel == wx.NOT_FOUND:
            return
        page = self.notebook.GetPage(sel)
        if isinstance(page, SessionPanel):
            page.cancel_worker()
        self.notebook.DeletePage(sel)
        self._refresh_picker()

    def _on_tab_changed(self, event: wx.BookCtrlEvent) -> None:
        event.Skip()
        # Keep the picker selection mirroring the notebook.
        sel = self.notebook.GetSelection()
        if sel != wx.NOT_FOUND and self.session_picker.GetSelection() != sel:
            self.session_picker.SetSelection(sel)
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            self._set_status_text(page.last_status)
            wx.CallAfter(announce, f"Session: {_short_label(page.cwd)}")
            wx.CallAfter(page.focus_prompt)

    # ----- Status routing -----
    def _set_status_text(self, text: str) -> None:
        self.statusbar.SetStatusText(text)

    def _panel_status_changed(self, panel: SessionPanel, text: str) -> None:
        # Only show the status bar message for the currently visible tab.
        if self.notebook.GetCurrentPage() is panel:
            self._set_status_text(text)

    # ----- Focus delegation -----
    def _focus_active(self, which: str) -> None:
        page = self.notebook.GetCurrentPage()
        if not isinstance(page, SessionPanel):
            return
        if which == "prompt":
            page.focus_prompt()

    def _cycle_mode_active(self) -> None:
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page.cycle_mode()

    def _find_active(self) -> None:
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page.open_find()

    def _create_desktop_shortcut(self) -> None:
        try:
            link = create_desktop_shortcut()
        except (OSError, subprocess.SubprocessError) as exc:
            self._announce_setting(f"The desktop shortcut could not be created: {exc}")
            return
        self._announce_setting(f"Desktop shortcut created at {link}")

    def _stop_active(self) -> None:
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page._on_stop()

    def _attach_active(self) -> None:
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page.attach_files()

    def _jump_to_latest_response(self) -> None:
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page.jump_to_latest_response()

    def _slash_active(self) -> None:
        page = self.notebook.GetCurrentPage()
        if isinstance(page, SessionPanel):
            page._pick_slash_command()

    # ----- Cleanup -----
    def _on_close(self, event: wx.CloseEvent) -> None:
        for i in range(self.notebook.GetPageCount()):
            page = self.notebook.GetPage(i)
            if isinstance(page, SessionPanel):
                page.cancel_worker()
        event.Skip()


def _bring_to_front() -> None:
    """Force the window to the foreground on macOS.

    When launched from a plain `python` invocation (rather than a .app bundle),
    macOS may treat the process as a background accessory and never activate its
    window. Claiming the regular activation policy and activating brings it to
    the front. No-op when AppKit isn't available.
    """
    if not _MAC_ANNOUNCE:
        return
    try:
        from AppKit import (  # type: ignore
            NSApplication,
            NSApplicationActivationPolicyRegular,
        )

        nsapp = NSApplication.sharedApplication()
        nsapp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        nsapp.activateIgnoringOtherApps_(True)
    except Exception:
        pass


def main() -> int:
    if "--startup-smoke" in sys.argv:
        # Importing this module has already loaded wxPython, every backend,
        # updater support, and platform accessibility dependencies. Verify the
        # packaged resources without opening a window so CI can test startup.
        required = ("send.wav", "in-progress.wav", "received.wav")
        earcons = Path(_resource_dir()) / "EarCons"
        if not all((earcons / name).is_file() for name in required):
            return 2
        if not APP_NAME or not version_tuple(APP_VERSION):
            return 3
        return 0
    gui_startup_smoke = "--startup-gui-smoke" in sys.argv
    # Before anything is started: nothing BlindPilot launches may inherit a
    # PATH that points back into its own install folder, or the files there
    # stay open long after BlindPilot has closed and cannot be updated.
    keep_bundle_off_child_path()
    # Claim the console before anything can create one on screen. Doing it here,
    # rather than when a terminal is first needed, keeps it out of the way of
    # the first message as well as every later one.
    reserve_hidden_console()
    app = wx.App(False)

    cfg = _load_config()
    # A packaged GUI smoke test runs with a clean temporary profile in CI. It
    # must exercise the real main window without waiting in the interactive
    # first-run wizard.
    if not cfg.get("setup_complete") and not gui_startup_smoke:
        wizard = SetupWizard(
            None,
            cfg.get("projects_folder"),
            normalize_backend(cfg.get("backend")),
        )
        result = wizard.ShowModal()
        if result == wx.ID_OK:
            if wizard.projects_folder:
                cfg["projects_folder"] = wizard.projects_folder
            cfg["backend"] = wizard.backend
        # Finishing or deliberately dismissing the optional Claude setup both
        # count as handled. Users choosing Codex or FreeBuff should not be sent
        # back through the Claude wizard on every launch.
        cfg["setup_complete"] = True
        _save_config(cfg)
        wizard.Destroy()
        # Even if cancelled, open the app — user may know what they're doing.

    frame = MainFrame(initial_cwd=os.getcwd())
    frame.Show()
    frame.Raise()
    _bring_to_front()
    if gui_startup_smoke:
        wx.CallLater(1500, frame.Close)
    else:
        # An update that failed did so with no window to report to, so its
        # reason is read out here, before anything else competes for attention.
        wx.CallLater(1200, frame.report_failed_update)
        wx.CallLater(5000, frame.check_for_updates_silently)
        # Abandoned downloads are tens of megabytes each.
        wx.CallLater(8000, sweep_temporary_files)
    app.MainLoop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
