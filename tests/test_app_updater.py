"""GitHub updater selection, version, and verification regression tests."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import pytest

from app_updater import (
    ReleaseInfo,
    UpdateError,
    _WINDOWS_HELPER,
    asset_name_for_platform,
    download_update,
    fetch_latest_release,
    schedule_install,
    version_tuple,
)


class _Response(io.BytesIO):
    def __init__(self, payload: bytes, url: str = "https://github.com/release"):
        super().__init__(payload)
        self._url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def geturl(self) -> str:
        return self._url


def test_version_and_platform_asset_selection():
    assert version_tuple("v1.2.10-beta") == (1, 2, 10)
    assert asset_name_for_platform("Windows", "AMD64") == "BlindPilot-Windows-x64.zip"
    assert asset_name_for_platform("Darwin", "arm64") == "BlindPilot-macOS-arm64.zip"
    assert asset_name_for_platform("Darwin", "x86_64") == "BlindPilot-macOS-x64.zip"


def test_latest_release_is_discovered_from_github_at_runtime():
    archive = b"verified archive"
    digest = hashlib.sha256(archive).hexdigest()
    payload = {
        "tag_name": "v0.4.0",
        "name": "BlindPilot 0.4.0",
        "body": "Accessible update",
        "html_url": "https://github.com/serrebidev/BlindPilot/releases/tag/v0.4.0",
        "draft": False,
        "assets": [
            {
                "name": "BlindPilot-Windows-x64.zip",
                "size": len(archive),
                "digest": f"sha256:{digest}",
                "browser_download_url": "https://github.com/download/update.zip",
            }
        ],
    }

    release = fetch_latest_release(
        "0.3.0",
        opener=lambda *_args, **_kwargs: _Response(json.dumps(payload).encode()),
        system="Windows",
        machine="AMD64",
    )

    assert release.version == "0.4.0"
    assert release.is_newer_than("0.3.0")
    assert release.asset_size == len(archive)
    assert release.sha256 == digest


def test_download_rejects_a_hash_mismatch():
    archive = b"tampered"
    release = ReleaseInfo(
        version="0.4.0",
        tag="v0.4.0",
        title="Update",
        notes="",
        page_url="https://github.com/release",
        asset_name="BlindPilot-Windows-x64.zip",
        asset_url="https://github.com/download/update.zip",
        asset_size=len(archive),
        sha256="0" * 64,
    )

    with pytest.raises(UpdateError, match="SHA-256"):
        download_update(
            release,
            "0.3.0",
            opener=lambda *_args, **_kwargs: _Response(archive),
        )


def test_download_accepts_the_published_hash():
    archive = b"verified"
    release = ReleaseInfo(
        version="0.4.0",
        tag="v0.4.0",
        title="Update",
        notes="",
        page_url="https://github.com/release",
        asset_name="BlindPilot-Windows-x64.zip",
        asset_url="https://github.com/download/update.zip",
        asset_size=len(archive),
        sha256=hashlib.sha256(archive).hexdigest(),
    )

    downloaded = download_update(
        release,
        "0.3.0",
        opener=lambda *_args, **_kwargs: _Response(archive),
    )
    try:
        assert downloaded.read_bytes() == archive
    finally:
        downloaded.unlink(missing_ok=True)


def test_schedule_install_rejects_source_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    with pytest.raises(UpdateError, match="packaged builds only"):
        schedule_install(tmp_path / "update.zip")


def test_windows_installer_waits_swaps_and_relaunches(monkeypatch, tmp_path):
    install_dir = tmp_path / "installed BlindPilot"
    executable = install_dir / "BlindPilot.exe"
    archive = tmp_path / "BlindPilot update.zip"
    install_dir.mkdir()
    archive.write_bytes(b"archive")
    launched = {}

    def popen(argv, **kwargs):
        launched["argv"] = argv
        launched["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr("app_updater.platform.system", lambda: "Windows")
    monkeypatch.setattr("app_updater.subprocess.Popen", popen)

    schedule_install(archive)

    argv = launched["argv"]
    helper = Path(argv[argv.index("-File") + 1])
    try:
        script = helper.read_text(encoding="utf-8-sig")
        assert argv[argv.index("-ParentPid") + 1] == str(os.getpid())
        assert argv[argv.index("-Archive") + 1] == str(archive)
        assert argv[argv.index("-InstallDir") + 1] == str(install_dir.resolve())
        assert argv[argv.index("-Executable") + 1] == "BlindPilot.exe"
        assert launched["kwargs"]["cwd"] == tempfile.gettempdir()
        assert launched["kwargs"]["close_fds"] is True
        assert "Stop-Process -Id $ParentPid -Force" in script
        assert script.index("Get-Process -Id $ParentPid") < script.index(
            "Move-Item -LiteralPath $InstallDir"
        )
        assert script.index("Move-Item -LiteralPath $incoming") < script.index("Start-Process")
        assert "-WorkingDirectory $InstallDir" in script
        assert "Move-Item -LiteralPath $backup -Destination $InstallDir" in script
    finally:
        helper.unlink(missing_ok=True)


def test_windows_installer_spawn_error_is_accessible_and_cleans_helper(monkeypatch, tmp_path):
    executable = tmp_path / "BlindPilot" / "BlindPilot.exe"
    archive = tmp_path / "update.zip"
    created_helper = None
    real_mkstemp = tempfile.mkstemp

    def mkstemp(*args, **kwargs):
        nonlocal created_helper
        fd, name = real_mkstemp(*args, dir=tmp_path, **kwargs)
        created_helper = Path(name)
        return fd, name

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr("app_updater.platform.system", lambda: "Windows")
    monkeypatch.setattr("app_updater.tempfile.mkstemp", mkstemp)
    monkeypatch.setattr(
        "app_updater.subprocess.Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("launch failed")),
    )

    with pytest.raises(UpdateError, match="Could not start.*launch failed"):
        schedule_install(archive)

    assert created_helper is not None
    assert not created_helper.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows PowerShell")
def test_windows_helper_script_has_valid_powershell_syntax(tmp_path):
    helper = tmp_path / "updater.ps1"
    helper.write_text(_WINDOWS_HELPER, encoding="utf-8-sig")
    powershell = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    command = (
        "$errors=$null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        "'" + str(helper).replace("'", "''") + "', [ref]$null, [ref]$errors) | Out-Null; "
        "if ($errors.Count) { $errors | ForEach-Object { $_.Message }; exit 1 }"
    )

    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        # A child with no console of its own must not be handed the parent's
        # console as stdin: PowerShell blocks on reading it and never exits.
        stdin=subprocess.DEVNULL,
        timeout=60,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows PowerShell")
def test_windows_helper_waits_for_old_process_then_replaces_and_launches(tmp_path):
    install_dir = tmp_path / "installed BlindPilot"
    payload = tmp_path / "payload" / "BlindPilot"
    archive = tmp_path / "BlindPilot update.zip"
    helper = tmp_path / "updater.ps1"
    marker = install_dir / "new-launched.txt"
    running_marker = install_dir / "new-running.txt"
    install_dir.mkdir()
    payload.mkdir(parents=True)
    # The stand-in for the app is a script host rather than a batch file: it
    # keeps running long enough for the helper's launch check, and it does it
    # without putting a console window on screen during the test run.
    (install_dir / "BlindPilot.vbs").write_text("WScript.Quit 0\r\n", encoding="ascii")
    (install_dir / "obsolete.txt").write_text("old", encoding="ascii")
    (payload / "BlindPilot.vbs").write_text(
        'Set fso = CreateObject("Scripting.FileSystemObject")\r\n'
        "home = fso.GetParentFolderName(WScript.ScriptFullName)\r\n"
        'fso.CreateTextFile(fso.BuildPath(home, "new-launched.txt"), True).WriteLine "launched"\r\n'
        'fso.CreateTextFile(fso.BuildPath(home, "new-running.txt"), True).WriteLine "running"\r\n'
        "WScript.Sleep 5000\r\n"
        # Step out of the install directory before finishing, or the running
        # process keeps a handle on it and the temporary tree cannot be removed.
        'CreateObject("WScript.Shell").CurrentDirectory = fso.GetSpecialFolder(2)\r\n'
        'fso.DeleteFile fso.BuildPath(home, "new-running.txt")\r\n',
        encoding="ascii",
    )
    (payload / "current.txt").write_text("new", encoding="ascii")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in payload.rglob("*"):
            if path.is_file():
                bundle.write(path, Path("BlindPilot") / path.relative_to(payload))
    helper.write_text(_WINDOWS_HELPER, encoding="utf-8-sig")
    powershell = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    blocker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=install_dir,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    updater = None
    try:
        updater = subprocess.Popen(
            [
                str(powershell),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(helper),
                "-ParentPid",
                str(blocker.pid),
                "-Archive",
                str(archive),
                "-InstallDir",
                str(install_dir),
                "-Executable",
                "BlindPilot.vbs",
            ],
            cwd=tempfile.gettempdir(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if list(tmp_path.glob("installed BlindPilot.update-new-*")):
                break
            if updater.poll() is not None:
                break
            time.sleep(0.05)

        assert blocker.poll() is None
        assert (install_dir / "obsolete.txt").read_text(encoding="ascii") == "old"
        assert not marker.exists()

        blocker.terminate()
        blocker.wait(timeout=10)
        stdout, stderr = updater.communicate(timeout=20)
        assert updater.returncode == 0, stdout + stderr

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.05)
        assert marker.exists()
        assert (install_dir / "current.txt").read_text(encoding="ascii") == "new"
        assert not (install_dir / "obsolete.txt").exists()
        assert not archive.exists()
        assert not helper.exists()
        assert not list(tmp_path.glob("installed BlindPilot.update-*"))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and running_marker.exists():
            time.sleep(0.05)
        assert not running_marker.exists()
    finally:
        if blocker.poll() is None:
            blocker.kill()
            blocker.wait(timeout=10)
        if updater is not None and updater.poll() is None:
            updater.kill()
            updater.wait(timeout=10)
