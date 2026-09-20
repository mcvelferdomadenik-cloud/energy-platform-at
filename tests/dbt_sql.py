"""Turn a dbt model into plain SQL, so a test can run it without a dbt build."""

import re
from pathlib import Path

MODELS = Path(__file__).resolve().parents[1] / "dbt" / "models"


def rendered(model: str) -> str:
    """The model's SQL with source() and ref() resolved by hand, wherever the model lives."""
    (path,) = MODELS.rglob(f"{model}.sql")
    sql = path.read_text(encoding="utf-8")
    # A config block tells dbt how to build the model; it is not part of the query.
    sql = re.sub(r"\{\{\s*config\(.*?\)\s*\}\}", "", sql, flags=re.DOTALL)
    sql = re.sub(r"\{\{\s*source\('raw',\s*'(\w+)'\)\s*\}\}", r"raw.\1", sql)
    return re.sub(r"\{\{\s*ref\('(\w+)'\)\s*\}\}", lambda m: f"({rendered(m.group(1))})", sql)
