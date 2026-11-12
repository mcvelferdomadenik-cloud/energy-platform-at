"""The DAG files are read as text: Airflow is not installed outside its image."""

import ast
from pathlib import Path

DAGS = Path(__file__).resolve().parents[1] / "dags"
# A DAG that handles exactly one day per run must catch up the days the server slept through.
ONE_DAY_PER_RUN = {"meter_stream_daily", "entsoe_day_ahead_prices", "geosphere_weather"}
# A DAG whose single run covers a window, or everything, must not pile up runs instead.
WINDOW_PER_RUN = {"entsoe_actuals", "dbt_build"}


def dag_keywords() -> dict[str, dict[str, ast.expr]]:
    """The keyword arguments of every `@dag(...)` decorator, by dag_id."""
    found = {}
    for path in DAGS.glob("*.py"):
        in_file = []
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.FunctionDef):
                continue
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call) and getattr(decorator.func, "id", "") == "dag":
                    keywords = {kw.arg: kw.value for kw in decorator.keywords}
                    in_file.append(keywords)
                    found[ast.literal_eval(keywords["dag_id"])] = keywords
        # A DAG written any other way (`with DAG(...)`, `@airflow.sdk.dag`) would escape every
        # test here without a sound; one `@dag(...)` per file is the shape this test knows.
        assert len(in_file) == 1, path.name
    return found


def outlets_by_task(dag_file: str) -> dict[str, list[str]]:
    """The names in `outlets=[...]` of each decorated task that has any, by function name."""
    found = {}
    for node in ast.walk(ast.parse((DAGS / dag_file).read_text(encoding="utf-8"))):
        for decorator in getattr(node, "decorator_list", []):
            for keyword in getattr(decorator, "keywords", []):
                if keyword.arg == "outlets":
                    found[node.name] = [element.id for element in keyword.value.elts]
    return found


def test_every_expected_dag_is_present():
    assert ONE_DAY_PER_RUN | WINDOW_PER_RUN | {"apcs_load_profiles"} == set(dag_keywords())


def test_dbt_builds_after_the_delivery_and_its_prices_not_on_the_clock():
    """A list is AND in Airflow: both assets, each marked by the task that stores last."""
    schedule = dag_keywords()["dbt_build"]["schedule"]
    assert isinstance(schedule, ast.List)
    assert all(asset.func.id == "Asset" for asset in schedule.elts)
    assert sorted(ast.literal_eval(asset.args[0]) for asset in schedule.elts) == [
        "imbalance_prices",
        "meter_delivery",
    ]
    meter = (DAGS / "meter_stream_daily.py").read_text(encoding="utf-8")
    assert 'METER_DELIVERY = Asset("meter_delivery")' in meter
    assert outlets_by_task("meter_stream_daily.py") == {"wait_for_storage": ["METER_DELIVERY"]}
    actuals = (DAGS / "entsoe_actuals.py").read_text(encoding="utf-8")
    assert 'IMBALANCE_PRICES = Asset("imbalance_prices")' in actuals
    assert outlets_by_task("entsoe_actuals.py") == {"fetch_imbalance_prices": ["IMBALANCE_PRICES"]}


def test_every_dag_states_its_policy():
    for dag_id, keywords in dag_keywords().items():
        assert {"catchup", "max_active_runs"} <= set(keywords), dag_id


def test_one_day_per_run_dags_catch_up():
    dags = dag_keywords()
    for dag_id in ONE_DAY_PER_RUN:
        assert ast.literal_eval(dags[dag_id]["catchup"]) is True, dag_id


def test_window_dags_do_not_catch_up():
    dags = dag_keywords()
    for dag_id in WINDOW_PER_RUN | {"apcs_load_profiles"}:
        assert ast.literal_eval(dags[dag_id]["catchup"]) is False, dag_id


def test_scheduled_dags_run_one_at_a_time():
    """Catch-up runs must stay in date order, so no DAG may run two days at once."""
    for dag_id, keywords in dag_keywords().items():
        assert ast.literal_eval(keywords["max_active_runs"]) == 1, dag_id
