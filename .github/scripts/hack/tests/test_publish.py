"""Unit tests for publish.py; boto3 and the GitHub token endpoint are faked."""

import base64
import importlib.util
import io
import json
import os
import re
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
PUSHER = "tide-watcher"


@pytest.fixture(autouse=True)
def started_by_the_pusher(monkeypatch):
    """As in a first run: the account that pushed started it."""
    monkeypatch.setenv("GITHUB_ACTOR", PUSHER)
    monkeypatch.setenv("GITHUB_TRIGGERING_ACTOR", PUSHER)


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
        # A new value per request, as GitHub's: "jwt", then "jwt-2", "jwt-3", ...
        return "jwt" if len(self.audiences) == 1 else f"jwt-{len(self.audiences)}"


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


def test_package_leaves_out_the_claude_code_settings(app):
    """The templates ship .claude/settings.json: for building the app, never part of it."""
    (app / ".claude").mkdir()
    (app / ".claude" / "settings.json").write_text('{"permissions": {}}')
    (app / ".claude" / "commands").mkdir()
    (app / ".claude" / "commands" / "publish.md").write_text("x")
    (app / "lib" / ".claude").mkdir(parents=True)
    (app / "lib" / ".claude" / "settings.local.json").write_text("{}")
    files = hp.Packager(app).build().files
    assert not [name for name in files if ".claude" in name.split("/")]
    assert {"app.py", "manifest.yaml"} <= set(files)


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


#: The message when the reply does not say what changed (a publisher or runtime from before
#: `change`): it works for a first install and for an update.
EITHER_MESSAGE = (
    "**Days Until 0.1.0 is published** as a private draft: only you and your testers can get it until "
    "Pippa Labs approves it.\n\n"
    "#### Try it in Pippa\n\n"
    "1. Open [app.getpippa.ai/apps](https://app.getpippa.ai/apps) and choose **Created by me**.\n"
    "2. Press **Install v0.1.0** if your app isn't installed yet, or **Update to latest** if an older "
    "version is. With **Always use my latest version** on, you have it already.\n"
    "3. Ask Pippa something your app does. The reply should carry the badge **via Days Until 0.1.0**.\n\n"
    "#### When it's ready\n\n"
    "- **Let someone try it:** press **Share for testing** on the card and add the email address they "
    "use for Pippa.\n"
    "- **Make it public:** under **Versions** on the card, press **Submit for review** next to 0.1.0. "
    "Pippa Labs reviews it, and once it's approved anyone can install it.\n\n"
    "Can't see your app? Reload the Apps page: it loads the list only once."
)
DRAFT_STATUS = "Published as a private draft: hack.x v0.1.0"


def test_success_payloads(app):
    run, _ = make_run(app, FakeClient(json.dumps(SUCCESS).encode()))
    outcome = run.execute(hp.CheckState())
    assert outcome.kind == "success"
    assert outcome.status_payload() == {
        "state": "success", "context": "pippa/publish",
        "description": DRAFT_STATUS, "target_url": RUN_URL,
    }
    assert outcome.comment_payload() == {"body": EITHER_MESSAGE}
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


OLD_WORKFLOW_SENTENCE = ("This run was made by an older version of the Pippa publish workflow, which can no "
                         "longer publish.")


@pytest.mark.parametrize("verdict", ["", "passed"])
def test_the_old_workflows_failed_check_is_refused_not_a_platform_error(app, verdict):
    """A re-run of a run made before the check was split: its check job can no longer pass."""
    run, client = make_run(app)
    outcome = run.execute(hp.CheckState(result="failure", verdict=verdict, old_workflow=True))
    assert outcome.kind == "refused" and outcome.reason == "old_workflow"
    body = outcome.comment_payload()["body"]
    assert body.startswith(f"REFUSED: the app was not published:\n\n```text\n{OLD_WORKFLOW_SENTENCE}\n```")
    assert "Push a new commit to main to publish your app." in body
    assert "PLATFORM_ERROR" not in body and "CODE_ERROR" not in body
    assert outcome.status_payload()["description"] == f"Refused: {OLD_WORKFLOW_SENTENCE}"
    assert client.calls == []


def test_the_old_workflows_refusal_by_prepare_stays_a_code_error(app):
    run, client = make_run(app)
    outcome = run.execute(hp.CheckState(result="failure", verdict="failed", details=" FAIL x", old_workflow=True))
    assert outcome.kind == "code"
    assert client.calls == []


def test_the_old_workflow_still_publishes_a_passed_check(app):
    """Re-running only the publish job keeps the first attempt's passed check; the publisher decides."""
    run, client = make_run(app, FakeClient(json.dumps(SUCCESS).encode()))
    assert run.execute(hp.CheckState(old_workflow=True)).kind == "success"
    assert len(client.calls) == 1


def test_main_reports_the_old_workflows_failed_check_as_refused(app, tmp_path):
    out = tmp_path / "out"
    assert hp.main(["--app-dir", str(app), "--run-url", RUN_URL, "--reference", REF, "--out-dir", str(out),
                    "--check-result", "failure", "--check-verdict", "", "--old-workflow"]) == 0
    assert (out / "outcome").read_text() == "refused"
    assert "(Reason: old_workflow)" in json.loads((out / "comment.json").read_text())["body"]


def test_main_without_old_workflow_keeps_a_failed_check_without_verdict_infra(app, tmp_path):
    out = tmp_path / "out"
    hp.main(["--app-dir", str(app), "--run-url", RUN_URL, "--reference", REF, "--out-dir", str(out),
             "--check-result", "failure", "--check-verdict", ""])
    assert (out / "outcome").read_text() == "infra"


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


# ---- manifest.yaml is read only as a regular file of the repository ----
#
# The publish job holds the role's credentials and can request an OIDC token, and a YAML error
# quotes the line it stopped at in the commit comment, so a link must never be followed.

LINK_TARGET = "token: [the-line-of-the-link-target\n"


def link_manifest(app, target):
    (app / "manifest.yaml").unlink()
    os.symlink(target, app / "manifest.yaml")


def test_a_linked_manifest_is_refused_without_reading_its_target(app, tmp_path):
    target = tmp_path / "elsewhere.yaml"
    target.write_text(LINK_TARGET)
    link_manifest(app, target)
    with pytest.raises(hp.CodeError) as err:
        hp.load_manifest(app)
    assert str(err.value) == "ERROR: manifest.yaml must be a regular file in the repository, not a link or a folder."
    assert "the-line-of-the-link-target" not in str(err.value)


def test_a_dangling_link_is_refused_as_a_link(app, tmp_path):
    link_manifest(app, tmp_path / "missing.yaml")
    with pytest.raises(hp.CodeError, match="not a link or a folder"):
        hp.load_manifest(app)


def test_a_folder_named_manifest_yaml_is_refused(app):
    (app / "manifest.yaml").unlink()
    (app / "manifest.yaml").mkdir()
    with pytest.raises(hp.CodeError, match="not a link or a folder"):
        hp.load_manifest(app)


def test_a_link_that_appears_after_the_first_look_is_still_not_followed(app, tmp_path, monkeypatch):
    target = tmp_path / "elsewhere.yaml"
    target.write_text(LINK_TARGET)
    link_manifest(app, target)
    monkeypatch.setattr(hp.Path, "is_symlink", lambda self: False)
    with pytest.raises(hp.CodeError) as err:
        hp.load_manifest(app)
    assert str(err.value).startswith("ERROR: manifest.yaml could not be read:")
    assert "the-line-of-the-link-target" not in str(err.value)


def test_a_regular_manifest_is_still_read(app):
    assert hp.load_manifest(app)["app"]["id"] == "hack.x"


def test_a_linked_manifest_never_reaches_the_publisher(app, tmp_path):
    target = tmp_path / "elsewhere.yaml"
    target.write_text(MANIFEST)
    link_manifest(app, target)
    token = FakeToken()
    run, client = make_run(app, token=token)
    assert run.execute(hp.CheckState()).kind == "code"
    assert client.calls == [] and token.audiences == []


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
    hp.LambdaPublisher(client=client, sleep=delays.append).publish(lambda: {"x": 1})
    assert delays == [2.0, 4.0]


def test_other_client_errors_are_not_retried(app):
    error = ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "x"}}, "Invoke")
    client = FakeClient(failures=[error])
    run, _ = make_run(app, client)
    assert run.execute(hp.CheckState()).kind == "infra"
    assert len(client.calls) == 1


class Recorder:
    """Stands in for the token provider and the publisher; records that either was made at all."""

    made = []

    def __init__(self, *args, **kwargs):
        Recorder.made.append(type(self).__name__)

    def token(self, audience):
        raise AssertionError("a token was requested")


class TokenRecorder(Recorder, hp.TokenProvider):
    pass


class PublisherRecorder(Recorder):
    def publish(self, make_event):
        raise AssertionError("the publisher was invoked")


@pytest.fixture
def recorded(monkeypatch):
    Recorder.made = []
    monkeypatch.setattr(hp, "ActionsTokenProvider", TokenRecorder)
    monkeypatch.setattr(hp, "LambdaPublisher", PublisherRecorder)
    return Recorder.made


@pytest.mark.parametrize("actor, triggering", [
    (PUSHER, "org-writer"),
    (PUSHER, "Tide-Watcher"),
    (PUSHER, ""),
    ("", PUSHER),
    (None, None),
])
def test_a_run_the_pusher_did_not_start_requests_no_token_and_writes_no_report(
        app, tmp_path, monkeypatch, caplog, recorded, actor, triggering):
    for name, value in (("GITHUB_ACTOR", actor), ("GITHUB_TRIGGERING_ACTOR", triggering)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    out = tmp_path / "out"
    rc = hp.main(["--app-dir", str(app), "--run-url", RUN_URL, "--reference", REF, "--out-dir", str(out)])
    assert rc == hp.RERUN_REFUSED_EXIT == 77
    assert recorded == []
    assert not out.exists() or list(out.iterdir()) == []
    assert "Not published:" in caplog.text


def test_a_rerun_by_another_account_names_both_accounts(app, tmp_path, monkeypatch, caplog, recorded):
    monkeypatch.setenv("GITHUB_TRIGGERING_ACTOR", "org-writer")
    hp.main(["--app-dir", str(app), "--run-url", RUN_URL, "--reference", REF, "--out-dir", str(tmp_path / "out")])
    assert ("this re-run was started by org-writer, not by tide-watcher, who pushed; only the account that "
            "pushed can re-run a publish") in caplog.text


def test_a_rerun_by_another_account_reports_not_even_a_failed_check(app, tmp_path, monkeypatch, recorded):
    monkeypatch.setenv("GITHUB_TRIGGERING_ACTOR", "org-writer")
    details = tmp_path / "details.txt"
    details.write_text(" FAIL the tests fail")
    out = tmp_path / "out"
    rc = hp.main(["--app-dir", str(app), "--run-url", RUN_URL, "--reference", REF, "--out-dir", str(out),
                  "--check-result", "failure", "--check-verdict", "failed", "--check-details-file", str(details)])
    assert rc == hp.RERUN_REFUSED_EXIT
    assert not out.exists() or list(out.iterdir()) == []


def test_account_names_reach_the_log_as_plain_text(app, tmp_path, monkeypatch, caplog, recorded):
    monkeypatch.setenv("GITHUB_TRIGGERING_ACTOR", "x\n::error::forged" + "y" * 200)
    hp.main(["--app-dir", str(app), "--run-url", RUN_URL, "--reference", REF, "--out-dir", str(tmp_path / "out")])
    message = next(record.getMessage() for record in caplog.records if "Not published" in record.getMessage())
    assert "\n" not in message and "::" not in message
    assert "x???error??forged" in message and "y" * 100 not in message


def test_the_pushers_own_run_goes_on_to_publish(app, tmp_path, recorded):
    out = tmp_path / "out"
    rc = hp.main(["--app-dir", str(app), "--run-url", RUN_URL, "--reference", REF, "--out-dir", str(out),
                  "--credentials", "failure"])
    assert rc == 0
    assert (out / "outcome").read_text() == "infra"
    assert recorded == ["TokenRecorder"], "built, though the missing credentials stop it before any request"


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
#: How the warnings section starts; the warnings follow it in a fence, and nothing follows them.
WARNINGS_TITLE = "#### Warnings\n\nThese didn't stop the publish. Fix them and push again.\n\n"


def test_success_comment_has_no_warnings_section_without_warnings(app):
    run, _ = make_run(app, FakeClient(json.dumps(SUCCESS).encode()))
    assert "Warnings" not in run.execute(hp.CheckState()).comment_payload()["body"]


def test_success_comment_carries_warnings(app):
    run, _ = make_run(app, FakeClient(json.dumps(SUCCESS).encode()))
    run.factory = hp.OutcomeFactory(RUN_URL, REF, WARNINGS)
    outcome = run.execute(hp.CheckState())
    body = outcome.comment_payload()["body"]
    assert body.startswith(f"{EITHER_MESSAGE}\n\n#### Warnings\n\n")
    assert body.endswith(f"{WARNINGS_TITLE}{hp.fence(WARNINGS)}")
    assert "owner_email is still the template's placeholder" in body
    assert "1 problem(s) that will stop your app being chosen." in body
    assert outcome.status_payload()["description"] == DRAFT_STATUS
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


# ---- refusals: the publisher says no before deploying, for a reason outside the code ----

NOT_LINKED = (
    "This repository isn't linked to a Pippa builder account, so it can't publish. Apps are published "
    "only from the repository Pippa Labs created for you: a fork or a copy can't publish. If you think "
    "this is wrong, ask an organiser."
)


def refusal_reply(error=NOT_LINKED, **extra):
    return {"statusCode": 403, "ok": False, "error_class": "code", "error_kind": "refused",
            "error": error, **extra}


def test_publisher_refusal_is_refused_not_a_code_error(app, capsys):
    run, client = make_run(app, FakeClient(json.dumps(refusal_reply()).encode()))
    outcome = run.execute(hp.CheckState())
    assert len(client.calls) == 1
    assert outcome.kind == "refused"
    body = outcome.comment_payload()["body"]
    assert body.startswith("REFUSED: the app was not published:\n\n```text\nThis repository isn't linked")
    assert "CODE_ERROR" not in body and "problem in its code" not in body
    assert "Nothing was published, and every version published before is unchanged. This is not " in body
    assert "a problem in the app's code" in body
    assert body.endswith(f"Run log: {RUN_URL}")
    status = outcome.status_payload()
    assert status["state"] == "failure" and status["context"] == "pippa/publish"
    assert status["description"].startswith("Refused: This repository isn't linked")
    assert len(status["description"]) <= hp.STATUS_DESCRIPTION_LIMIT and status["description"].endswith("...")
    assert "REFUSED: the app was not published:" in capsys.readouterr().out


def test_refused_error_class_alone_is_a_refusal(app):
    reply = {"statusCode": 403, "ok": False, "error_class": "refused", "error": "Remove `owner_email`."}
    run, _ = make_run(app, FakeClient(json.dumps(reply).encode()))
    outcome = run.execute(hp.CheckState())
    assert outcome.kind == "refused"
    assert outcome.status_payload()["description"] == "Refused: Remove `owner_email`."


def test_code_error_without_refused_kind_stays_a_code_error(app):
    reply = {"statusCode": 403, "ok": False, "error_class": "code", "error_kind": "something-else",
             "error": "your app did not return a usable result when called"}
    run, _ = make_run(app, FakeClient(json.dumps(reply).encode()))
    assert run.execute(hp.CheckState()).kind == "code"


def test_infra_with_refused_kind_stays_infra(app):
    reply = {"statusCode": 500, "ok": False, "error_class": "infra", "error_kind": "refused",
             "error": "x", "reference": "pub-1"}
    run, _ = make_run(app, FakeClient(json.dumps(reply).encode()))
    assert run.execute(hp.CheckState()).kind == "infra"


def test_refusal_without_a_sentence_says_so(app):
    run, _ = make_run(app, FakeClient(json.dumps(refusal_reply(error="")).encode()))
    outcome = run.execute(hp.CheckState())
    assert outcome.kind == "refused"
    assert "without giving a reason. Ask an organiser." in outcome.comment_payload()["body"]


def test_refusal_text_is_fenced_so_manifest_names_are_not_markdown(app):
    detail = ('Another builder already uses the name "@someone [x](https://example.com/y)". '
              "Choose a different app_author_name.")
    run, _ = make_run(app, FakeClient(json.dumps(refusal_reply(error=detail)).encode()))
    body = run.execute(hp.CheckState()).comment_payload()["body"]
    fenced = body.split("```text\n", 1)[1].split("\n```", 1)[0]
    assert fenced == detail
    assert body.count("@someone") == 1


def test_refusal_ignores_the_check_warnings(app):
    run, _ = make_run(app, FakeClient(json.dumps(refusal_reply()).encode()))
    run.factory = hp.OutcomeFactory(RUN_URL, REF, WARNINGS)
    assert "Warnings" not in run.execute(hp.CheckState()).comment_payload()["body"]


def test_refusal_details_are_cut():
    outcome = hp.Refusal(RUN_URL, "z" * 20000)
    assert len(outcome.detail) == hp.DETAILS_LIMIT


def test_main_writes_a_refused_outcome(app, tmp_path, monkeypatch):
    out = tmp_path / "out"
    real_publisher = hp.LambdaPublisher
    monkeypatch.setattr(hp, "LambdaPublisher", lambda: real_publisher(
        client=FakeClient(json.dumps(refusal_reply()).encode()), sleep=lambda _: None))
    monkeypatch.setattr(hp, "ActionsTokenProvider", FakeToken)
    assert hp.main(["--app-dir", str(app), "--run-url", RUN_URL, "--reference", REF,
                    "--out-dir", str(out)]) == 0
    assert (out / "outcome").read_text() == "refused"
    assert json.loads((out / "comment.json").read_text())["body"].startswith("REFUSED")
    assert json.loads((out / "status.json").read_text())["description"].startswith("Refused: ")


# ---- Pippa's own warnings on a published app ----

IGNORED_OWNER = ("owner_email in manifest.yaml is ignored: Pippa knows who owns this app from your "
                 "repository. Remove it.")
LEGACY_PUBLISHER = ("Add app_author_name to manifest.yaml: the public name shown with your app. Until "
                    "then it shows `publisher`.")


def test_success_lists_the_runtime_warnings_before_the_build_warnings(app, capsys):
    reply = {**SUCCESS, "warnings": [IGNORED_OWNER, LEGACY_PUBLISHER]}
    run, _ = make_run(app, FakeClient(json.dumps(reply).encode()))
    run.factory = hp.OutcomeFactory(RUN_URL, REF, WARNINGS)
    outcome = run.execute(hp.CheckState())
    assert outcome.kind == "success" and outcome.state == "success"
    body = outcome.comment_payload()["body"]
    block = body.split("```text\n", 1)[1].split("\n```", 1)[0]
    assert block.startswith(f"Pippa\n WARN {IGNORED_OWNER}\n WARN {LEGACY_PUBLISHER}\n\nscripts/check.py\n")
    assert "1 problem(s) that will stop your app being chosen." in block
    assert outcome.status_payload()["description"] == DRAFT_STATUS
    assert f"Warning  {IGNORED_OWNER}" in capsys.readouterr().out


def test_runtime_warnings_alone_make_a_warnings_section(app):
    reply = {**SUCCESS, "warnings": [LEGACY_PUBLISHER]}
    run, _ = make_run(app, FakeClient(json.dumps(reply).encode()))
    body = run.execute(hp.CheckState()).comment_payload()["body"]
    assert body.endswith(f"\n\n{WARNINGS_TITLE}```text\nPippa\n WARN {LEGACY_PUBLISHER}\n```")


@pytest.mark.parametrize("warnings", [None, "one sentence", {"a": 1}, [], [1, None, "", "   "]])
def test_missing_or_odd_runtime_warnings_add_nothing(app, warnings):
    reply = {**SUCCESS, "warnings": warnings}
    run, _ = make_run(app, FakeClient(json.dumps(reply).encode()))
    outcome = run.execute(hp.CheckState())
    assert outcome.runtime_warnings == []
    assert "Warnings" not in outcome.comment_payload()["body"]


def test_runtime_warnings_are_one_line_each_counted_and_cut():
    warnings = ["  two\nlines  ", "x" * 1000] + [f"warning {n}" for n in range(20)]
    published = hp.Published(RUN_URL, {**SUCCESS, "warnings": warnings})
    assert len(published.runtime_warnings) == hp.RUNTIME_WARNINGS_COUNT
    assert published.runtime_warnings[0] == "two lines"
    assert len(published.runtime_warnings[1]) == hp.RUNTIME_WARNING_LIMIT


# ---- every attempt carries its own token: the publisher refuses a token it has seen ----

def test_a_throttled_invoke_is_retried_with_a_fresh_token(app):
    client = FakeClient(json.dumps(SUCCESS).encode(), failures=[throttle(), throttle()])
    token = FakeToken()
    run, _ = make_run(app, client, token)
    assert run.execute(hp.CheckState()).kind == "success"
    tokens = [json.loads(call["Payload"])["token"] for call in client.calls]
    assert tokens == ["jwt", "jwt-2", "jwt-3"]
    assert token.audiences == ["pippa-hackathon"] * 3
    events = [json.loads(call["Payload"]) for call in client.calls]
    assert all(event["code_zip_b64"] == events[0]["code_zip_b64"] for event in events)


def test_a_failure_that_is_not_retried_requests_one_token(app):
    error = ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "x"}}, "Invoke")
    token = FakeToken()
    run, client = make_run(app, FakeClient(failures=[error]), token)
    assert run.execute(hp.CheckState()).kind == "infra"
    assert len(token.audiences) == 1 and len(client.calls) == 1


def test_a_token_failure_on_a_retry_is_a_platform_error(app):
    class FailsSecond(FakeToken):
        def token(self, audience):
            if self.audiences:
                self.audiences.append(audience)
                raise hp.PlatformError("token endpoint down")
            return super().token(audience)

    client = FakeClient(json.dumps(SUCCESS).encode(), failures=[throttle()])
    run, _ = make_run(app, client, FailsSecond())
    assert run.execute(hp.CheckState()).kind == "infra"
    assert len(client.calls) == 1


def test_the_publisher_never_reuses_an_event():
    built = []

    def make_event():
        built.append({"token": f"t{len(built)}"})
        return built[-1]

    client = FakeClient(json.dumps(SUCCESS).encode(), failures=[throttle()])
    hp.LambdaPublisher(client=client, sleep=lambda _: None).publish(make_event)
    assert [json.loads(call["Payload"]) for call in client.calls] == [{"token": "t0"}, {"token": "t1"}]


# ---- refusals of the run itself: who pushed, a replayed token, not a push ----

ACTOR_SENTENCE = ("Only the builder this repository belongs to can publish it. "
                  "This push was made by tide-watcher-2.")


@pytest.mark.parametrize("reason", sorted(hp.IDENTITY_REFUSALS))
def test_identity_refusals_are_refusals_with_what_to_do(app, reason):
    reply = {"statusCode": 403, "ok": False, "error_class": "code", "error": "Pippa's own sentence.",
             "reason": reason}
    run, _ = make_run(app, FakeClient(json.dumps(reply).encode()))
    outcome = run.execute(hp.CheckState())
    assert outcome.kind == "refused" and outcome.reason == reason
    body = outcome.comment_payload()["body"]
    assert body.startswith("REFUSED: the app was not published:\n\n```text\nPippa's own sentence.\n```")
    assert hp.IDENTITY_REFUSALS[reason].next_step in body
    assert f"(Reason: {reason})" in body
    assert "CODE_ERROR" not in body and "problem in its code" not in body
    assert "This is not a problem in the app's code." in body
    assert outcome.status_payload()["description"] == "Refused: Pippa's own sentence."


@pytest.mark.parametrize("reason", sorted(hp.IDENTITY_REFUSALS))
def test_an_identity_refusal_without_a_sentence_uses_the_built_in_one(app, reason):
    reply = {"statusCode": 409, "ok": False, "error_class": "code", "error_kind": "refused", "error": "",
             "error_reason": reason}
    run, _ = make_run(app, FakeClient(json.dumps(reply).encode()))
    outcome = run.execute(hp.CheckState())
    assert outcome.kind == "refused"
    assert outcome.detail == hp.IDENTITY_REFUSALS[reason].sentence
    assert "without giving a reason" not in outcome.comment_payload()["body"]


def test_the_actor_refusal_is_recognised_by_its_sentence_alone(app):
    reply = {"statusCode": 403, "ok": False, "error_class": "code", "error": ACTOR_SENTENCE}
    run, _ = make_run(app, FakeClient(json.dumps(reply).encode()))
    outcome = run.execute(hp.CheckState())
    assert outcome.kind == "refused" and outcome.reason == "actor_not_builder"
    body = outcome.comment_payload()["body"]
    assert f"```text\n{ACTOR_SENTENCE}\n```" in body
    assert "Push from the GitHub account Pippa Labs invited to this repository." in body


#: The runtime's sentences as the publisher relays them in `error` (it sends no `reason`).
RUNTIME_SENTENCES = {
    "token_replayed": ("This publish used a GitHub sign-in token that had already been used, so it was refused. "
                       "Each run can publish once: push again to publish."),
    "builder_identity_unknown": ("Pippa doesn't know which GitHub account this repository belongs to yet, so it "
                                 "can't check who pushed."),
    "actor_not_builder": ACTOR_SENTENCE,
    "not_a_push": "Only a push to main publishes; this run was workflow_dispatch.",
}


@pytest.mark.parametrize("reason", sorted(RUNTIME_SENTENCES))
@pytest.mark.parametrize("error_kind", [None, "refused"])
def test_every_identity_refusal_is_recognised_by_the_runtimes_sentence_alone(app, reason, error_kind):
    reply = {"statusCode": 403, "ok": False, "error_class": "code", "error": RUNTIME_SENTENCES[reason]}
    if error_kind:
        reply["error_kind"] = error_kind
    run, _ = make_run(app, FakeClient(json.dumps(reply).encode()))
    outcome = run.execute(hp.CheckState())
    assert outcome.kind == "refused" and outcome.reason == reason
    body = outcome.comment_payload()["body"]
    assert f"```text\n{RUNTIME_SENTENCES[reason]}\n```" in body
    assert hp.IDENTITY_REFUSALS[reason].next_step in body


def test_a_curly_apostrophe_in_the_sentence_still_names_the_refusal():
    sentence = RUNTIME_SENTENCES["builder_identity_unknown"].replace("'", "\N{RIGHT SINGLE QUOTATION MARK}")
    assert hp.identity_reason({"error": sentence}) == "builder_identity_unknown"


def test_a_sentence_that_only_mentions_a_token_is_not_an_identity_refusal():
    assert hp.identity_reason({"error": "The GitHub sign-in token could not be verified."}) == ""


def test_the_publishers_own_event_refusal_is_recognised_by_its_sentence(app):
    reply = {"statusCode": 403, "ok": False, "error_class": "code",
             "error": "Only a push to main publishes; this run was workflow_dispatch"}
    run, _ = make_run(app, FakeClient(json.dumps(reply).encode()))
    outcome = run.execute(hp.CheckState())
    assert outcome.kind == "refused" and outcome.reason == "not_a_push"
    assert "Push a commit to main to publish." in outcome.comment_payload()["body"]


#: The publisher's own refusals of the run, as it answers them (hackathon/publisher/handler.py): its
#: sentence, `error_kind` (none for the visibility refusal, a plain `Refused`) and `reason`.
PUBLISHER_REFUSALS = {
    "stale_run": ("This workflow run is older than the one that published the live version of this app, so it "
                  "cannot replace it. Re-running an old run never publishes; push a new commit to main instead.",
                  "refused"),
    "rerun_untagged": ("This is a re-run of an earlier workflow run, and Pippa cannot tell whether it is older than "
                       "the live version of this app, so it cannot replace it. Push a new commit to main instead.",
                       "refused"),
    "visibility_not_allowed": ("this repository is not private, so it cannot publish through the private pipeline; "
                               "restore .github/workflows/publish.yml from the public template", None),
    "concurrent_publish": ("Another publish of this app is running at the same time, so this one stopped and "
                           "published nothing. Wait for that run to finish, then push again.", "refused"),
}
#: What each of them tells the builder to do next.
PUBLISHER_NEXT_STEPS = {
    "stale_run": "Push a new commit to main to publish your latest code.",
    "rerun_untagged": "Push a new commit to main to publish. Re-running this run will not help;",
    "visibility_not_allowed": "Restore .github/workflows/publish.yml from the public app template",
    "concurrent_publish": "Wait for the other publish of this app to finish",
}


def publisher_refusal(reason, with_reason=True):
    sentence, kind = PUBLISHER_REFUSALS[reason]
    reply = {"statusCode": 403, "ok": False, "error_class": "code", "error": sentence}
    if kind:
        reply["error_kind"] = kind
    if with_reason:
        reply["reason"] = reason
    return reply


@pytest.mark.parametrize("reason", sorted(PUBLISHER_REFUSALS))
def test_the_publishers_own_refusals_of_the_run_name_the_next_step(app, reason):
    run, _ = make_run(app, FakeClient(json.dumps(publisher_refusal(reason)).encode()))
    outcome = run.execute(hp.CheckState())
    assert outcome.kind == "refused" and outcome.reason == reason
    body = outcome.comment_payload()["body"]
    assert f"```text\n{PUBLISHER_REFUSALS[reason][0]}\n```" in body
    assert PUBLISHER_NEXT_STEPS[reason] in body
    assert f"(Reason: {reason})" in body
    assert hp.REFUSED_NEXT_STEP not in body and "CODE_ERROR" not in body


@pytest.mark.parametrize("reason", sorted(PUBLISHER_REFUSALS))
def test_the_publishers_own_refusals_are_recognised_by_their_sentence_alone(app, reason):
    """A publisher from before it sent these reasons (concurrent_publish came without one)."""
    reply = publisher_refusal(reason, with_reason=False)
    assert hp.identity_reason(reply) == reason
    run, _ = make_run(app, FakeClient(json.dumps(reply).encode()))
    outcome = run.execute(hp.CheckState())
    assert outcome.kind == "refused" and outcome.reason == reason
    assert PUBLISHER_NEXT_STEPS[reason] in outcome.comment_payload()["body"]


@pytest.mark.parametrize("reason", sorted(PUBLISHER_REFUSALS))
def test_the_built_in_sentence_is_the_publishers(reason):
    assert hp.IDENTITY_REFUSALS[reason].sentence == PUBLISHER_REFUSALS[reason][0]


def test_an_untagged_re_run_from_an_older_publisher_still_says_push_a_new_commit(app):
    """A publisher that predates rerun_untagged sends stale_run with the untagged sentence."""
    reply = {**publisher_refusal("rerun_untagged"), "reason": "stale_run"}
    run, _ = make_run(app, FakeClient(json.dumps(reply).encode()))
    outcome = run.execute(hp.CheckState())
    assert outcome.reason == "stale_run"
    assert "Push a new commit to main" in outcome.comment_payload()["body"]


@pytest.mark.parametrize("reply", [
    {"statusCode": 500, "ok": False, "error_class": "infra", "error": ACTOR_SENTENCE, "reason": "actor_not_builder",
     "reference": "pub-1"},
    {"statusCode": 403, "ok": False, "error": ACTOR_SENTENCE, "reason": "token_replayed"},
])
def test_an_identity_reason_never_turns_an_infra_or_unclassified_reply_into_a_refusal(app, reply):
    run, _ = make_run(app, FakeClient(json.dumps(reply).encode()))
    assert run.execute(hp.CheckState()).kind == "infra"


@pytest.mark.parametrize("reason", ["something_else", "", None, 5, ["actor_not_builder"]])
def test_unknown_reasons_keep_the_old_classification(app, reason):
    reply = {"statusCode": 400, "ok": False, "error_class": "code", "error": "bad id", "reason": reason}
    run, _ = make_run(app, FakeClient(json.dumps(reply).encode()))
    assert run.execute(hp.CheckState()).kind == "code"


def test_a_repository_refusal_keeps_the_generic_next_step(app):
    run, _ = make_run(app, FakeClient(json.dumps(refusal_reply()).encode()))
    outcome = run.execute(hp.CheckState())
    assert outcome.reason == ""
    assert hp.REFUSED_NEXT_STEP in outcome.comment_payload()["body"]
    assert "(Reason:" not in outcome.comment_payload()["body"]


# ---- an app published without all of its API keys ----

KEYS_WARNING = "Published, but it can't run until you add OPENWEATHER_API_KEY in Created by me."
#: What the message says first, after the headline, while an API key is missing.
KEYS_SECTION = (
    "#### First, add its API keys\n\n"
    "Days Until can't run until you add them. Open [app.getpippa.ai/apps](https://app.getpippa.ai/apps), "
    "choose **Created by me**, and press **Add** next to each key under **Keys** on the app's card. No "
    "push is needed: the keys stay in Pippa, never in GitHub.\n\n"
    f"```text\n WARN {KEYS_WARNING}\n```"
)


def test_missing_api_keys_lead_the_message_apart_from_the_warnings(app, capsys):
    reply = {**SUCCESS, "warnings": [KEYS_WARNING, LEGACY_PUBLISHER]}
    run, _ = make_run(app, FakeClient(json.dumps(reply).encode()))
    run.factory = hp.OutcomeFactory(RUN_URL, REF, WARNINGS)
    outcome = run.execute(hp.CheckState())
    assert outcome.kind == "success" and outcome.state == "success"
    assert outcome.keys_missing == [KEYS_WARNING]
    body = outcome.comment_payload()["body"]
    headline, rest = body.split("\n\n", 1)
    assert headline == EITHER_MESSAGE.split("\n\n", 1)[0]
    assert rest.startswith(f"{KEYS_SECTION}\n\n#### Try it in Pippa\n\n")
    warnings = body.split(WARNINGS_TITLE, 1)[1]
    assert KEYS_WARNING not in warnings
    assert f"Pippa\n WARN {LEGACY_PUBLISHER}\n\nscripts/check.py" in warnings
    assert "it's live" not in body
    assert outcome.status_payload()["description"] == f"{DRAFT_STATUS}; add its API keys in Pippa"
    assert f"Warning  {KEYS_WARNING}" in capsys.readouterr().out


@pytest.mark.parametrize("sentence", [
    "Published, but it cannot run until you add A_KEY and B_KEY in Created by me.",
    "Published, but it can" + chr(0x2019) + "t run until you add A_KEY in Created by me.",
    "It CAN'T RUN UNTIL YOU ADD WEATHER_KEY.",
])
def test_missing_key_warnings_in_other_words(sentence):
    published = hp.Published(RUN_URL, {**SUCCESS, "warnings": [sentence]})
    assert published.keys_missing == [published.runtime_warnings[0]]
    assert published.all_warnings == ""


def test_only_missing_keys_make_no_warnings_section(app):
    reply = {**SUCCESS, "warnings": [KEYS_WARNING]}
    run, _ = make_run(app, FakeClient(json.dumps(reply).encode()))
    body = run.execute(hp.CheckState()).comment_payload()["body"]
    assert "#### Warnings" not in body
    assert body.count(KEYS_WARNING) == 1


def test_other_warnings_keep_the_usual_message():
    published = hp.Published(RUN_URL, {**SUCCESS, "warnings": ["Your app runs slowly until you add a cache."]})
    assert published.keys_missing == []
    assert published.message.startswith(f"{EITHER_MESSAGE}\n\n{WARNINGS_TITLE}")
    assert "add its API keys" not in published.message
    assert published.description == DRAFT_STATUS


# ---- the success message: how to reach this version in Pippa ----
#
# GitHub emails the comment to whoever pushed. What it says depends on the reply's `change` (the
# app's first version, a new version number, or a draft pushed again), `review_status` (approved:
# public at once, because review is switched off) and `live_version` (what everybody else gets).

GUIDE_URL = "https://github.com/Pippa-Labs/pippa-hack-x/blob/HEAD/docs/REVIEW_AND_SHARING.md"
GUIDE_LINE = f"More about versions, testers and review: [REVIEW_AND_SHARING.md in your repository]({GUIDE_URL})"
PHRASE_MANIFEST = {"outcomes": [{"id": "days.until", "example_phrases": ["how long until Friday?"]}]}


def published(manifest=PHRASE_MANIFEST, guide_url=GUIDE_URL, check_warnings="", **reply):
    """A version published with `reply` added to SUCCESS; `check_warnings` are the build's."""
    return hp.Published(RUN_URL, {**SUCCESS, **reply}, check_warnings, manifest, guide_url)


FIRST_MESSAGE = (
    "**Days Until is published.** Version 0.1.0 is in Pippa now as a private draft: only you can see it "
    "until Pippa Labs approves it.\n\n"
    "#### Try it in Pippa\n\n"
    "1. Open [app.getpippa.ai/apps](https://app.getpippa.ai/apps), signed in with the Pippa account your "
    "invitation was for.\n"
    "2. Choose **Created by me**. Your app has a card there.\n"
    "3. Press **Install v0.1.0**.\n"
    "4. Ask Pippa something your app does, for example `how long until Friday?`.\n\n"
    "When your app answers, the reply carries the badge **via Days Until 0.1.0**, so you know it was your "
    "app.\n\n"
    "While you're building, switch on **Always use my latest version** on the card: then every push is "
    "what answers you in chat.\n\n"
    "#### When it's ready\n\n"
    "- **Let someone try it:** press **Share for testing** on the card and add the email address they "
    "use for Pippa.\n"
    "- **Make it public:** under **Versions** on the card, press **Submit for review** next to 0.1.0. "
    "Pippa Labs reviews it, and once it's approved anyone can install it.\n\n"
    "Can't see your app? Reload the Apps page: it loads the list only once.\n\n"
    f"{GUIDE_LINE}"
)

NEW_VERSION_MESSAGE = (
    "**Days Until 0.2.0 is published** as a private draft: only you and your testers can get it. "
    "Everyone else keeps using 0.1.0, your live version, until 0.2.0 is approved.\n\n"
    "#### Try the new version\n\n"
    "You stay on the version you installed until you switch:\n\n"
    "1. Open [app.getpippa.ai/apps](https://app.getpippa.ai/apps) and choose **Created by me**.\n"
    "2. On your app's card, press **Update to latest**. If **Always use my latest version** is on, you "
    "have 0.2.0 already.\n"
    "3. Ask Pippa something your app does, for example `how long until Friday?`. The reply should carry "
    "the badge **via Days Until 0.2.0**.\n\n"
    "Your testers get 0.2.0 too, unless you chose a particular version for them under **Testers get**.\n\n"
    "If **Always use my latest version** is on and you changed the app's name, summary or example "
    "phrases, press **Uninstall** on the card, then **Install v0.2.0**: Pippa picks up that wording when "
    "the app is installed.\n\n"
    "#### When it's ready\n\n"
    "Under **Versions** on the card, press **Submit for review** next to 0.2.0. Once it's approved, it "
    "replaces 0.1.0 for everyone: people who have your app move to it the next time they ask Pippa. If "
    "0.2.0 adds something new your app can do, they get an **Update** button instead, and choose.\n\n"
    f"{GUIDE_LINE}"
)

SAME_VERSION_MESSAGE = (
    "**Days Until 0.2.0 is updated.** You pushed version 0.2.0 again while it's still a draft, so its "
    "code was replaced. Anyone on 0.2.0 gets the new code straight away, you included if it's installed "
    "for you.\n\n"
    "#### Try it\n\n"
    "Ask Pippa something your app does, for example `how long until Friday?`. The reply should carry the "
    "badge **via Days Until 0.2.0**. Not installed yet? Open "
    "[app.getpippa.ai/apps](https://app.getpippa.ai/apps), choose **Created by me** and press "
    "**Install v0.2.0**.\n\n"
    "Changed the app's name, summary or example phrases? Press **Uninstall** on the card, then "
    "**Install v0.2.0**: Pippa picks up that wording when the app is installed.\n\n"
    "#### When it's ready\n\n"
    "Under **Versions** on the card, press **Submit for review** next to 0.2.0. From then on 0.2.0 can't "
    "change: for anything after that, raise `version:` in `manifest.yaml` (to 0.2.1, say) and push.\n\n"
    f"{GUIDE_LINE}"
)


def test_the_first_version_says_how_to_find_install_and_try_it():
    message = published(change="first", review_status="draft", live_version=None).message
    assert message == FIRST_MESSAGE


def test_a_new_version_says_who_keeps_the_live_one_and_how_to_switch():
    message = published(change="new_version", review_status="draft", live_version="0.1.0", version="0.2.0").message
    assert message == NEW_VERSION_MESSAGE


def test_a_draft_pushed_again_says_its_code_was_replaced_in_place():
    message = published(change="same_version", review_status="draft", live_version="0.1.0", version="0.2.0").message
    assert message == SAME_VERSION_MESSAGE


def test_a_new_version_of_an_app_nobody_else_has_yet():
    message = published(change="new_version", review_status="draft", live_version=None, version="0.2.0").message
    assert message.startswith(
        "**Days Until 0.2.0 is published** as a private draft: only you and your testers can get it. Your "
        "app isn't public yet.\n\n#### Try the new version\n\n"
    )
    assert "your live version" not in message and "replaces" not in message
    assert ("#### When it's ready\n\nUnder **Versions** on the card, press **Submit for review** next to 0.2.0. "
            "Pippa Labs reviews it, and once it's approved anyone can install it.\n\n") in message


@pytest.mark.parametrize("live", [None, "", "   ", 5, ["0.1.0"], {"version": "0.1.0"}])
def test_a_live_version_that_is_missing_or_not_text_is_no_live_version(live):
    reply = {"change": "new_version", "review_status": "draft", "version": "0.2.0"}
    if live is not None:
        reply["live_version"] = live
    assert published(**reply).live_version == ""
    assert "Your app isn't public yet." in published(**reply).message


def test_the_live_version_is_text_from_the_reply_and_is_escaped():
    message = published(change="new_version", review_status="draft", version="0.2.0",
                        live_version="0.1.0 <b>@octo</b>").message
    assert "Everyone else keeps using 0.1.0 \\<b\\>`@`octo\\</b\\>, your live version" in message
    assert "<b>" not in message and "@octo" not in message


@pytest.mark.parametrize("change, headline", [
    ("first", "**Days Until 0.1.0 is published, and it's live:** anyone in Pippa can install it."),
    ("new_version", "**Days Until 0.1.0 is published, and it's live:** anyone in Pippa can install it."),
    ("", "**Days Until 0.1.0 is published, and it's live:** anyone in Pippa can install it."),
])
def test_a_version_public_at_once_says_it_is_live_and_skips_review(change, headline):
    """review_status approved: review is switched off on the runtime, so nothing waits for approval."""
    outcome = published(change=change, review_status="approved", live_version="0.1.0")
    message = outcome.message
    assert message.split("\n\n", 1)[0] == headline
    assert "#### When it's ready" not in message and "Submit for review" not in message
    assert "private draft" not in message and "Testers get" not in message
    assert message.endswith(GUIDE_LINE)
    assert outcome.description == "Live: hack.x v0.1.0"


def test_a_public_first_version_still_says_how_to_install_it():
    message = published(change="first", review_status="approved", live_version="0.1.0").message
    assert message == FIRST_MESSAGE.replace(
        FIRST_MESSAGE.split("\n\n", 1)[0],
        "**Days Until 0.1.0 is published, and it's live:** anyone in Pippa can install it.",
    ).replace(FIRST_MESSAGE[FIRST_MESSAGE.index("#### When it's ready"):FIRST_MESSAGE.index("Can't see")], "")


@pytest.mark.parametrize("status", [None, "draft", "pending", "rejected", "suspended", "APPROVED", "live", 1, True])
def test_only_approved_is_public(status):
    reply = {"change": "first"} if status is None else {"change": "first", "review_status": status}
    outcome = published(**reply)
    assert not outcome.public
    assert outcome.description == DRAFT_STATUS
    assert "it's live" not in outcome.message


@pytest.mark.parametrize("reply", [
    {},
    {"change": None},
    {"change": ""},
    {"change": "renamed"},
    {"change": "FIRST"},
    {"change": 1},
    {"change": ["first"]},
    {"change": {"first": True}},
])
def test_a_missing_or_unknown_change_gets_the_message_for_either_case(reply):
    outcome = published(manifest=None, guide_url="", **reply)
    assert outcome.change == ""
    assert outcome.message == EITHER_MESSAGE


@pytest.mark.parametrize("change", [*hp.CHANGES, ""])
@pytest.mark.parametrize("status", ["draft", "approved"])
def test_missing_api_keys_come_first_in_every_case(change, status):
    outcome = published(change=change, review_status=status, live_version="0.1.0", check_warnings=WARNINGS,
                        warnings=[KEYS_WARNING, LEGACY_PUBLISHER])
    message = outcome.message
    headline, rest = message.split("\n\n", 1)
    assert rest.startswith(f"{KEYS_SECTION}\n\n#### Try")
    assert message.count(KEYS_WARNING) == 1
    assert message.endswith(f"{WARNINGS_TITLE}{hp.fence(outcome.all_warnings)}")
    assert outcome.description.endswith("; add its API keys in Pippa")
    without_keys = published(change=change, review_status=status, live_version="0.1.0")
    assert headline == without_keys.message.split("\n\n", 1)[0]
    assert "First, add its API keys" not in without_keys.message


@pytest.mark.parametrize("change", [*hp.CHANGES, ""])
def test_the_warnings_section_is_last_in_every_case(change):
    outcome = published(change=change, review_status="draft", live_version="0.1.0", check_warnings=WARNINGS,
                        warnings=[LEGACY_PUBLISHER])
    message = outcome.message
    assert message.count("#### Warnings") == 1
    section = message.split(f"\n\n{WARNINGS_TITLE}", 1)[1]
    assert section == hp.fence(f"Pippa\n WARN {LEGACY_PUBLISHER}\n\n{WARNINGS}")
    assert "#### Warnings" not in published(change=change, review_status="draft").message


@pytest.mark.parametrize("change", [*hp.CHANGES, ""])
@pytest.mark.parametrize("status", ["draft", "approved"])
@pytest.mark.parametrize("keys", [[], [KEYS_WARNING]])
def test_the_status_says_draft_or_live_and_fits_in_140_characters(change, status, keys):
    long_id = "hack.a_rather_long_app_id_for_a_rather_long_app_name_x"
    outcome = hp.Published(RUN_URL, {**SUCCESS, "app_id": long_id, "version": "10.20.300", "change": change,
                                     "review_status": status, "warnings": keys})
    state = "Live" if status == "approved" else "Published as a private draft"
    expected = f"{state}: {long_id} v10.20.300" + ("; add its API keys in Pippa" if keys else "")
    assert outcome.description == expected
    assert len(expected) <= hp.STATUS_DESCRIPTION_LIMIT
    assert outcome.status_payload()["description"] == expected


def test_the_footer_tells_a_first_publisher_to_reload_and_everyone_where_the_guide_is():
    for change, reload in (("first", True), ("", True), ("new_version", False), ("same_version", False)):
        message = published(change=change, review_status="draft", live_version="0.1.0").message
        assert ("Can't see your app? Reload the Apps page" in message) is reload
        assert message.endswith(GUIDE_LINE)
    assert "REVIEW_AND_SHARING" not in published(change="first", guide_url="").message


# ---- the example phrase: what to ask Pippa ----

@pytest.mark.parametrize("manifest, phrase", [
    (PHRASE_MANIFEST, "how long until Friday?"),
    ({"outcomes": [{"id": "a", "example_phrases": ["", "   ", 7, None, "  days\n until\tpayday "]}]},
     "days until payday"),
    ({"outcomes": [{"id": "a"}, {"id": "b", "example_phrases": []}, {"id": "c", "example_phrases": ["second"]}]},
     "second"),
    ({"outcomes": [{"id": "a", "example_phrases": ["first"]}, {"id": "b", "example_phrases": ["later"]}]}, "first"),
    ({"outcomes": [{"id": "a", "example_phrases": ["x" * 500]}]}, "x" * (hp.PHRASE_LIMIT - 3) + "..."),
])
def test_the_example_phrase_is_the_first_one_that_is_not_empty(manifest, phrase):
    assert hp.example_phrase(manifest) == phrase


@pytest.mark.parametrize("manifest", [
    None, {}, {"outcomes": None}, {"outcomes": "days.until"}, {"outcomes": {"id": "a"}},
    {"outcomes": ["days.until", 5, None]}, {"outcomes": [{"example_phrases": "how long until Friday?"}]},
    {"outcomes": [{"example_phrases": {"a": "b"}}]}, {"outcomes": [{"example_phrases": [["nested"], {"a": 1}]}]},
])
def test_a_manifest_of_another_shape_has_no_example_phrase(manifest):
    assert hp.example_phrase(manifest) == ""
    message = published(manifest=manifest, change="first").message
    assert "4. Ask Pippa something your app does.\n" in message


def test_the_example_phrase_is_quoted_as_code_whatever_it_holds():
    phrase = "ask @octo about [this](https://example.com) <b>now</b> `x`"
    message = published(manifest={"outcomes": [{"example_phrases": [phrase]}]}, change="first").message
    assert f"for example {hp.code_span(phrase)}." in message
    assert hp.code_span(phrase) == f"`` {phrase} ``"


# ---- text from manifest.yaml in the comment: never a link, an image, HTML or a mention ----

def outside_code(markdown):
    """What GitHub would still read as Markdown: fenced blocks, escaped characters and code spans taken out."""
    text = re.sub(r"^(`{3,})text\n.*?\n\1$", "", markdown, flags=re.MULTILINE | re.DOTALL)
    text = re.sub(r"\\[!-/:-@\[-`{-~]", "", text)
    return re.sub(r"(?<!`)(`+)(?!`).+?(?<!`)\1(?!`)", "", text)


#: What would make Markdown, or what GitHub links after rendering, if it were not escaped or quoted.
ACTIVE = re.compile(r"[\[\]<>!*_~|&`\\]|@|://|www\.|#\d|gh-\d", re.IGNORECASE)
#: A heading or a list marker starting the text (a space always follows it in the message).
LINE_MARKER = re.compile(r"^(?:#{1,6}|[+*-]|\d{1,9}[.)])(?:\s|$)")

HOSTILE = [
    "Ask @octo now", "x@example.com", "@octo/team", "@@octo", "mailto:x@example.com",
    "Visit https://example.com/x", "HTTPS://EXAMPLE.COM", "See www.example.com today", "https://www.example.com",
    "Bug #1 fixed", "GH-12 tool", "gh-3", "octo/repo#1 helper", "x#1",
    "[click](https://example.com)", "![image](https://example.com/a.png)", "[ref]: https://example.com",
    "<img src=x onerror=alert(1)>", "<b>bold</b>", "&lt;b&gt; &#64;octo", "**bold** _it_ ~~gone~~ | cell",
    "a`b", "a`@`b", "a\\`@octo", "`` ` ``", "back\\slash\\", "# Heading", "###### Heading", "- item", "+ item",
    "1. item", "12) item", "-", "1.", "  # spaced\n- out ",
]


@pytest.mark.parametrize("text", HOSTILE)
def test_md_text_leaves_nothing_github_would_render_or_link(text):
    escaped = hp.md_text(text)
    assert not ACTIVE.search(outside_code(escaped)), escaped
    assert not LINE_MARKER.match(escaped), escaped
    # The words are all still there, in order: only escapes and code spans were added.
    assert re.sub(r"[\\`\s]", "", escaped) == re.sub(r"[\\`\s]", "", text)


@pytest.mark.parametrize("text, escaped", [
    ("Ask @octo now", "Ask `@`octo now"),
    ("@@octo", "`@@`octo"),
    ("x@example.com", "x`@`example.com"),
    ("Visit https://example.com/x", "Visit https`://`example.com/x"),
    ("https://www.example.com", "https`://www.`example.com"),
    ("See WWW.example.com", "See `WWW.`example.com"),
    ("Bug #1 fixed", "Bug `#`1 fixed"),
    ("GH-12 tool", "`GH-`12 tool"),
    ("[click](https://example.com)", "\\[click\\](https`://`example.com)"),
    ("![image](x.png)", "\\!\\[image\\](x.png)"),
    ("<b>bold</b>", "\\<b\\>bold\\</b\\>"),
    ("&amp;", "\\&amp;"),
    ("a`@`b", "a\\``@`\\`b"),
    ("# Heading", "\\# Heading"),
    ("###### Heading", "#####\\# Heading"),
    ("- item", "\\- item"),
    ("+ item", "\\+ item"),
    ("1. item", "1\\. item"),
    ("12) item", "12\\) item"),
    ("1.", "1\\."),
])
def test_md_text_escapes_and_quotes_exactly_what_it_must(text, escaped):
    assert hp.md_text(text) == escaped


@pytest.mark.parametrize("text", [
    "Days Until", "Tide Times", "Café Finder", "R2-D2 Helper", "UK Tides 2025", "C# Tools", "#hashtag",
    "Weather: today", "sigh-12 days", "0.1.0", "1.2.3-beta.4", "10.20.300", "Ask 3 things", "A+ Grades",
])
def test_md_text_leaves_ordinary_names_and_versions_as_written(text):
    assert hp.md_text(text) == text


def test_md_text_collapses_whitespace_and_cuts_long_text():
    assert hp.md_text("  Tide \n\t Times  ") == "Tide Times"
    assert hp.md_text("x" * 500) == "x" * (hp.NAME_LIMIT - 3) + "..."
    assert hp.md_text("1" * 500, hp.VERSION_LIMIT) == "1" * (hp.VERSION_LIMIT - 3) + "..."


@pytest.mark.parametrize("text, span", [
    ("plain", "`plain`"),
    ("a`b", "``a`b``"),
    ("a``b`c", "```a``b`c```"),
    ("`start", "`` `start ``"),
    ("end`", "`` end` ``"),
    ("`", "`` ` ``"),
    ("  two \n lines ", "`two lines`"),
    ("@octo [x](https://example.com) <b> #1", "`@octo [x](https://example.com) <b> #1`"),
])
def test_code_span_is_one_span_whatever_the_backticks(text, span):
    assert hp.code_span(text) == span
    assert outside_code(span) == ""


def test_text_from_the_manifest_never_reaches_the_comment_unescaped():
    hostile = "@octo [x](https://example.com) #1 <b>"
    outcome = hp.Published(
        RUN_URL,
        {**SUCCESS, "name": hostile, "version": f"0.2.0 {hostile}", "live_version": f"0.1.0 {hostile}",
         "change": "new_version", "review_status": "draft", "warnings": [f"{hostile} can't run until you add X"]},
        hostile, {"outcomes": [{"example_phrases": [hostile]}]}, GUIDE_URL,
    )
    for message in (outcome.message, published(name=hostile, version=hostile, change="same_version").message):
        ours = message.replace(hp.APPS_LINK, "").replace(GUIDE_LINE, "")
        assert not re.search(r"@|://|www\.|#\d|<|\[", outside_code(ours).replace("```text", "")), ours
    assert hp.md_text(hostile) in outcome.message


# ---- version numbers, the guide's address, the reply's keys reaching the message ----

@pytest.mark.parametrize("version, following", [
    ("0.2.0", "0.2.1"), ("1.9.99", "1.9.100"), ("10.0.0", "10.0.1"),
    ("0.2", ""), ("0.2.0-beta", ""), ("v0.2.0", ""), ("", ""), ("1.2.3.4", ""), ("1.2.x", ""), ("1234567.0.0", ""),
])
def test_next_version_is_the_next_patch_of_a_plain_version_only(version, following):
    assert hp.next_version(version) == following


def test_a_draft_of_another_shape_is_told_to_raise_its_version_without_an_example():
    message = published(change="same_version", review_status="draft", version="0.2").message
    assert "raise `version:` in `manifest.yaml` and push." in message
    assert ", say)" not in message


@pytest.mark.parametrize("environ, url", [
    ({"GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "Pippa-Labs/pippa-hack-x"}, GUIDE_URL),
    ({"GITHUB_SERVER_URL": "https://github.com/", "GITHUB_REPOSITORY": "Pippa-Labs/pippa-hack-x"}, GUIDE_URL),
    ({"GITHUB_SERVER_URL": "https://github.example.com", "GITHUB_REPOSITORY": "team_x/app.v2"},
     "https://github.example.com/team_x/app.v2/blob/HEAD/docs/REVIEW_AND_SHARING.md"),
])
def test_the_guide_url_is_this_repositorys_review_guide(environ, url):
    assert hp.review_guide_url(environ) == url


@pytest.mark.parametrize("server", [
    "", "http://github.com", "https://", "https://github.com/evil", "https://user@github.com",
    "https://github.com?x=1", "https://github.com#x", "https://github.com:8443", "https://github..com",
    "https://github.com)[x](https://example.com", "javascript:alert(1)", "https://github.com\n",
    " https://github.com", "ftp://github.com",
])
def test_the_guide_url_needs_a_plain_https_server(server):
    assert hp.review_guide_url({"GITHUB_SERVER_URL": server, "GITHUB_REPOSITORY": "Pippa-Labs/x"}) == ""


@pytest.mark.parametrize("repository", [
    "", "Pippa-Labs", "Pippa-Labs/", "/x", "a/b/c", "a/..", "a/.", "../b", "-a/b", "a b/c", "a/b)",
    "a/b](https://example.com)", "a/b#c", "a/b?c", "a/b\n", "a/" + "b" * 101,
])
def test_the_guide_url_needs_an_owner_and_a_repository_name(repository):
    assert hp.review_guide_url({"GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": repository}) == ""
    assert hp.review_guide_url({"GITHUB_SERVER_URL": "https://github.com"}) == ""


def test_the_reply_and_the_manifest_reach_the_message_through_the_factory(app, capsys):
    (app / "manifest.yaml").write_text(
        MANIFEST + "outcomes:\n  - id: days.until\n    example_phrases:\n      - how long until Friday?\n"
    )
    reply = {**SUCCESS, "change": "first", "review_status": "draft", "live_version": None}
    run, client = make_run(app, FakeClient(json.dumps(reply).encode()))
    run.factory = hp.OutcomeFactory(RUN_URL, REF, "", GUIDE_URL)
    outcome = run.execute(hp.CheckState())
    assert outcome.comment_payload()["body"] == FIRST_MESSAGE
    assert json.loads(client.calls[0]["Payload"])["manifest"]["outcomes"][0]["id"] == "days.until"
    assert "YOUR APP IS PUBLISHED IN PIPPA" in capsys.readouterr().out


def test_from_reply_without_a_manifest_has_no_example_phrase():
    outcome = hp.OutcomeFactory(RUN_URL, REF).from_reply({**SUCCESS, "change": "first"})
    assert outcome.phrase == "" and outcome.guide_url == ""
    assert "4. Ask Pippa something your app does.\n" in outcome.message


@pytest.mark.parametrize("status, banner", [("approved", "YOUR APP IS LIVE IN PIPPA"),
                                            ("draft", "YOUR APP IS PUBLISHED IN PIPPA"),
                                            (None, "YOUR APP IS PUBLISHED IN PIPPA")])
def test_the_log_banner_says_live_only_for_a_public_version(status, banner, capsys):
    hp.OutcomeFactory(RUN_URL, REF).from_reply({**SUCCESS, "review_status": status})
    out = capsys.readouterr().out
    assert banner in out
    assert "  App      Days Until  (hack.x v0.1.0)\n  Can do   days.until\n" in out


def test_the_log_banner_keeps_text_from_the_manifest_on_one_line(capsys):
    """A line of the log that starts with "::" is a workflow command to the runner."""
    reply = {**SUCCESS, "name": "Days\n::error::forged", "outcomes": ["a\n::add-mask::x"]}
    hp.OutcomeFactory(RUN_URL, REF).from_reply(reply)
    lines = capsys.readouterr().out.splitlines()
    assert not [line for line in lines if line.lstrip().startswith("::")]
    assert "  App      Days ::error::forged  (hack.x v0.1.0)" in lines


def test_the_summary_quotes_each_outcome_as_code():
    outcome = hp.Published(RUN_URL, {**SUCCESS, "outcomes": ["days.until", "a`b"]})
    assert outcome.summary.endswith("\nOutcomes: `days.until`, ``a`b``\n")


def test_main_links_the_review_guide_of_the_repository_that_ran(app, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "Pippa-Labs/pippa-hack-x")
    reply = {**SUCCESS, "change": "first", "review_status": "draft"}
    real_publisher = hp.LambdaPublisher
    monkeypatch.setattr(hp, "LambdaPublisher", lambda: real_publisher(
        client=FakeClient(json.dumps(reply).encode()), sleep=lambda _: None))
    monkeypatch.setattr(hp, "ActionsTokenProvider", FakeToken)
    out = tmp_path / "out"
    assert hp.main(["--app-dir", str(app), "--run-url", RUN_URL, "--reference", REF, "--out-dir", str(out)]) == 0
    body = json.loads((out / "comment.json").read_text())["body"]
    assert body.endswith(f"\n\n{GUIDE_LINE}")
    assert json.loads((out / "status.json").read_text())["description"] == DRAFT_STATUS

    monkeypatch.delenv("GITHUB_REPOSITORY")
    assert hp.main(["--app-dir", str(app), "--run-url", RUN_URL, "--reference", REF, "--out-dir", str(out)]) == 0
    assert "REVIEW_AND_SHARING" not in json.loads((out / "comment.json").read_text())["body"]
