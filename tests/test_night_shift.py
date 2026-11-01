"""The night shift against a fake Airflow: nothing here touches the network or the warehouse."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from megavolt_ops import night_shift
from megavolt_ops.night_shift import Airflow, FailedRun, State, Verdict, fold, shift

NOW = datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
RUN = {
    "dag_id": "dbt_build",
    "dag_run_id": "scheduled__2026-10-31T04:30:00+00:00",
    "end_date": "2026-10-31T09:29:06Z",
}
ENTRIES = [
    {"timestamp": "t1", "level": "info", "event": "Filling up the DagBag"},
    {
        "timestamp": "t2",
        "level": "error",
        "event": "Task failed with exception",
        "error_detail": [{"exc_type": "AirflowException", "exc_value": "exit code 1"}],
    },
]
TRANSIENT = Verdict("ENTSO-E unreachable", transient=True, suggested_fix="wait")
BROKEN = Verdict("model SQL broken", transient=False)
SILENT = lambda subject, body: None  # noqa: E731


class FakeAirflow:
    """Answers the five calls the night shift makes and records every clear."""

    def __init__(self):
        self.runs = [RUN]
        self.tasks = [{"task_id": "dbt_build", "try_number": 2}]
        self.cleared = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/auth/token":
            return httpx.Response(201, json={"access_token": "secret-token"})
        assert request.headers["Authorization"] == "Bearer secret-token"
        if path == "/api/v2/dags/~/dagRuns":
            assert request.url.params["state"] == "failed"
            return httpx.Response(200, json={"dag_runs": self.runs})
        if path.endswith("/taskInstances"):
            return httpx.Response(200, json={"task_instances": self.tasks})
        if "/logs/" in path:
            assert request.headers["Accept"] == "application/json"
            return httpx.Response(200, json={"content": ENTRIES})
        if path.endswith("/clearTaskInstances"):
            self.cleared.append(json.loads(request.content))
            return httpx.Response(200, json={"task_instances": []})
        return httpx.Response(404)

    def client(self) -> Airflow:
        client = httpx.Client(transport=httpx.MockTransport(self.handle))
        return Airflow("http://airflow.test", "admin", "pw", client)


@pytest.fixture
def world(tmp_path):
    fake = FakeAirflow()
    return fake, fake.client(), State(tmp_path / "state.json"), tmp_path / "incidents"


def test_secrets_held_by_the_process_never_reach_a_report(world, monkeypatch):
    fake, airflow, state, incidents = world
    monkeypatch.setenv("WAREHOUSE_DBT_PASSWORD", "exit code 1")
    written = shift(airflow, state, incidents, NOW, lambda *a: BROKEN, SILENT)
    assert "AirflowException: ***" in written[0].read_text("utf-8")


def test_fold_keeps_one_line_per_entry_with_the_exception():
    assert fold(ENTRIES + ["not a dict"]).splitlines() == [
        "t1 info Filling up the DagBag",
        "t2 error Task failed with exception | AirflowException: exit code 1",
    ]


def test_a_failed_run_becomes_a_report_and_is_not_handled_twice(world):
    fake, airflow, state, incidents = world
    written = shift(airflow, state, incidents, NOW, lambda *a: BROKEN, SILENT)
    assert [p.name for p in written] == ["2026-10-31T092906Z-dbt_build.md"]
    report = written[0].read_text(encoding="utf-8")
    assert "AirflowException: exit code 1" in report and "model SQL broken" in report
    assert "secret-token" not in report
    assert fake.cleared == []
    assert shift(airflow, state, incidents, NOW, lambda *a: BROKEN, SILENT) == []
    assert State(state.path).data["last_checked"] == NOW.isoformat()


def test_the_first_pass_looks_one_day_back_and_later_passes_from_the_last_check(tmp_path):
    state = State(tmp_path / "state.json")
    assert state.since(NOW) == NOW - night_shift.FIRST_LOOK_BACK
    state.data["last_checked"] = "2026-11-01T05:30:00+00:00"
    assert state.since(NOW) == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)


def test_a_full_page_moves_the_check_to_the_last_failure_seen(world):
    fake, airflow, state, incidents = world
    fake.runs = [
        {**RUN, "dag_run_id": f"r{i}", "end_date": f"2026-10-31T0{i % 10}:00:00Z"}
        for i in range(100)
    ]
    shift(airflow, state, incidents, NOW, lambda *a: BROKEN, SILENT)
    assert state.data["last_checked"] == fake.runs[-1]["end_date"]


def test_a_transient_verdict_clears_the_run_exactly_once(world):
    fake, airflow, state, incidents = world
    shift(airflow, state, incidents, NOW, lambda *a: TRANSIENT, SILENT)
    assert fake.cleared == [
        {"dag_run_id": RUN["dag_run_id"], "only_failed": True, "dry_run": False}
    ]
    # The cleared run fails again (same run id, a later end): a new report, no second clear.
    fake.runs = [{**RUN, "end_date": "2026-10-31T10:00:00Z"}]
    written = shift(airflow, state, incidents, NOW, lambda *a: TRANSIENT, SILENT)
    assert len(fake.cleared) == 1
    assert "Cleared for a re-run: no" in written[0].read_text("utf-8")


def test_the_clear_is_remembered_before_the_mail_can_fail(world):
    fake, airflow, state, incidents = world

    def broken_mail(subject, body):
        raise night_shift.smtplib.SMTPException("gmail down")

    with pytest.raises(night_shift.smtplib.SMTPException):
        shift(airflow, state, incidents, NOW, lambda *a: TRANSIENT, broken_mail)
    again = State(state.path)
    assert again.was_cleared(FailedRun("dbt_build", RUN["dag_run_id"], "x"))
    assert again.agent_runs_today(NOW) == 1
    assert next(incidents.glob("*.md")).exists()


def test_a_run_without_a_failed_task_is_reported_not_fatal(world):
    fake, airflow, state, incidents = world
    fake.tasks = []
    written = shift(airflow, state, incidents, NOW, lambda *a: TRANSIENT, SILENT)
    assert "not analysed: no failed task instance" in written[0].read_text("utf-8")
    assert fake.cleared == []
    assert state.is_handled(FailedRun("dbt_build", RUN["dag_run_id"], RUN["end_date"]))


def test_the_daily_cap_stops_the_analyser_not_the_report(world):
    fake, airflow, state, incidents = world
    fake.runs = [
        {**RUN, "dag_run_id": f"run{i}", "end_date": f"2026-10-31T0{i}:00:00Z"} for i in range(5)
    ]
    calls = []

    def analyse(run, task, log):
        calls.append(run.run_id)
        return BROKEN

    written = shift(airflow, state, incidents, NOW, analyse, SILENT)
    assert len(written) == 5 and len(calls) == night_shift.AGENT_RUNS_PER_DAY
    assert "daily cap" in written[-1].read_text("utf-8")


def test_notify_gets_the_subject_and_the_report(world):
    fake, airflow, state, incidents = world
    sent = []
    shift(airflow, state, incidents, NOW, lambda *a: BROKEN, lambda s, b: sent.append((s, b)))
    assert sent[0][0] == "[megavolt] dbt_build failed: model SQL broken"
    assert "## Log tail" in sent[0][1]


def test_airflow_errors_are_one_exception_type():
    def down(request):
        raise httpx.ConnectError("refused")

    airflow = Airflow("http://x", "u", "p", httpx.Client(transport=httpx.MockTransport(down)))
    with pytest.raises(night_shift.NightShiftError, match="unreachable"):
        airflow.failed_runs(NOW)

    def denied(request):
        return httpx.Response(401, json={"detail": "no"})

    airflow = Airflow("http://x", "u", "p", httpx.Client(transport=httpx.MockTransport(denied)))
    with pytest.raises(night_shift.NightShiftError, match="401"):
        airflow.failed_runs(NOW)


def test_state_survives_a_round_trip(tmp_path):
    state = State(tmp_path / "s.json")
    run = FailedRun("d", "r", "e")
    state.mark(run, cleared=True)
    state.count_agent_run(NOW)
    state.save()
    again = State(state.path)
    assert again.is_handled(run) and again.was_cleared(run) and again.agent_runs_today(NOW) == 1


@pytest.mark.parametrize(
    ("text", "cause", "transient"),
    [
        ('{"cause": "API down", "transient": true}', "API down", True),
        ('Here: {"cause": "bad SQL", "transient": false} done', "bad SQL", False),
        ('{"cause": "x", "transient": "true"}', "x", False),
        ("I could not tell.", "not analysed: the agent gave no readable verdict", False),
    ],
    ids=["clean json", "json inside text", "string true is not true", "no json"],
)
def test_parse_verdict(text, cause, transient):
    verdict = night_shift.parse_verdict(text)
    assert (verdict.cause, verdict.transient) == (cause, transient)


def test_the_agent_gets_read_only_tools_code_folders_and_the_untrusted_warning(
    monkeypatch, tmp_path
):
    seen = {}

    def fake_run(command, **kwargs):
        seen.update(command=command, **kwargs)
        answer = {"result": '{"cause": "c", "transient": false}'}
        return type("Done", (), {"stdout": json.dumps(answer)})()

    monkeypatch.setattr(night_shift.subprocess, "run", fake_run)
    run = FailedRun("dbt_build", "r1", "e")
    verdict = night_shift.ask_agent(tmp_path)(run, "dbt_build", "the log")
    assert verdict.cause == "c"
    command = seen["command"]
    assert command[1] == "-p" and "untrusted" in command[2] and "the log" in command[2]
    tools = command.index("--tools")
    assert command[tools + 1 : tools + 4] == ["Read", "Glob", "Grep"]
    assert "--max-budget-usd" in command and "--strict-mcp-config" in command
    assert command[command.index("--setting-sources") + 1] == ""
    folders = [str(tmp_path / d) for d in ("dags", "src", "dbt")]
    assert command[command.index("--add-dir") + 1 :] == folders
    assert "--restricted" in command
    assert tmp_path not in seen["cwd"].parents and seen["cwd"].is_dir()
    assert set(seen["env"]) == {"HOME", "PATH"}


def test_an_agent_that_hangs_gives_an_unknown_verdict(monkeypatch, tmp_path):
    def hang(command, **kwargs):
        raise night_shift.subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(night_shift.subprocess, "run", hang)
    verdict = night_shift.ask_agent(tmp_path)(FailedRun("d", "r", "e"), "t", "")
    assert verdict.transient is False and "TimeoutExpired" in verdict.cause


def test_mail_is_sent_with_starttls_and_login_and_a_one_line_subject(monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def starttls(self, context):
            calls.append(("starttls", context.check_hostname))

        def login(self, user, password):
            calls.append(("login", user))

        def send_message(self, message):
            calls.append(("send", message["To"], message["Subject"]))

    monkeypatch.setattr(night_shift.smtplib, "SMTP", FakeSMTP)
    night_shift.send_mail("me@test", "app-pw", "mark@test")("subj\nBcc: x", "body\n")
    assert calls == [
        ("connect", "smtp.gmail.com", 587),
        ("starttls", True),
        ("login", "me@test"),
        ("send", "mark@test", "subj Bcc: x"),
    ]
