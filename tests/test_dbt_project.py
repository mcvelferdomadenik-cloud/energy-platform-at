"""Guards on the dbt project files: pinned packages, no literal credentials, no quiet warnings."""

import re
from pathlib import Path

DBT = Path(__file__).resolve().parents[1] / "dbt"


def test_every_package_is_pinned_to_an_exact_version():
    versions = re.findall(r"^\s*version:\s*(\S+)", (DBT / "packages.yml").read_text(), re.M)
    assert versions and all(re.fullmatch(r"\d+\.\d+\.\d+", v) for v in versions), versions


def test_the_lock_file_pins_the_same_versions():
    lock = (DBT / "package-lock.yml").read_text()
    for version in re.findall(r"^\s*version:\s*(\S+)", (DBT / "packages.yml").read_text(), re.M):
        assert f"version: {version}" in lock
    assert re.search(r"^sha1_hash:\s*[0-9a-f]{40}$", lock, re.M)


def test_the_profile_holds_names_of_variables_and_never_a_value():
    for line in (DBT / "profiles.yml").read_text().splitlines():
        if re.match(r"\s*(password|user|host|dbname):", line):
            assert "env_var(" in line or line.strip() == "user: megavolt_dbt", line


def test_a_singular_test_that_only_warns_says_why():
    for test in DBT.glob("tests/*.sql"):
        text = test.read_text()
        if re.search(r"severity\s*=\s*'warn'", text):
            assert re.search(r"^--.*\bwarn\b", text, re.M), f"{test.name} warns without a reason"


def test_a_warning_severity_test_carries_a_reason():
    for yml in DBT.glob("models/**/*.yml"):
        lines = yml.read_text().splitlines()
        for number, line in enumerate(lines):
            if "severity: warn" in line:
                above = " ".join(lines[max(0, number - 6) : number])
                assert "#" in above, f"{yml.name}:{number + 1} warns without a written reason"
