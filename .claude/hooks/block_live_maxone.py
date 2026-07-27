#!/usr/bin/env python3
"""Block live MaxOne execution from Claude Code."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

BLOCK_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"MAXONE_HARDWARE_ENABLED\s*=\s*1", re.IGNORECASE),
        "Setting the live-hardware environment gate is blocked inside Claude Code.",
    ),
    (
        re.compile(r"--mode(?:=|\s+)armed\b", re.IGNORECASE),
        "Running MAXONE in armed mode is blocked inside Claude Code.",
    ),
    (
        re.compile(r"\bmode\s*[:=]\s*armed\b", re.IGNORECASE),
        "Changing or invoking armed mode through Bash is blocked inside Claude Code.",
    ),
    (
        re.compile(r"\barm(?:ed)?[-_ ]?(?:run|experiment)\b", re.IGNORECASE),
        "A command that appears to arm or run live hardware is blocked.",
    ),
)

def deny(reason: str) -> None:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    sys.stdout.write("\n")

def main() -> int:
    try:
        payload: dict[str, Any] = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        deny("MAXONE safety hook could not parse the Bash tool request.")
        return 0

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        deny("MAXONE safety hook received an invalid tool input.")
        return 0

    command = tool_input.get("command", "")
    if not isinstance(command, str):
        deny("MAXONE safety hook received a non-string command.")
        return 0

    for pattern, reason in BLOCK_PATTERNS:
        if pattern.search(command):
            deny(reason)
            return 0

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
