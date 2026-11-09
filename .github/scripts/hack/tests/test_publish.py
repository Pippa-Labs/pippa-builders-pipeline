"""Unit tests for publish.py; boto3 and the GitHub token endpoint are faked."""

import base64
import importlib.util
import io
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

sys.dont_write_bytecode = True
MODULE = Path(__file__).resolve().parent.parent / "publish.py"
spec = importlib.util.spec_from_file_location("hack_publish", MODULE)
hp = importlib.util.module_from_spec(spec)
sys.modules["hack_publish"] = hp
spec.loader.exec_module(hp)

RUN_URL = "https://github.com/Pippa-Labs/pippa-hack-x/actions/runs/1"
REF = "gh-1-1"

MANIFEST = "manifest_version: 1\napp:\n  id: hack.x\n  name: Days Until\n  version: 0.1.0\n"


@pytest.fixture
def app(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "app.py").write_text("class App: pass\n")
    (root / "manifest.yaml").write_text(MANIFEST)
    (root / "helper.py").write_text("X = 1\n")
    (root / "test_app.py").write_text("def test(): pass\n")
    (root / "requirements-dev.txt").write_text("pytest\n")
    (root / "CLAUDE.md").write_text("x")
    for d in ("scripts", "vendor", "docs", ".github/workflows", ".git", "__pycache__"):
        (root / d).mkdir(parents=True, exist_ok=True)
        (root / d / "f.py").write_text("x")
    (root / "vendor" / "sdk.whl").write_text("x")
    (root / "data").mkdir()
    (root / "data" / "ports.json").write_text("{}")
    (root / "data" / "archive.tar.gz").write_text("x")
    return root


class FakePayload:
    def __init__(self, raw):
        self.raw = raw

    def read(self):
        return self.raw


class FakeClient:
    def __init__(self, raw=b"", function_error=None, exc=None, failures=None):
        self.raw = raw
        self.function_error = function_error
        self.exc = exc
        self.failures = list(failures or [])
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        if self.failures:
            raise self.failures.pop(0)
        if self.exc:
            raise self.exc
        response = {"Payload": FakePayload(self.raw), "ResponseMetadata": {"RequestId": "req-123"}}
        if self.function_error:
            response["FunctionError"] = self.function_error
        return response


class FakeToken(hp.TokenProvider):
    def __init__(self, exc=None):
        self.exc = exc
        self.audiences = []

    def token(self, audience):
        self.audiences.append(audience)
        if self.exc:
            raise self.exc
        return "jwt"


def make_run(app_dir, client=None, token=None):
    client = client or FakeClient(b"{}")
    return hp.PublishRun(
        app_dir=app_dir,
        factory=hp.OutcomeFactory(RUN_URL, REF),
        token_provider=token or FakeToken(),
        publisher_factory=lambda: hp.LambdaPublisher(client=client, sleep=lambda _: None),
    ), client


SUCCESS = {
    "statusCode": 200, "ok": True, "app_id": "hack.x", "version": "0.1.0",
    "name": "Days Until", "outcomes": ["days.until"], "function": "pippa-hack-x",
}


# ---- packaging ----

def test_package_keeps_only_runtime_files(app):
    package = hp.Packager(app).build()
    assert sorted(package.files) == ["app.py", "data/ports.json", "helper.py", "manifest.yaml"]
    names = zipfile.ZipFile(io.BytesIO(package.data)).namelist()
    assert sorted(names) == sorted(package.files)


def test_package_skips_symlinks(app, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("token")
    os.symlink(secret, app / "leak.txt")
    os.symlink(tmp_path, app / "linkdir")
    assert "leak.txt" not in hp.Packager(app).build().files
    assert not any(f.startswith("linkdir") for f in hp.Packager(app).build().files)


def test_package_over_limit_is_code_error_naming_largest_files(app):
    (app / "cities.json").write_bytes(os.urandom(40 * 1024))
    with pytest.raises(hp.CodeError) as err:
        hp.Packager(app, limit=10 * 1024).build()
    text = str(err.value)
    assert "ERROR: the package is" in text and "the limit is 10 KB" in text
    assert "cities.json" in text


def test_default_limit_is_3_mb(app):
    assert hp.MAX_BUNDLE_BYTES == 3 * 1024 * 1024
    (app / "big.bin").write_bytes(os.urandom(3 * 1024 * 1024 + 1024))
    with pytest.raises(hp.CodeError, match="the limit is 3072 KB"):
        hp.Packager(app).build()


def test_package_just_under_limit_passes(app):
    (app / "big.bin").write_bytes(os.urandom(3 * 1024 * 1024 - 64 * 1024))
    assert "big.bin" in hp.Packager(app).build().files


def test_missing_app_py_is_code_error(app):
    (app / "app.py").unlink()
    with pytest.raises(hp.CodeError, match="app.py is not in the package"):
        hp.Packager(app).build()


# ---- invoke contract ----

def test_success_event_and_outcome(app):
    run, client = make_run(app, FakeClient(json.dumps(SUCCESS).encode()))
    token = FakeToken()
    run.token_provider = token
    outcome = run.execute(hp.CheckState())
    assert isinstance(outcome, hp.Published)
    assert token.audiences == ["pippa-hackathon"]
    call = client.calls[0]
    assert call["FunctionName"] == "pippa-hack-publish"
    assert call["InvocationType"] == "RequestResponse"
    event = json.loads(call["Payload"])
    assert set(event) == {"token", "manifest", "code_zip_b64"}
    assert event["token"] == "jwt"
    assert event["manifest"]["app"]["id"] == "hack.x"
    names = zipfile.ZipFile(io.BytesIO(base64.b64decode(event["code_zip_b64"]))).namelist()
    assert "app.py" in names and "test_app.py" not in names


def test_success_payloads(app):
    run, _ = make_run(app, FakeClient(json.dumps(SUCCESS).encode()))
    outcome = run.execute(hp.CheckState())
    assert outcome.kind == "success"
    assert outcome.status_payload() == {
        "state": "success", "context": "pippa/publish",
        "description": "Live: hack.x v0.1.0", "target_url": RUN_URL,
    }
    assert outcome.comment_payload() == {"body": (
        "Your app Days Until (hack.x v0.1.0) is live. Open https://app.getpippa.ai, "
        "go to Apps, install it and try it."
    )}
    assert "`days.until`" in outcome.summary


def test_publisher_code_error(app, capsys):
    reply = {"statusCode": 400, "ok": False, "error_class": "code",
             "error": "app id must be hack.x, not hack.y"}
    run, _ = make_run(app, FakeClient(json.dumps(reply).encode()))
    outcome = run.execute(hp.CheckState())
    assert outcome.kind == "code"
    body = outcome.comment_payload()["body"]
    assert body.startswith("CODE_ERROR")
    assert "app id must be hack.x, not hack.y" in body
    assert "Fix the code and push again." in body
    assert RUN_URL in body
    status = outcome.status_payload()
    assert status["state"] == "failure" and status["context"] == "pippa/publish"
    assert status["description"] == "Code error: app id must be hack.x, not hack.y"
    assert "Pippa refused to publish this app:" in capsys.readouterr().out


def test_publisher_infra_error_uses_reference(app):
    reply = {"statusCode": 502, "ok": False, "error_class": "infra",
             "error": "register returned 503", "reference": "pub-abc"}
    run, _ = make_run(app, FakeClient(json.dumps(reply).encode()))
    outcome = run.execute(hp.CheckState())
    assert outcome.kind == "infra"
    body = outcome.comment_payload()["body"]
    assert body.startswith("PLATFORM_ERROR")
    assert "pub-abc" in body and "Do not change your code." in body
    assert "register returned 503" not in body
    assert outcome.status_payload()["description"] == "Platform error, reference pub-abc"


def test_publisher_refusal_without_class_is_infra(app):
    run, _ = make_run(app, FakeClient(json.dumps({"statusCode": 403, "ok": False, "error": "x"}).encode()))
    assert run.execute(hp.CheckState()).kind == "infra"


@pytest.mark.parametrize("exc", [
    ReadTimeoutError(endpoint_url="https://lambda"),
    ConnectTimeoutError(endpoint_url="https://lambda"),
    ClientError({"Error": {"Code": "TooManyRequestsException", "Message": "Rate"}}, "Invoke"),
    RuntimeError("boom"),
])
def test_invoke_exceptions_are_infra(app, exc):
    run, _ = make_run(app, FakeClient(exc=exc))
    outcome = run.execute(hp.CheckState())
    assert outcome.kind == "infra"
    assert REF in outcome.comment_payload()["body"]
    assert type(exc).__name__ not in outcome.comment_payload()["body"]


def test_function_error_is_infra_with_request_id(app, caplog):
    run, _ = make_run(app, FakeClient(b'{"errorMessage":"Task timed out"}', function_error="Unhandled"))
    outcome = run.execute(hp.CheckState())
    assert "Unhandled" in caplog.text and "req-123" in caplog.text
    assert "Task timed out" not in caplog.text
    assert outcome.kind == "infra"
    assert "req-123" in outcome.comment_payload()["body"]
    assert "Task timed out" not in outcome.comment_payload()["body"]


@pytest.mark.parametrize("raw", [b"not json", b"[1,2]", b'"text"', json.dumps({"ok": True}).encode()])
def test_unparseable_or_malformed_reply_is_infra(app, raw):
    run, _ = make_run(app, FakeClient(raw))
    assert run.execute(hp.CheckState()).kind == "infra"


def test_token_failure_is_infra(app):
    run, client = make_run(app, token=FakeToken(exc=hp.PlatformError("no token")))
    assert run.execute(hp.CheckState()).kind == "infra"
    assert client.calls == []


def test_actions_token_provider_without_env_is_platform_error():
    with pytest.raises(hp.PlatformError):
        hp.ActionsTokenProvider(environ={}).token("pippa-hackathon")


def test_actions_token_provider_request(monkeypatch):
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.headers["Authorization"]
        return Resp(b'{"value": "jwt-1"}')

    monkeypatch.setattr(hp.urllib.request, "urlopen", fake_urlopen)
    provider = hp.ActionsTokenProvider(environ={
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.example/x?api-version=2.0",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "rt",
    })
    assert provider.token("pippa-hackathon") == "jwt-1"
    assert seen["url"].endswith("&audience=pippa-hackathon")
    assert seen["auth"] == "bearer rt"


# ---- preflight from the check job / steps ----

def test_check_failure_is_code_without_invoke(app):
    run, client = make_run(app)
    outcome = run.execute(hp.CheckState(result="failure", verdict="failed",
                                        details=" FAIL the tests fail\n       Publishing runs them too"))
    assert outcome.kind == "code"
    assert "FAIL the tests fail" in outcome.comment_payload()["body"]
    assert outcome.status_payload()["description"] == "Code error: FAIL the tests fail"
    assert client.calls == []


def test_check_job_failure_without_verdict_is_infra(app):
    run, client = make_run(app)
    assert run.execute(hp.CheckState(result="failure", verdict="")).kind == "infra"
    assert client.calls == []


def test_credentials_failure_is_infra(app):
    run, client = make_run(app)
    assert run.execute(hp.CheckState(credentials="failure")).kind == "infra"
    assert client.calls == []


def test_setup_failure_is_infra(app):
    run, client = make_run(app)
    assert run.execute(hp.CheckState(setup="failure")).kind == "infra"
    assert client.calls == []


def test_bad_manifest_is_code_error(app):
    (app / "manifest.yaml").write_text("app: [unclosed\n")
    run, client = make_run(app)
    assert run.execute(hp.CheckState()).kind == "code"
    assert client.calls == []


# ---- rendering ----

def test_status_description_truncated_to_140():
    outcome = hp.CodeFailure(RUN_URL, "x" * 500)
    assert len(outcome.status_payload()["description"]) == 140


def test_code_fence_survives_backticks():
    outcome = hp.CodeFailure(RUN_URL, "bad ```inner``` text")
    body = outcome.message
    assert "````text" in body


def test_details_truncated():
    outcome = hp.CodeFailure(RUN_URL, "y" * 20000)
    assert len(outcome.error) == hp.DETAILS_LIMIT


# ---- CLI ----

def test_main_writes_report_files(app, tmp_path):
    details = tmp_path / "details.txt"
    details.write_text(" FAIL manifest.yaml is not valid: x")
    out = tmp_path / "out"
    rc = hp.main([
        "--app-dir", str(app), "--run-url", RUN_URL, "--reference", REF,
        "--out-dir", str(out), "--check-result", "failure", "--check-verdict", "failed",
        "--check-details-file", str(details), "--credentials", "skipped",
    ])
    assert rc == 0
    assert (out / "outcome").read_text() == "code"
    status = json.loads((out / "status.json").read_text())
    assert status["context"] == "pippa/publish" and status["state"] == "failure"
    comment = json.loads((out / "comment.json").read_text())
    assert comment["body"].startswith("CODE_ERROR")
    assert (out / "summary.md").read_text().startswith("### Publish to Pippa")


def test_main_credentials_failure_writes_infra(app, tmp_path):
    out = tmp_path / "out"
    hp.main(["--app-dir", str(app), "--run-url", RUN_URL, "--reference", REF,
             "--out-dir", str(out), "--credentials", "failure"])
    assert (out / "outcome").read_text() == "infra"
    assert REF in json.loads((out / "comment.json").read_text())["body"]


def test_lambda_client_config():
    publisher = hp.LambdaPublisher()
    config = publisher.client.meta.config
    assert config.read_timeout == 360
    assert config.retries["total_max_attempts"] == 1
    assert publisher.client.meta.region_name == "eu-west-2"


def throttle():
    return ClientError({"Error": {"Code": "TooManyRequestsException", "Message": "Rate"}}, "Invoke")


def test_throttle_is_retried_then_succeeds(app):
    client = FakeClient(json.dumps(SUCCESS).encode(), failures=[throttle(), throttle()])
    run, _ = make_run(app, client)
    assert run.execute(hp.CheckState()).kind == "success"
    assert len(client.calls) == 3


def test_throttle_exhausted_is_infra(app):
    client = FakeClient(failures=[throttle(), throttle(), throttle(), throttle()])
    run, _ = make_run(app, client)
    assert run.execute(hp.CheckState()).kind == "infra"
    assert len(client.calls) == hp.THROTTLE_ATTEMPTS


def test_throttle_backoff_doubles():
    delays = []
    client = FakeClient(json.dumps(SUCCESS).encode(), failures=[throttle(), throttle()])
    hp.LambdaPublisher(client=client, sleep=delays.append).publish({"x": 1})
    assert delays == [2.0, 4.0]


def test_other_client_errors_are_not_retried(app):
    error = ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "x"}}, "Invoke")
    client = FakeClient(failures=[error])
    run, _ = make_run(app, client)
    assert run.execute(hp.CheckState()).kind == "infra"
    assert len(client.calls) == 1


def test_cli_accepts_values_starting_with_dashes(app, tmp_path):
    out = tmp_path / "out"
    hp.main([f"--app-dir={app}", f"--run-url={RUN_URL}", f"--reference={REF}",
             f"--out-dir={out}", "--check-result=failure", "--check-verdict=--help"])
    assert (out / "outcome").read_text() == "infra"


# ---- warnings on a published app ----

WARNINGS = (
    "scripts/check.py\n WARN owner_email is still the template's placeholder (you@example.com)\n\n"
    "scripts/routing_check.py\n FAIL days.until: \"how long\" is one ordinary word\n"
    "1 problem(s) that will stop your app being chosen."
)


def test_success_comment_has_no_warnings_section_without_warnings(app):
    run, _ = make_run(app, FakeClient(json.dumps(SUCCESS).encode()))
    assert "Warnings" not in run.execute(hp.CheckState()).comment_payload()["body"]


def test_success_comment_carries_warnings(app):
    run, _ = make_run(app, FakeClient(json.dumps(SUCCESS).encode()))
    run.factory = hp.OutcomeFactory(RUN_URL, REF, WARNINGS)
    outcome = run.execute(hp.CheckState())
    body = outcome.comment_payload()["body"]
    assert body.startswith("Your app Days Until (hack.x v0.1.0) is live.")
    assert "\n\nWarnings (they did not stop the publish; fix them and push again):\n\n```text\n" in body
    assert "owner_email is still the template's placeholder" in body
    assert "1 problem(s) that will stop your app being chosen." in body
    assert outcome.status_payload()["description"] == "Live: hack.x v0.1.0"
    assert outcome.state == "success"


def test_warnings_are_cut(app):
    published = hp.Published(RUN_URL, SUCCESS, "w" * 10000)
    assert len(published.warnings) == hp.WARNINGS_LIMIT


def test_code_error_comment_ignores_warnings(app):
    reply = {"statusCode": 400, "ok": False, "error_class": "code", "error": "bad id"}
    run, _ = make_run(app, FakeClient(json.dumps(reply).encode()))
    run.factory = hp.OutcomeFactory(RUN_URL, REF, WARNINGS)
    assert "Warnings" not in run.execute(hp.CheckState()).comment_payload()["body"]


def test_main_reads_the_warnings_file(app, tmp_path, monkeypatch):
    warnings_file = tmp_path / "warnings.txt"
    warnings_file.write_text(WARNINGS)
    out = tmp_path / "out"
    real_publisher = hp.LambdaPublisher
    monkeypatch.setattr(hp, "LambdaPublisher", lambda: real_publisher(
        client=FakeClient(json.dumps(SUCCESS).encode()), sleep=lambda _: None))
    monkeypatch.setattr(hp, "ActionsTokenProvider", FakeToken)
    assert hp.main(["--app-dir", str(app), "--run-url", RUN_URL, "--reference", REF,
                    "--out-dir", str(out), "--check-warnings-file", str(warnings_file)]) == 0
    assert (out / "outcome").read_text() == "success"
    assert "owner_email is still" in json.loads((out / "comment.json").read_text())["body"]
