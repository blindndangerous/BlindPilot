"""Stable BlindPilot application entry point.

Based on the original Claude Code Reader application by doubletaponair
(https://github.com/doubletaponair/claude-code-reader) and distributed under
the MIT License. A compatibility module keeps older imports and integrations
working after the implementation was renamed for BlindPilot.
"""

from blindpilot_app import main


if __name__ == "__main__":
    main()
