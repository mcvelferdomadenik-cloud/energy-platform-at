"""The Airflow image and the laptop must run the same versions of the runtime dependencies."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def locked_versions() -> dict[str, str]:
    """Every package in uv.lock with the one version it is locked to."""
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    return {package["name"]: package["version"] for package in lock["package"]}


def image_pins() -> dict[str, str]:
    """The exact pins the image installs, extras such as psycopg[binary] stripped."""
    text = (ROOT / "airflow" / "requirements.txt").read_text(encoding="utf-8")
    return dict(re.findall(r"^([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?==(\S+)$", text, re.M))


def test_the_image_pins_every_runtime_dependency_to_the_locked_version():
    pins, locked = image_pins(), locked_versions()
    assert pins, "airflow/requirements.txt holds no exact pins"
    assert {name: locked.get(name) for name in pins} == pins


def test_no_runtime_dependency_is_missing_from_the_image():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    names = {re.match(r"[A-Za-z0-9_.-]+", spec).group(0) for spec in project["dependencies"]}
    assert names == set(image_pins())
