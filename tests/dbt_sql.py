"""Turn a dbt model into plain SQL, so a test can run it without a dbt build."""

import re
from pathlib import Path

import yaml

PROJECT = Path(__file__).resolve().parents[1] / "dbt"
MODELS = PROJECT / "models"
VARS = yaml.safe_load((PROJECT / "dbt_project.yml").read_text(encoding="utf-8"))["vars"]


def rendered(model: str) -> str:
    """The model's SQL with source() and ref() resolved by hand, wherever the model lives."""
    # A model, or one of the singular tests, which are queries too. Exactly one file may match.
    (path,) = [*MODELS.rglob(f"{model}.sql"), *(PROJECT / "tests").glob(f"{model}.sql")]
    sql = path.read_text(encoding="utf-8")
    # A config block tells dbt how to build the model; it is not part of the query.
    sql = re.sub(r"\{\{\s*config\(.*?\)\s*\}\}", "", sql, flags=re.DOTALL)
    sql = re.sub(r"\{\{\s*var\('(\w+)'\)\s*\}\}", lambda m: str(VARS[m.group(1)]), sql)
    sql = re.sub(r"\{\{\s*source\('raw',\s*'(\w+)'\)\s*\}\}", r"raw.\1", sql)
    return re.sub(r"\{\{\s*ref\('(\w+)'\)\s*\}\}", lambda m: f"({rendered(m.group(1))})", sql)
