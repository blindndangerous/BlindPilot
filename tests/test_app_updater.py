"""GitHub updater selection, version, and verification regression tests."""

from __future__ import annotations

import hashlib
import io
import json

import pytest

from app_updater import (
    ReleaseInfo,
    UpdateError,
    asset_name_for_platform,
    download_update,
    fetch_latest_release,
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
