"""Compatibility import for the former Claude Code Reader module name.

BlindPilot is based on the original Claude Code Reader application by
doubletaponair: https://github.com/doubletaponair/claude-code-reader

New code should import :mod:`blindpilot_app`. This alias remains so upgrades do
not break scripts written for the original application.
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import sys

import blindpilot_app as _implementation


if __name__ == "__main__":
    raise SystemExit(_implementation.main())

# Return the implementation module itself so attribute patching and private
# compatibility imports behave exactly as they did before the rename.
sys.modules[__name__] = _implementation
