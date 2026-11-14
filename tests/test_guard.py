"""The port guard asks on listeners and stays silent on everything else."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "guard.py"


def verdict(command: str) -> str | None:
    """Run the hook as Claude Code does and return the decision, or None for silence."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    out = subprocess.run(
        [sys.executable, GUARD], input=payload, capture_output=True, text=True, check=True
    ).stdout
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else None


@pytest.mark.parametrize(
    "command",
    [
        "uv run --group tool uvicorn megavolt_tool.main:app --host 127.0.0.1 --port 8000",
        "sudo docker run --rm -p 8080:80 nginx",
        "python3 -m http.server 8765",
        "ssh -L 5433:127.0.0.1:5433 mark@server",
        "nc -l 9000",
        "socat TCP-LISTEN:9000,fork EXEC:/bin/sh",
        "airflow api-server",
    ],
)
def test_a_listener_asks(command):
    assert verdict(command) == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "uv run pytest tests/test_tool.py -q",
        "git status --short",
        "curl -s http://127.0.0.1:8080/api/v2/dags",
        "grep -n port dags/dbt_build.py",
        "sudo docker compose ps",
        "psql -h 127.0.0.1 -p 5433 -U megavolt_reader",
    ],
)
def test_a_client_or_plain_command_is_silent(command):
    assert verdict(command) is None


def test_a_huge_command_asks_instead_of_timing_out():
    assert verdict("nc " * 200_000) == "ask"
