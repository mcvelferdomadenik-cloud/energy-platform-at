"""A late day-ahead publication is waited for; the DAG file is read as text, without Airflow."""

import ast
from datetime import timedelta
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "dags" / "entsoe_day_ahead_prices.py"


def fetch_task() -> ast.FunctionDef:
    """The one task of the DAG."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "fetch_and_store"
    )


def sensor_arguments() -> dict[str, ast.expr]:
    """The keyword arguments of the task's `@task.sensor(...)` decorator."""
    (decorator,) = fetch_task().decorator_list
    assert ast.unparse(decorator.func) == "task.sensor"
    return {keyword.arg: keyword.value for keyword in decorator.keywords}


def duration(call: ast.expr) -> timedelta:
    """A `timedelta(...)` call in the source, as a value."""
    assert ast.unparse(call.func) == "timedelta"
    return timedelta(**{keyword.arg: ast.literal_eval(keyword.value) for keyword in call.keywords})


def test_the_fetch_waits_for_a_late_publication_without_holding_a_worker():
    arguments = sensor_arguments()
    assert ast.literal_eval(arguments["mode"]) == "reschedule"
    assert timedelta(hours=8) <= duration(arguments["timeout"]) < timedelta(hours=24)
    assert duration(arguments["poke_interval"]) <= timedelta(hours=1)


def test_only_a_missing_publication_is_waited_for():
    (handler,) = (node for node in ast.walk(fetch_task()) if isinstance(node, ast.ExceptHandler))
    assert ast.unparse(handler.type) == "EntsoeError"
    unpublished, *rest = handler.body
    assert ast.unparse(unpublished) == "if NOT_PUBLISHED not in str(exc):\n    raise"
    assert ast.unparse(rest[-1]) == "return False"
