"""The secret-scanning rules must keep catching what they were written to catch.

A scan of a clean repository passes whether the rules work or not, so a weakened rule would go
unnoticed for as long as nothing leaks. These cases hand known-bad and known-good lines straight
to gitleaks. Every secret below is invented; `gitleaks:allow` keeps the repository scan from
reporting this file, while the value handed to gitleaks carries no such marker.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GITLEAKS = shutil.which("gitleaks")

if GITLEAKS is None:
    if os.environ.get("CI"):
        raise RuntimeError("gitleaks must be installed in CI, or the secret rules go untested")
    pytest.skip("gitleaks is not installed on this machine", allow_module_level=True)

MUST_BE_CAUGHT = {
    "password-assignment": "WAREHOUSE_WRITER_PASSWORD=Qz7fakeFAKEfake91xY",  # gitleaks:allow
    "password-in-dsn": "postgresql://writer:Qz7fakeFAKEfake91xY@db:5432/megavolt",  # gitleaks:allow
    "token-in-python": 'token = "Zr4fakeFAKEfake22pLm"',  # gitleaks:allow
    "token-in-url": "api?securityToken=Hq8fakeFAKEfake5Tz2w&documentType=A44",  # gitleaks:allow
    "fernet-in-yaml": "FERNET_KEY: kX9fAkEfAkEfAkEfAkEfAkEfAkEfAkEfAkEfAkE0aQ8=",  # gitleaks:allow
    "github-token": "# ghp_aB3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6",  # gitleaks:allow
}

# Real lines from this repository. A rule that reports them trains everyone to ignore it.
MUST_PASS = {
    "placeholder-dsn": "WAREHOUSE_DSN=postgresql://megavolt_writer:CHANGEME@127.0.0.1:5433/megavolt",
    "empty-example-value": "POSTGRES_PASSWORD=",
    "compose-secret": "JWT_SECRET: ${AIRFLOW_JWT_SECRET:?set AIRFLOW_JWT_SECRET in .env}",
    "compose-dsn": "postgresql+psycopg2://airflow:${AIRFLOW_DB_PASSWORD:?set it}@airflow-db/db",
    "env-variable-name": 'TOKEN_ENV = "ENTSOE_API_TOKEN"  # noqa: S105',
    "psql-variable": "CREATE ROLE megavolt_writer LOGIN PASSWORD :'writer_password';",
    "reading-the-environment": 'token = os.environ.get(TOKEN_ENV, "").strip()',
    "redacted-url": 'redact(url) == "api?securityToken=***&documentType=A44"',
}


def leaks(line: str) -> bool:
    """Ask gitleaks whether this one line contains a secret under the repository's rules."""
    result = subprocess.run(  # noqa: S603 - fixed executable and arguments, input is test data
        [GITLEAKS, "stdin", "--config", str(ROOT / ".gitleaks.toml"), "--no-banner", "--redact"],
        input=line,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise AssertionError(f"gitleaks failed with {result.returncode}: {result.stderr}")
    return result.returncode == 1


@pytest.mark.parametrize("line", MUST_BE_CAUGHT.values(), ids=MUST_BE_CAUGHT.keys())
def test_a_secret_is_caught(line):
    assert leaks(line)


@pytest.mark.parametrize("line", MUST_PASS.values(), ids=MUST_PASS.keys())
def test_a_line_that_only_looks_like_a_secret_is_left_alone(line):
    assert not leaks(line)
