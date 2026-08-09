"""Secure GitHub Releases updater for packaged BlindPilot installations.

Copyright (c) 2026 doubletaponair and BlindPilot contributors.
Based on the original Claude Code Reader application by doubletaponair:
https://github.com/doubletaponair/claude-code-reader
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen


GITHUB_REPOSITORY = "serrebidev/BlindPilot"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
MAX_UPDATE_BYTES = 750 * 1024 * 1024
_ALLOWED_DOWNLOAD_HOSTS = ("github.com", "githubusercontent.com")


class UpdateError(RuntimeError):
    """An update could not be checked, verified, or scheduled safely."""


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag: str
    title: str
    notes: str
    page_url: str
    asset_name: str
    asset_url: str
    asset_size: int
    sha256: str
    checksum_url: str = ""

    def is_newer_than(self, current_version: str) -> bool:
        return version_tuple(self.version) > version_tuple(current_version)


def version_tuple(value: str) -> tuple[int, ...]:
    """Return the numeric portion of a release version for safe comparisons."""
    match = re.search(r"\d+(?:\.\d+)+", value or "")
    return tuple(int(part) for part in match.group(0).split(".")) if match else ()


INSTALL_SETUP = "setup"
INSTALL_PORTABLE = "portable"


def install_kind(executable: Optional[Path] = None) -> str:
    """Say whether this copy was put here by the installer or unpacked by hand.

    The two need different updates. A copy from the setup program is registered
    in Add or Remove Programs and keeps its uninstaller beside the executable;
    replacing that directory wholesale deletes the uninstaller and leaves the
    registered version behind, so the installer has to do the update itself.
    An unpacked copy owns nothing but its own folder and is simply swapped.
    """
    if platform.system() != "Windows":
        return INSTALL_PORTABLE
    folder = (executable or Path(sys.executable)).resolve().parent
    try:
        uninstallers = any(folder.glob("unins*.exe"))
    except OSError:
        uninstallers = False
    return INSTALL_SETUP if uninstallers else INSTALL_PORTABLE


def asset_name_for_platform(
    system: Optional[str] = None,
    machine: Optional[str] = None,
    kind: Optional[str] = None,
) -> str:
    """Return the release asset expected by this operating system and CPU."""
    system = (system or platform.system()).casefold()
    machine = (machine or platform.machine()).casefold()
    if system == "windows" and machine in ("amd64", "x86_64"):
        if (kind or install_kind()) == INSTALL_SETUP:
            return "BlindPilot-Setup-x64.exe"
        return "BlindPilot-Windows-x64.zip"
    if system == "darwin":
        if machine in ("arm64", "aarch64"):
            return "BlindPilot-macOS-arm64.zip"
        if machine in ("x86_64", "amd64"):
            return "BlindPilot-macOS-x64.zip"
    raise UpdateError(
        f"Automatic updates are not available for {platform.system()} {platform.machine()}."
    )


def _request(url: str, current_version: str) -> Request:
    return Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"BlindPilot/{current_version}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def _read_limited(response: object, limit: int) -> bytes:
    chunks: list[bytes] = []
    received = 0
    while True:
        chunk = response.read(min(1024 * 1024, limit + 1 - received))
        if not chunk:
            return b"".join(chunks)
        received += len(chunk)
        if received > limit:
            raise UpdateError("The update response exceeded the allowed size.")
        chunks.append(chunk)


def fetch_latest_release(
    current_version: str,
    *,
    opener: Callable[..., object] = urlopen,
    system: Optional[str] = None,
    machine: Optional[str] = None,
    kind: Optional[str] = None,
) -> ReleaseInfo:
    """Query GitHub at runtime and select the asset for this computer."""
    try:
        with opener(_request(LATEST_RELEASE_API, current_version), timeout=20) as response:
            payload = json.loads(_read_limited(response, 4 * 1024 * 1024))
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError(f"Could not contact GitHub: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("draft"):
        raise UpdateError("GitHub returned an invalid latest release.")
    tag = str(payload.get("tag_name") or "").strip()
    if not version_tuple(tag):
        raise UpdateError("The latest GitHub release has no valid version tag.")
    expected_name = asset_name_for_platform(system, machine, kind)
    assets = payload.get("assets")
    if not isinstance(assets, list):
        assets = []
    asset = next(
        (
            entry
            for entry in assets
            if isinstance(entry, dict) and entry.get("name") == expected_name
        ),
        None,
    )
    if asset is None:
        raise UpdateError(f"The release does not contain {expected_name}.")
    size = int(asset.get("size") or 0)
    if size <= 0 or size > MAX_UPDATE_BYTES:
        raise UpdateError("The release asset has an invalid size.")
    digest = str(asset.get("digest") or "")
    sha256 = digest.removeprefix("sha256:").casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        sha256 = ""
    checksum_name = expected_name + ".sha256"
    checksum_asset = next(
        (
            entry
            for entry in assets
            if isinstance(entry, dict) and entry.get("name") == checksum_name
        ),
        None,
    )
    checksum_url = str(checksum_asset.get("browser_download_url") or "") if checksum_asset else ""
    if not sha256 and not checksum_url:
        raise UpdateError("The release has no SHA-256 verification data.")
    return ReleaseInfo(
        version=".".join(map(str, version_tuple(tag))),
        tag=tag,
        title=str(payload.get("name") or tag),
        notes=str(payload.get("body") or "").strip(),
        page_url=str(payload.get("html_url") or ""),
        asset_name=expected_name,
        asset_url=str(asset.get("browser_download_url") or ""),
        asset_size=size,
        sha256=sha256,
        checksum_url=checksum_url,
    )


def _allowed_download_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    return parsed.scheme == "https" and any(
        host == allowed or host.endswith("." + allowed) for allowed in _ALLOWED_DOWNLOAD_HOSTS
    )


def _checksum_from_sidecar(
    release: ReleaseInfo, current_version: str, opener: Callable[..., object]
) -> str:
    if not _allowed_download_url(release.checksum_url):
        raise UpdateError("The checksum download URL is not trusted.")
    try:
        with opener(_request(release.checksum_url, current_version), timeout=20) as response:
            text = _read_limited(response, 4096).decode("ascii", errors="strict")
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError(f"Could not download the update checksum: {exc}") from exc
    match = re.search(r"\b[0-9a-fA-F]{64}\b", text)
    if not match:
        raise UpdateError("The update checksum file is invalid.")
    return match.group(0).casefold()


def download_update(
    release: ReleaseInfo,
    current_version: str,
    *,
    progress: Optional[Callable[[int, int], None]] = None,
    opener: Callable[..., object] = urlopen,
) -> Path:
    """Download a release archive and reject it unless SHA-256 matches."""
    if not _allowed_download_url(release.asset_url):
        raise UpdateError("The update download URL is not trusted.")
    expected = release.sha256 or _checksum_from_sidecar(release, current_version, opener)
    # Keep the published extension: an installer has to stay a .exe to be run.
    suffix = ".exe" if release.asset_name.casefold().endswith(".exe") else ".zip"
    fd, temporary_name = tempfile.mkstemp(prefix="BlindPilot-update-", suffix=suffix)
    os.close(fd)
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    received = 0
    try:
        with opener(_request(release.asset_url, current_version), timeout=60) as response:
            final_url = getattr(response, "geturl", lambda: release.asset_url)()
            if not _allowed_download_url(final_url):
                raise UpdateError("GitHub redirected the update to an untrusted host.")
            with temporary.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > MAX_UPDATE_BYTES or received > release.asset_size:
                        raise UpdateError("The update download exceeded its published size.")
                    digest.update(chunk)
                    handle.write(chunk)
                    if progress:
                        progress(received, release.asset_size)
        if received != release.asset_size:
            raise UpdateError("The update download was incomplete.")
        if digest.hexdigest().casefold() != expected:
            raise UpdateError("The update failed SHA-256 verification.")
        return temporary
    except UpdateError:
        temporary.unlink(missing_ok=True)
        raise
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        raise UpdateError(f"Could not download the update: {exc}") from exc


_WINDOWS_HELPER = r"""param(
    [Parameter(Mandatory=$true)][int]$ParentPid,
    [Parameter(Mandatory=$true)][string]$Archive,
    [Parameter(Mandatory=$true)][string]$InstallDir,
    [Parameter(Mandatory=$true)][string]$Executable
)
$ErrorActionPreference = "Stop"
$stage = Join-Path ([IO.Path]::GetTempPath()) ("BlindPilot-stage-" + [guid]::NewGuid())
$installParent = Split-Path -Parent $InstallDir
$installName = Split-Path -Leaf $InstallDir
$token = [guid]::NewGuid().ToString("N")
$incoming = Join-Path $installParent ($installName + ".update-new-" + $token)
$backup = Join-Path $installParent ($installName + ".update-backup-" + $token)
$oldMoved = $false
$replacementActive = $false
$parent = Get-Process -Id $ParentPid -ErrorAction SilentlyContinue
$parentStarted = if ($null -ne $parent) { $parent.StartTime } else { $null }
try {
    New-Item -ItemType Directory -Path $stage | Out-Null
    Expand-Archive -LiteralPath $Archive -DestinationPath $stage -Force
    $source = Join-Path $stage "BlindPilot"
    if (-not (Test-Path -LiteralPath (Join-Path $source $Executable) -PathType Leaf)) {
        throw "The update archive does not contain $Executable"
    }

    # Prepare a complete sibling installation before touching the live one.
    # Keeping it on the install volume makes both directory moves atomic.
    Copy-Item -LiteralPath $source -Destination $incoming -Recurse -Force

    # Give BlindPilot time to run its normal close handler. If a worker or
    # window prevents shutdown, stop this exact PID before replacing files.
    $parent = Get-Process -Id $ParentPid -ErrorAction SilentlyContinue
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ($null -ne $parent) {
        if ($null -ne $parentStarted -and $parent.StartTime -ne $parentStarted) {
            break
        }
        if ([DateTime]::UtcNow -ge $deadline) {
            Stop-Process -Id $ParentPid -Force -ErrorAction SilentlyContinue
            Wait-Process -Id $ParentPid -ErrorAction SilentlyContinue
            break
        }
        Start-Sleep -Milliseconds 200
        $parent = Get-Process -Id $ParentPid -ErrorAction SilentlyContinue
    }

    if (Test-Path -LiteralPath $InstallDir) {
        Move-Item -LiteralPath $InstallDir -Destination $backup
        $oldMoved = $true
    }
    Move-Item -LiteralPath $incoming -Destination $InstallDir
    $replacementActive = $true

    $newProcess = Start-Process `
        -FilePath (Join-Path $InstallDir $Executable) `
        -WorkingDirectory $InstallDir `
        -PassThru
    Start-Sleep -Seconds 2
    $newProcess.Refresh()
    if ($newProcess.HasExited) {
        throw "The new BlindPilot version exited during startup"
    }

    # A successful process creation completes the handoff. Backup cleanup is
    # deliberately best-effort so it cannot roll back a running new version.
    Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue
    $oldMoved = $false
} catch {
    $failure = $_
    if ($replacementActive -and (Test-Path -LiteralPath $InstallDir)) {
        Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($oldMoved -and (Test-Path -LiteralPath $backup)) {
        Move-Item -LiteralPath $backup -Destination $InstallDir -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath (Join-Path $InstallDir $Executable) -PathType Leaf) {
            Start-Process `
                -FilePath (Join-Path $InstallDir $Executable) `
                -WorkingDirectory $InstallDir `
                -ErrorAction SilentlyContinue
        }
    } elseif (-not (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)) {
        if (Test-Path -LiteralPath (Join-Path $InstallDir $Executable) -PathType Leaf) {
            Start-Process `
                -FilePath (Join-Path $InstallDir $Executable) `
                -WorkingDirectory $InstallDir `
                -ErrorAction SilentlyContinue
        }
    }
    throw $failure
} finally {
    Remove-Item -LiteralPath $incoming -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $Archive -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
}
"""


_WINDOWS_SETUP_HELPER = r"""param(
    [Parameter(Mandatory=$true)][int]$ParentPid,
    [Parameter(Mandatory=$true)][string]$Installer,
    [Parameter(Mandatory=$true)][string]$Executable
)
$ErrorActionPreference = "Stop"
try {
    # Give BlindPilot time to close on its own before the installer replaces
    # its files. Same bounded forced-close fallback as the portable updater.
    $parent = Get-Process -Id $ParentPid -ErrorAction SilentlyContinue
    $parentStarted = if ($null -ne $parent) { $parent.StartTime } else { $null }
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ($null -ne $parent) {
        if ($null -ne $parentStarted -and $parent.StartTime -ne $parentStarted) {
            break
        }
        if ([DateTime]::UtcNow -ge $deadline) {
            Stop-Process -Id $ParentPid -Force -ErrorAction SilentlyContinue
            Wait-Process -Id $ParentPid -ErrorAction SilentlyContinue
            break
        }
        Start-Sleep -Milliseconds 200
        $parent = Get-Process -Id $ParentPid -ErrorAction SilentlyContinue
    }

    # The setup program owns this installation: it keeps the uninstaller and
    # the Add or Remove Programs entry correct, and it remembers the shortcuts
    # chosen last time. Silent, because there is no one at the keyboard.
    $run = Start-Process -FilePath $Installer -ArgumentList @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOCANCEL"
    ) -Wait -PassThru
    if ($run.ExitCode -ne 0) {
        throw "The BlindPilot installer exited with code $($run.ExitCode)"
    }
    Start-Process -FilePath $Executable -WorkingDirectory (Split-Path -Parent $Executable)
} finally {
    Remove-Item -LiteralPath $Installer -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
}
"""


_MACOS_HELPER = r"""#!/bin/sh
set -eu
parent_pid="$1"
archive="$2"
app_path="$3"
waited=0
while kill -0 "$parent_pid" 2>/dev/null; do
    if [ "$waited" -ge 30 ]; then
        kill -TERM "$parent_pid" 2>/dev/null || true
        sleep 2
        kill -KILL "$parent_pid" 2>/dev/null || true
        forced_wait=0
        while kill -0 "$parent_pid" 2>/dev/null && [ "$forced_wait" -lt 10 ]; do
            sleep 1
            forced_wait=$((forced_wait + 1))
        done
        if kill -0 "$parent_pid" 2>/dev/null; then
            exit 1
        fi
        break
    fi
    sleep 1
    waited=$((waited + 1))
done
stage="$(mktemp -d "${TMPDIR:-/tmp}/BlindPilot-stage.XXXXXX")"
backup="${app_path}.update-backup"
cleanup() { rm -rf "$stage" "$archive" "$0"; }
trap cleanup EXIT
ditto -x -k "$archive" "$stage"
new_app="$stage/BlindPilot.app"
test -x "$new_app/Contents/MacOS/BlindPilot"
rm -rf "$backup"
mv "$app_path" "$backup"
if ditto "$new_app" "$app_path"; then
    rm -rf "$backup"
    open "$app_path"
else
    rm -rf "$app_path"
    mv "$backup" "$app_path"
    exit 1
fi
"""


def schedule_install(archive: Path) -> None:
    """Install the verified archive after this packaged process exits."""
    if not getattr(sys, "frozen", False):
        raise UpdateError("Automatic installation is available in packaged builds only.")
    executable = Path(sys.executable).resolve()
    if platform.system() == "Windows":
        install_dir = executable.parent
        setup = install_kind(executable) == INSTALL_SETUP
        if setup:
            script = _WINDOWS_SETUP_HELPER
            arguments = ["-Installer", str(archive), "-Executable", str(executable)]
        else:
            script = _WINDOWS_HELPER
            arguments = [
                "-Archive",
                str(archive),
                "-InstallDir",
                str(install_dir),
                "-Executable",
                executable.name,
            ]
        helper: Optional[Path] = None
        try:
            fd, helper_name = tempfile.mkstemp(prefix="BlindPilot-update-", suffix=".ps1")
            os.close(fd)
            helper = Path(helper_name)
            helper.write_text(script, encoding="utf-8-sig")
            powershell = os.environ.get("SystemRoot", r"C:\Windows")
            powershell = str(
                Path(powershell) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            )
            subprocess.Popen(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-WindowStyle",
                    "Hidden",
                    "-File",
                    str(helper),
                    "-ParentPid",
                    str(os.getpid()),
                    *arguments,
                ],
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    | getattr(subprocess, "DETACHED_PROCESS", 0)
                ),
                # The helper outlives BlindPilot and has no console of its own,
                # so it must never be left holding an inherited console handle.
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                cwd=tempfile.gettempdir(),
            )
        except (OSError, ValueError) as exc:
            if helper is not None:
                helper.unlink(missing_ok=True)
            raise UpdateError(f"Could not start the update installer: {exc}") from exc
        return
    if platform.system() == "Darwin":
        app_path = next(
            (parent for parent in executable.parents if parent.suffix == ".app"),
            None,
        )
        if app_path is None:
            raise UpdateError("BlindPilot is not running from an application bundle.")
        helper = None
        try:
            fd, helper_name = tempfile.mkstemp(prefix="BlindPilot-update-", suffix=".sh")
            os.close(fd)
            helper = Path(helper_name)
            helper.write_text(_MACOS_HELPER, encoding="utf-8")
            helper.chmod(0o700)
            subprocess.Popen(
                ["/bin/sh", str(helper), str(os.getpid()), str(archive), str(app_path)],
                start_new_session=True,
                close_fds=True,
                cwd=tempfile.gettempdir(),
            )
        except (OSError, ValueError) as exc:
            if helper is not None:
                helper.unlink(missing_ok=True)
            raise UpdateError(f"Could not start the update installer: {exc}") from exc
        return
    raise UpdateError("Automatic installation is not supported on this platform.")
