"""Claude Code PreToolUse hook: a Bash command that opens a port needs Mark's yes first."""

import json
import re
import sys

LISTENERS = [
    r"\buvicorn\b",
    r"\bgunicorn\b",
    r"\bhypercorn\b",
    r"\bflask\s+run\b",
    r"\bhttp\.server\b",
    r"--port\b",
    r"--bind\b",
    r"--host\b",
    r"\b0\.0\.0\.0\b",
    r"\bdocker\s+run\b.*(\s-p\s*\d|\s--publish\b|\s-P\b)",
    r"\b(nc|ncat|netcat)\b.*\s-l\b",
    r"\bsocat\b.*\bLISTEN\b",
    r"\bssh\b.*\s-[LRD]\s*\d",
    r"\bairflow\s+(api-server|standalone|webserver)\b",
    r"\bredpanda\s+start\b",
    r"\blisten\b",
]


def opens_port(command: str) -> str | None:
    """The text that makes the command a listener, or None; a huge command is asked about as is."""
    if len(command) > 20_000:
        return "a command too long to check"
    for pattern in LISTENERS:
        match = re.search(pattern, command, re.IGNORECASE)
        if match:
            return match.group(0).strip()
    return None


def main() -> None:
    """Read the hook payload, answer 'ask' when the command would open a port."""
    payload = json.load(sys.stdin)
    hit = opens_port(payload.get("tool_input", {}).get("command", ""))
    if hit:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "ask",
                        "permissionDecisionReason": f"opens a port: {hit}",
                    }
                }
            )
        )


if __name__ == "__main__":
    main()
