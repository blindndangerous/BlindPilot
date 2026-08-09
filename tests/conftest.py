"""Stable test fixtures for Windows and screen-reader development machines."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def tmp_path() -> Path:
    """Use ordinary workspace ACLs instead of pytest's restrictive Windows ACL."""
    root = Path.cwd() / ".test-tmp"
    root.mkdir(exist_ok=True)
    temporary = root / uuid.uuid4().hex
    temporary.mkdir()
    try:
        yield temporary
    finally:
        shutil.rmtree(temporary)
        try:
            root.rmdir()
        except OSError:
            # Parallel tests may still own another child directory.
            pass
