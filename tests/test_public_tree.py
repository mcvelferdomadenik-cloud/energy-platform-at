"""This repository is public. Guards on what a tracked file may say."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DOCUMENTS = ("CLAUDE.md", "PROJECT_PLAN.md", "THREAT_MODEL.md", "decisions.md")
# The ignore files have to name the private documents in order to ignore them.
MAY_NAME_PRIVATE_DOCUMENTS = {".gitignore", ".dockerignore", "tests/test_public_tree.py"}
# Working notes number their decisions and threats with a letter and a number. Those numbers mean
# nothing to a reader here and point at documents that are not published.
INTERNAL_ID = re.compile(r"\b[DT][0-9]{1,2}\b")
LOCK_FILES = {"uv.lock", "airflow/requirements-dbt.txt", "dbt/package-lock.yml"}


def tracked_text_files() -> list[tuple[str, str]]:
    """Every tracked file that reads as text, with its content."""
    names = subprocess.run(  # noqa: S603
        ["git", "ls-files"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    files = []
    for name in names:
        try:
            files.append((name, (ROOT / name).read_text(encoding="utf-8")))
        except (UnicodeDecodeError, FileNotFoundError):
            continue
    return files


def test_no_private_document_is_tracked():
    tracked = {name for name, _ in tracked_text_files()}
    assert not tracked & set(PRIVATE_DOCUMENTS)
    assert not [name for name in tracked if name.startswith("data/") or "/try_" in f"/{name}"]


def test_no_tracked_file_points_at_a_private_document():
    hits = [
        f"{name}: {document}"
        for name, text in tracked_text_files()
        if name not in MAY_NAME_PRIVATE_DOCUMENTS
        for document in PRIVATE_DOCUMENTS
        if document in text
    ]
    assert not hits, hits


def test_no_tracked_file_carries_an_internal_decision_or_threat_number():
    hits = [
        f"{name}:{number}: {line.strip()[:80]}"
        for name, text in tracked_text_files()
        if name not in LOCK_FILES
        for number, line in enumerate(text.splitlines(), start=1)
        if INTERNAL_ID.search(line)
    ]
    assert not hits, hits
