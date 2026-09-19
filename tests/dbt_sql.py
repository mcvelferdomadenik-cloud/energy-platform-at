"""Turn a dbt staging model into plain SQL, so a test can run it without a dbt build."""

import re
from pathlib import Path

MODELS = Path(__file__).resolve().parents[1] / "dbt" / "models" / "staging"


def rendered(model: str) -> str:
    """The model's SQL with source() and ref() resolved by hand."""
    sql = (MODELS / f"{model}.sql").read_text(encoding="utf-8")
    sql = re.sub(r"\{\{\s*source\('raw',\s*'(\w+)'\)\s*\}\}", r"raw.\1", sql)
    return re.sub(r"\{\{\s*ref\('(\w+)'\)\s*\}\}", lambda m: f"({rendered(m.group(1))})", sql)
