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
import time
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


# Everything the two Windows helpers share. An update runs with no application
# left to report to, so the rules here are: write down what happened, be sure
# nothing is still holding the files before touching them, and never leave the
# user without a working BlindPilot.
_WINDOWS_PRELUDE = r"""
$ErrorActionPreference = "Stop"

function Write-Log([string]$Message) {
    $stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    try { Add-Content -LiteralPath $LogFile -Value "$stamp  $Message" } catch { }
}

function Save-Failure([string]$Message) {
    # BlindPilot is not running to be told, so the reason is left where its next
    # start will find it. Without this a failed update is silent forever, which
    # is exactly how this went unnoticed before.
    try { Set-Content -LiteralPath $StatusFile -Value @($Message, $LogFile) } catch { }
}

function Get-Blockers([int]$TargetPid, [string]$Folder) {
    # Waiting for the one process id is not enough. Anything still running out
    # of the folder being replaced keeps its files mapped, and BlindPilot leaves
    # descendants behind: the agent command-line tools it starts, and the
    # console host bundled in _internal for FreeBuff's terminal.
    $root = ([IO.Path]::GetFullPath($Folder)).TrimEnd('\') + '\'
    $found = @()
    if ($TargetPid -gt 0) {
        $one = Get-Process -Id $TargetPid -ErrorAction SilentlyContinue
        if ($one) { $found += $one }
    }
    foreach ($item in (Get-Process -ErrorAction SilentlyContinue)) {
        try {
            $path = $item.Path
            if ($path) {
                $full = [IO.Path]::GetFullPath($path)
                if ($full.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { $found += $item }
            }
        } catch { }
    }
    return @($found | Sort-Object Id -Unique)
}

function Wait-ForExit([int]$TargetPid, [string]$Folder) {
    $stages = @(30, 10, 10)
    for ($stage = 0; $stage -lt $stages.Count; $stage++) {
        $deadline = [DateTime]::UtcNow.AddSeconds($stages[$stage])
        while ([DateTime]::UtcNow -lt $deadline) {
            if ((Get-Blockers $TargetPid $Folder).Count -eq 0) {
                # Give Windows a moment to release the image mappings of a
                # process that has only just gone.
                Start-Sleep -Milliseconds 1500
                return $true
            }
            Start-Sleep -Milliseconds 250
        }
        $blockers = Get-Blockers $TargetPid $Folder
        if ($blockers.Count -eq 0) { Start-Sleep -Milliseconds 1500; return $true }
        Write-Log ("Still running: " + (($blockers | ForEach-Object { $_.Id }) -join ", "))
        foreach ($item in $blockers) {
            try {
                if ($stage -eq 0) { $item.CloseMainWindow() | Out-Null }
                else { Stop-Process -Id $item.Id -Force -ErrorAction SilentlyContinue }
            } catch { }
        }
    }
    return ((Get-Blockers $TargetPid $Folder).Count -eq 0)
}

function Wait-Unlocked([string]$Folder) {
    # A process that has just exited can leave its libraries mapped a moment
    # longer, and a virus scanner reading the new files holds them too. Opening
    # each one with no sharing allowed is the only way to know the swap can go
    # ahead rather than fail halfway.
    $targets = @()
    $exe = Join-Path $Folder $Executable
    if (Test-Path -LiteralPath $exe -PathType Leaf) { $targets += $exe }
    $internal = Join-Path $Folder "_internal"
    if (Test-Path -LiteralPath $internal -PathType Container) {
        $targets += @(
            Get-ChildItem -LiteralPath $internal -File -Filter *.dll -ErrorAction SilentlyContinue |
                ForEach-Object { $_.FullName }
        )
        $targets += @(
            Get-ChildItem -LiteralPath $internal -File -Filter *.exe -Recurse -ErrorAction SilentlyContinue |
                ForEach-Object { $_.FullName }
        )
    }
    $locked = @()
    foreach ($path in $targets) {
        $opened = $false
        for ($attempt = 0; $attempt -lt 10 -and -not $opened; $attempt++) {
            try {
                $handle = [IO.File]::Open(
                    $path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None
                )
                $handle.Close()
                $opened = $true
            } catch { Start-Sleep -Milliseconds 500 }
        }
        if (-not $opened) { $locked += $path }
    }
    if ($locked.Count -gt 0) {
        Write-Log ("Locked after waiting: " + ($locked -join "; "))
        return $false
    }
    return $true
}

function Invoke-Robocopy([string]$From, [string]$To, [bool]$Move) {
    $arguments = @($From, $To, "/E", "/R:10", "/W:3", "/NFL", "/NDL", "/NP", "/NJH", "/NJS")
    if ($Move) { $arguments += "/MOVE" }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & robocopy.exe @arguments 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    foreach ($line in $output) {
        $text = "$line".Trim()
        if ($text) { Write-Log ("robocopy: " + $text) }
    }
    # Robocopy reports through a bit mask: anything under 8 copied cleanly.
    return $code
}

function Test-Drained([string]$Folder) {
    if (-not (Test-Path -LiteralPath $Folder -PathType Container)) { return $true }
    $left = @(Get-ChildItem -LiteralPath $Folder -Recurse -Force -File -ErrorAction SilentlyContinue)
    if ($left.Count -eq 0) { return $true }
    Write-Log ("Folder still holds " + $left.Count + " file(s), first: " + $left[0].FullName)
    return $false
}

function Start-BlindPilot([string]$Folder) {
    $exe = Join-Path $Folder $Executable
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
        Write-Log ("Cannot restart: " + $exe + " is missing.")
        return $false
    }
    # Never start it with the install folder as its working directory. That
    # directory handle is what stops the *next* update from replacing it.
    try {
        Start-Process -FilePath $exe -WorkingDirectory $HOME -ErrorAction Stop | Out-Null
        return $true
    } catch {
        Write-Log ("Could not restart BlindPilot: " + $_.Exception.Message)
        return $false
    }
}
"""


_WINDOWS_HELPER = (
    r"""param(
    [Parameter(Mandatory=$true)][int]$ParentPid,
    [Parameter(Mandatory=$true)][string]$Archive,
    [Parameter(Mandatory=$true)][string]$InstallDir,
    [Parameter(Mandatory=$true)][string]$Executable,
    [Parameter(Mandatory=$true)][string]$LogFile,
    [Parameter(Mandatory=$true)][string]$StatusFile
)
"""
    + _WINDOWS_PRELUDE
    + r"""
$token = [guid]::NewGuid().ToString("N")
$stage = Join-Path ([IO.Path]::GetTempPath()) ("BlindPilot-stage-" + $token)
$backup = (Join-Path (Split-Path -Parent $InstallDir) (Split-Path -Leaf $InstallDir)) +
    ".update-backup-" + $token
$movedAside = $false
$replaced = $false
$handedOver = $false
try {
    Write-Log ("Updating " + $InstallDir)
    New-Item -ItemType Directory -Path $stage | Out-Null
    Expand-Archive -LiteralPath $Archive -DestinationPath $stage -Force
    $source = Join-Path $stage "BlindPilot"
    if (-not (Test-Path -LiteralPath (Join-Path $source $Executable) -PathType Leaf)) {
        throw "The update archive does not contain $Executable."
    }

    if (-not (Wait-ForExit $ParentPid $InstallDir)) {
        throw "BlindPilot is still running, so its files could not be replaced."
    }
    if (-not (Wait-Unlocked $InstallDir)) {
        throw "Some BlindPilot files are still in use, so they could not be replaced."
    }

    # Move what is *inside* the folder rather than renaming the folder itself.
    # The folder can be held open by a shortcut's working directory or a file
    # sync client, and a rename fails where moving its contents succeeds.
    New-Item -ItemType Directory -Path $backup -Force | Out-Null
    # Set before the first move, not after a successful one: the moment
    # robocopy starts, files may already have left the install folder, and a
    # failure from here on has to put them back.
    $movedAside = $true
    $drained = $false
    for ($attempt = 1; $attempt -le 5 -and -not $drained; $attempt++) {
        $code = Invoke-Robocopy $InstallDir $backup $true
        if ($code -ge 8) { throw "Could not move the current version aside (robocopy $code)." }
        $drained = Test-Drained $InstallDir
        if (-not $drained) {
            # Robocopy retries a copy that fails, but not the delete that
            # follows it, so one briefly locked file leaves the folder
            # half-emptied. Waiting and repeating the whole move clears it.
            Write-Log ("Move left files behind; retrying (attempt " + $attempt + ").")
            Start-Sleep -Seconds 2
        }
    }
    if (-not $drained) { throw "The current version could not be moved aside." }

    $code = Invoke-Robocopy $source $InstallDir $true
    if ($code -ge 8) { throw "Could not put the new version in place (robocopy $code)." }
    $replaced = $true
    if (-not (Test-Path -LiteralPath (Join-Path $InstallDir $Executable) -PathType Leaf)) {
        throw "The installed folder is missing $Executable after the update."
    }

    Write-Log "Update applied. Restarting BlindPilot."
    Start-BlindPilot $InstallDir | Out-Null
    $handedOver = $true
    Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue
    Write-Log "Done."
} catch {
    $message = $_.Exception.Message
    Write-Log ("Update failed: " + $message)
    if (-not $handedOver -and $movedAside -and (Test-Path -LiteralPath $backup)) {
        Write-Log "Putting the previous version back."
        if ($replaced) {
            Get-ChildItem -LiteralPath $InstallDir -Force -ErrorAction SilentlyContinue |
                Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        }
        # Copy the backup back rather than moving it. Whatever caused the
        # failure must not be able to consume the only remaining copy of the
        # version that was working a minute ago.
        $code = Invoke-Robocopy $backup $InstallDir $false
        if ($code -ge 8) {
            Write-Log ("Restore reported robocopy " + $code + "; the backup is kept at " + $backup)
        } else {
            Write-Log ("Previous version restored; its backup is kept at " + $backup)
        }
    }
    if (-not $handedOver) { Start-BlindPilot $InstallDir | Out-Null }
    Save-Failure $message
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $Archive -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
    exit 1
}
Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $Archive -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $LogFile -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
"""
)


_WINDOWS_SETUP_HELPER = (
    r"""param(
    [Parameter(Mandatory=$true)][int]$ParentPid,
    [Parameter(Mandatory=$true)][string]$Installer,
    [Parameter(Mandatory=$true)][string]$InstallDir,
    [Parameter(Mandatory=$true)][string]$Executable,
    [Parameter(Mandatory=$true)][string]$LogFile,
    [Parameter(Mandatory=$true)][string]$StatusFile
)
"""
    + _WINDOWS_PRELUDE
    + r"""
try {
    Write-Log ("Updating the installed copy in " + $InstallDir)
    if (-not (Wait-ForExit $ParentPid $InstallDir)) {
        throw "BlindPilot is still running, so the installer could not replace its files."
    }

    # The setup program owns this installation: it keeps the uninstaller and
    # the Add or Remove Programs entry correct, and it remembers the shortcuts
    # chosen last time. Silent, because there is no one at the keyboard, and
    # pointed at the existing folder so an update never moves the application.
    $run = Start-Process -FilePath $Installer -ArgumentList @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/NOCANCEL",
        "/CLOSEAPPLICATIONS",
        ("/DIR=" + $InstallDir)
    ) -Wait -PassThru
    if ($run.ExitCode -ne 0) {
        throw "The BlindPilot installer exited with code $($run.ExitCode)."
    }
    Write-Log "Installer finished. Restarting BlindPilot."
    # The installer's own run entry is skipped when it runs silently, so
    # restarting is this script's job.
    if (-not (Start-BlindPilot $InstallDir)) {
        throw "BlindPilot was updated but could not be restarted."
    }
    Write-Log "Done."
} catch {
    $message = $_.Exception.Message
    Write-Log ("Update failed: " + $message)
    # The installer either succeeded or left the old copy alone, so there is
    # nothing to undo — but the user must not be left with no application.
    Start-BlindPilot $InstallDir | Out-Null
    Save-Failure $message
    Remove-Item -LiteralPath $Installer -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
    exit 1
}
Remove-Item -LiteralPath $Installer -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $LogFile -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
"""
)


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


TEMPORARY_PREFIX = "BlindPilot-update-"
STATUS_FILE_NAME = "BlindPilot-update-status.txt"


def _status_path() -> Path:
    return Path(tempfile.gettempdir()) / STATUS_FILE_NAME


def pending_failure() -> tuple[str, str]:
    """(reason, log path) from an update that failed after BlindPilot closed.

    An update runs with no application to report to, so the helper writes the
    reason down and this is read on the next start. Returns empty strings when
    the last update did not fail.
    """
    try:
        lines = _status_path().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "", ""
    reason = lines[0].strip() if lines else ""
    log = lines[1].strip() if len(lines) > 1 else ""
    return reason, log


def clear_pending_failure() -> None:
    _status_path().unlink(missing_ok=True)


def sweep_temporary_files(minimum_age_seconds: float = 6 * 60 * 60) -> int:
    """Delete abandoned update downloads and helpers, returning how many went.

    A failed update leaves its archive behind, and those are tens of megabytes
    each. Only files old enough to belong to a finished attempt are touched, so
    this can never delete the download of an update that is running right now.
    """
    removed = 0
    now = time.time()
    try:
        candidates = list(Path(tempfile.gettempdir()).glob(f"{TEMPORARY_PREFIX}*"))
    except OSError:
        return 0
    for path in candidates:
        if path.name == STATUS_FILE_NAME or path.suffix.casefold() == ".log":
            continue
        try:
            if not path.is_file() or now - path.stat().st_mtime < minimum_age_seconds:
                continue
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _windows_helper_flags() -> int:
    """Creation flags for a helper that must outlive the process starting it.

    DETACHED_PROCESS must never be used here. It leaves PowerShell with no
    console at all, and Windows PowerShell then exits reporting success without
    ever running the script — which is why no BlindPilot update since 0.3.0 has
    installed. CREATE_NO_WINDOW already gives the helper a console of its own
    that is never shown, which is what was actually wanted.
    """
    return (
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    )


def _spawn_detached(command: list[str], flags: int) -> None:
    subprocess.Popen(
        command,
        creationflags=flags,
        # The helper outlives BlindPilot, so it is left holding nothing of it.
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        cwd=tempfile.gettempdir(),
    )


def _start_windows_helper(command: list[str]) -> None:
    flags = _windows_helper_flags()
    try:
        _spawn_detached(command, flags)
    except OSError:
        # Breaking out of a job object is refused when the job forbids it, and
        # BlindPilot can be started from inside one. Staying in the job is
        # better than not updating at all.
        breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        if not breakaway:
            raise
        _spawn_detached(command, flags & ~breakaway)


def schedule_install(archive: Path) -> None:
    """Install the verified archive after this packaged process exits."""
    if not getattr(sys, "frozen", False):
        raise UpdateError("Automatic installation is available in packaged builds only.")
    executable = Path(sys.executable).resolve()
    if platform.system() == "Windows":
        install_dir = executable.parent
        log = Path(tempfile.gettempdir()) / f"{TEMPORARY_PREFIX}{os.getpid()}.log"
        shared = [
            "-InstallDir",
            str(install_dir),
            "-Executable",
            executable.name,
            "-LogFile",
            str(log),
            "-StatusFile",
            str(_status_path()),
        ]
        if install_kind(executable) == INSTALL_SETUP:
            script = _WINDOWS_SETUP_HELPER
            arguments = ["-Installer", str(archive), *shared]
        else:
            script = _WINDOWS_HELPER
            arguments = ["-Archive", str(archive), *shared]
        # A stale reason from a previous attempt must not be reported as this
        # one's outcome.
        clear_pending_failure()
        helper: Optional[Path] = None
        try:
            fd, helper_name = tempfile.mkstemp(prefix=TEMPORARY_PREFIX, suffix=".ps1")
            os.close(fd)
            helper = Path(helper_name)
            helper.write_text(script, encoding="utf-8-sig")
            powershell = os.environ.get("SystemRoot", r"C:\Windows")
            powershell = str(
                Path(powershell) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            )
            _start_windows_helper(
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
                ]
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
