"""Runs the hack-check and hack-publish scripts with stubbed gh, aws and pip."""

import json
import shutil
import subprocess

from conftest import CHECK_ACTION_DIR, PLACEHOLDER, PUBLISH_ACTION_DIR, base_env, make_app, read, write_exec

SHA = "a" * 64
PARAMETER = json.dumps({
    "version": "0.1.3",
    "filename": "pippa_agentapp_sdk-0.1.3-py3-none-any.whl",
    "sha256": SHA,
    "layer_arn": "arn:aws:lambda:eu-west-2:111122223333:layer:pippa-hack-sdk:5",
    "frozen": False,
    "updated_at": "2026-11-08T00:00:00Z",
})


def run_check_script(tmp_path, bindir, app, script, **extra):
    env = base_env(tmp_path, bindir)
    env.update({"GITHUB_ACTION_PATH": str(CHECK_ACTION_DIR), "GITHUB_WORKSPACE": str(app)})
    env.update(extra)
    result = subprocess.run(["bash", str(CHECK_ACTION_DIR / "scripts" / script)], cwd=app, env=env,
                            capture_output=True, text=True)
    return result, read(tmp_path / "output.txt"), read(tmp_path / "summary.md")


# ---- prepare ----

def test_prepare_placeholder_is_green_and_not_ready(tmp_path, bin_dir):
    app = make_app(tmp_path, manifest=PLACEHOLDER)
    result, output, summary = run_check_script(tmp_path, bin_dir, app, "prepare.sh")
    assert result.returncode == 0
    assert "ready=false" in output
    assert "Nothing to publish yet" in summary


def test_prepare_accepts_pypi_requirements(tmp_path, bin_dir):
    app = make_app(tmp_path, requirements="# tests\nPyYAML>=6.0\npytest>=8.0\n")
    result, output, _ = run_check_script(tmp_path, bin_dir, app, "prepare.sh")
    assert result.returncode == 0, result.stderr
    assert "ready=true" in output
    assert "verdict" not in output


def test_prepare_refuses_sdk_in_requirements_as_code_error(tmp_path, bin_dir):
    app = make_app(tmp_path, requirements="pippa_agentapp_sdk==0.1.3\npytest\n")
    result, output, _ = run_check_script(tmp_path, bin_dir, app, "prepare.sh")
    assert result.returncode == 0, result.stderr
    assert "ready=true" in output and "verdict=failed" in output
    assert " FAIL requirements-dev.txt line 1 installs the Pippa SDK" in output
    assert "Delete this line." in output


def test_prepare_refuses_committed_wheel(tmp_path, bin_dir):
    app = make_app(tmp_path, requirements="pytest\n")
    (app / "vendor").mkdir()
    (app / "vendor" / "pippa_agentapp_sdk-0.1.3-py3-none-any.whl").write_bytes(b"PK")
    result, output, _ = run_check_script(tmp_path, bin_dir, app, "prepare.sh")
    assert result.returncode == 0, result.stderr
    assert "verdict=failed" in output
    assert "vendor/pippa_agentapp_sdk-0.1.3-py3-none-any.whl is a wheel file" in output


def test_prepare_refuses_extra_index(tmp_path, bin_dir):
    app = make_app(tmp_path, requirements="--extra-index-url https://example.org/simple\npytest\n")
    result, output, _ = run_check_script(tmp_path, bin_dir, app, "prepare.sh")
    assert result.returncode == 0, result.stderr
    assert "sets a package index or download location (--extra-index-url)" in output


def test_prepare_guard_crash_fails_without_verdict(tmp_path, bin_dir):
    app = make_app(tmp_path, requirements="pytest\n")
    (bin_dir / "python").unlink()
    write_exec(bin_dir / "python", "#!/bin/sh\nexit 3\n")
    result, output, _ = run_check_script(tmp_path, bin_dir, app, "prepare.sh")
    assert result.returncode == 1
    assert "verdict" not in output
    assert "could not run (exit 3)" in result.stderr


# ---- install-sdk ----

def run_install(tmp_path, bin_dir, **extra):
    app = make_app(tmp_path)
    env = {"STUB_PIP": "1", "STUB_PARAMETER": PARAMETER,
           "PIP_EXTRA_INDEX_URL": "https://evil.example/simple"}
    env.update(extra)
    result, _, _ = run_check_script(tmp_path, bin_dir, app, "install-sdk.sh", **env)
    return result, read(tmp_path / "calls.log")


def test_install_sdk_uses_only_the_sdk_repository_with_pin_and_hash(tmp_path, bin_dir):
    result, calls = run_install(tmp_path, bin_dir)
    assert result.returncode == 0, result.stderr
    pip = next(line for line in calls.splitlines() if line.startswith("pip "))
    assert "--index-url https://aws:tok-secret-value@pippa-780886633355.d.codeartifact.eu-west-2.amazonaws.com" \
           "/pypi/pippa-prod-codeartifact-euw2-hack-sdk/simple/" in pip
    assert "extra-index-url" not in pip
    for flag in ("--no-deps", "--require-hashes", "--only-binary :all:", "--no-cache-dir"):
        assert flag in pip
    assert f"REQUIREMENTS pippa-agentapp-sdk==0.1.3 --hash=sha256:{SHA}" in calls
    assert "PIP_CONFIG_FILE=/dev/null" in calls
    assert "PIP_EXTRA_INDEX_URL=unset" in calls
    assert "FLAGS=-I" in calls
    assert f"CWD={tmp_path / 'runner' / 'hack-sdk'}" in calls


PLANTED_PIP = "open({marker!r}, 'w').write('planted pip ran')\n"


def test_planted_pip_in_the_app_never_runs_while_credentials_exist(tmp_path, bin_dir):
    app = make_app(tmp_path)
    marker = tmp_path / "planted.txt"
    (app / "pip").mkdir()
    (app / "pip" / "__init__.py").write_text("")
    (app / "pip" / "__main__.py").write_text(PLANTED_PIP.format(marker=str(marker)))

    subprocess.run([str(bin_dir / "python"), "-m", "pip", "--version"], cwd=app,
                   env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
    assert marker.exists(), "control: python -m pip from the app root runs the planted package"
    marker.unlink()

    result, _, _ = run_check_script(
        tmp_path, bin_dir, app, "install-sdk.sh", STUB_PARAMETER=PARAMETER,
        STUB_ENDPOINT="https://127.0.0.1:9/pypi/sdk/", PIP_RETRIES="0", PIP_TIMEOUT="2",
    )
    assert result.returncode != 0
    assert not marker.exists()
    assert "tok-secret-value" not in result.stderr


def test_install_sdk_token_is_short_lived_and_masked(tmp_path, bin_dir):
    result, calls = run_install(tmp_path, bin_dir)
    assert "--duration-seconds 900" in calls
    assert "--domain pippa --domain-owner 780886633355" in calls
    assert "--name /pippa/prod/hack/sdk" in calls
    assert "::add-mask::tok-secret-value" in result.stdout
    assert "tok-secret-value" not in result.stderr


def test_install_sdk_leaves_no_files(tmp_path, bin_dir):
    result, _ = run_install(tmp_path, bin_dir)
    assert result.returncode == 0
    assert not (tmp_path / "runner" / "hack-sdk").exists()
    assert list((tmp_path / "home").iterdir()) == []


def test_install_sdk_bad_parameter_fails_before_any_token(tmp_path, bin_dir):
    result, calls = run_install(tmp_path, bin_dir, STUB_PARAMETER='{"version": "0.1.3"}')
    assert result.returncode != 0
    assert "get-authorization-token" not in calls
    assert "pip " not in calls
    assert not (tmp_path / "runner" / "hack-sdk").exists()


def test_install_sdk_token_failure_fails(tmp_path, bin_dir):
    result, calls = run_install(tmp_path, bin_dir, STUB_AWS_FAIL="get-authorization-token")
    assert result.returncode != 0
    assert "pip " not in calls


def test_install_sdk_empty_token_fails(tmp_path, bin_dir):
    result, calls = run_install(tmp_path, bin_dir, STUB_TOKEN="")
    assert result.returncode != 0
    assert "pip " not in calls


def test_install_sdk_pip_failure_fails_and_cleans_up(tmp_path, bin_dir):
    result, _ = run_install(tmp_path, bin_dir, STUB_PIP_EXIT="1")
    assert result.returncode != 0
    assert "tok-secret-value" not in result.stderr
    assert not (tmp_path / "runner" / "hack-sdk").exists()


# ---- clear-credentials ----

def test_clear_blanks_credentials_and_removes_leftovers(tmp_path, bin_dir):
    app = make_app(tmp_path)
    home = tmp_path / "home"
    (home / ".config" / "pip").mkdir(parents=True)
    leaked = home / ".config" / "pip" / "pip.conf"
    leaked.write_text("[global]\nindex-url = https://aws:x@pippa-1.d.codeartifact.eu-west-2.amazonaws.com/\n")
    (home / ".pip").mkdir()
    kept = home / ".pip" / "pip.conf"
    kept.write_text("[global]\ntimeout = 60\n")
    (tmp_path / "runner" / "hack-sdk").mkdir(parents=True)
    result, _, _ = run_check_script(tmp_path, bin_dir, app, "clear-credentials.sh",
                                    AWS_ACCESS_KEY_ID="AKIA", AWS_SECRET_ACCESS_KEY="s", AWS_SESSION_TOKEN="t")
    assert result.returncode == 0, result.stderr
    lines = read(tmp_path / "env.txt").splitlines()
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN"):
        assert f"{name}=" in lines
    assert not leaked.exists()
    assert kept.exists()
    assert not (tmp_path / "runner" / "hack-sdk").exists()


# ---- check-app ----

def test_check_passing(tmp_path, bin_dir):
    result, output, _ = run_check_script(tmp_path, bin_dir, make_app(tmp_path), "check-app.sh")
    assert result.returncode == 0, result.stderr
    assert "verdict=passed" in output
    assert "warnings" not in output


def output_value(output: str, name: str) -> str:
    lines = output.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}<<"))
    delimiter = lines[start].split("<<", 1)[1]
    end = lines.index(delimiter, start + 1)
    return "\n".join(lines[start + 1:end])


def test_check_warnings_and_routing_findings_are_output(tmp_path, bin_dir):
    check_body = (
        "print(' WARN owner_email is still the template placeholder (you@example.com)')\n"
        "print('       Ask the person for the address they sign in with.')\n"
        "print('  OK  app.py is present')\n"
        "print(' WARN owner_email is still the template placeholder (you@example.com)')\n"
        "print('Ready to push.')"
    )
    routing_body = (
        "print('  OK  summary leads with something specific')\n"
        "print(' FAIL days.until: \"how long\" is one ordinary word')\n"
        "print('       Carry the subject in the phrase.')\n"
        "print('TRY THESE ONCE IT IS LIVE')\n"
        "print('    - when is christmas')\n"
        "print('1 problem(s) that will stop your app being chosen.')\n"
        "sys.exit(1)"
    )
    app = make_app(tmp_path, check_body=check_body, routing_body=routing_body)
    result, output, _ = run_check_script(tmp_path, bin_dir, app, "check-app.sh")
    assert result.returncode == 0, result.stderr
    assert "verdict=passed" in output
    warnings = output_value(output, "warnings")
    assert warnings.count("owner_email is still the template placeholder") == 1
    assert "Ask the person for the address" in warnings
    assert ' FAIL days.until: "how long" is one ordinary word' in warnings
    assert "1 problem(s) that will stop your app being chosen." in warnings
    assert "when is christmas" not in warnings and "  OK " not in warnings


def test_routing_check_crash_is_a_warning_not_a_failure(tmp_path, bin_dir):
    app = make_app(tmp_path, routing_body="raise ImportError('no module named pippa_agentapp_sdk')")
    result, output, _ = run_check_script(tmp_path, bin_dir, app, "check-app.sh")
    assert result.returncode == 0, result.stderr
    assert "verdict=passed" in output and "verdict=failed" not in output
    warnings = output_value(output, "warnings")
    assert "the routing check could not run (exit 1); routing was not checked." in warnings
    assert "ImportError: no module named pippa_agentapp_sdk" in warnings


def test_missing_routing_check_is_a_warning(tmp_path, bin_dir):
    app = make_app(tmp_path, routing_body=None)
    result, output, _ = run_check_script(tmp_path, bin_dir, app, "check-app.sh")
    assert result.returncode == 0
    assert "the routing check could not run (exit 2)" in output_value(output, "warnings")


def test_routing_check_does_not_run_after_a_failed_check(tmp_path, bin_dir):
    app = make_app(tmp_path, check_body="sys.exit(1)", routing_body="open('ran', 'w').write('x')")
    result, output, _ = run_check_script(tmp_path, bin_dir, app, "check-app.sh")
    assert result.returncode == 0, result.stderr
    assert "warnings" not in output
    assert not (app / "ran").exists()


def test_broken_warnings_summary_keeps_the_verdict(tmp_path, bin_dir):
    app = make_app(tmp_path)
    broken = tmp_path / "action"
    shutil.copytree(CHECK_ACTION_DIR.parent.parent, broken / ".github", dirs_exist_ok=True)
    (broken / ".github" / "scripts" / "hack" / "check_warnings.py").write_text("raise SystemExit(5)\n")
    result, output, _ = run_check_script(tmp_path, bin_dir, app, "check-app.sh",
                                         GITHUB_ACTION_PATH=str(broken / ".github" / "actions" / "hack-check"))
    assert result.returncode == 0, result.stderr
    assert "verdict=passed" in output and "warnings" not in output
    assert "could not be summarised" in result.stderr


def test_check_refuses_to_run_app_code_with_credentials(tmp_path, bin_dir):
    app = make_app(tmp_path, check_body="open('ran', 'w').write('x')")
    result, output, _ = run_check_script(tmp_path, bin_dir, app, "check-app.sh", AWS_SESSION_TOKEN="t")
    assert result.returncode == 1
    assert "verdict" not in output
    assert not (app / "ran").exists()


def test_check_runs_app_code_without_credential_variables(tmp_path, bin_dir):
    body = "import os\nprint(sorted(k for k in os.environ if k.startswith('AWS_')))"
    app = make_app(tmp_path, check_body=body)
    result, _, _ = run_check_script(tmp_path, bin_dir, app, "check-app.sh",
                                    AWS_ACCESS_KEY_ID="", AWS_SECRET_ACCESS_KEY="", AWS_SESSION_TOKEN="")
    assert result.returncode == 0
    assert "[]" in result.stdout


def test_check_failing_collects_fail_lines(tmp_path, bin_dir):
    body = (
        "print('  OK  app.py is present')\n"
        "print(' FAIL manifest.yaml declares x')\n"
        "print('       Change `app.id` in manifest.yaml to: hack.smoke')\n"
        "print('FAILED test_app.py::test_x - assert 1 == 2')\n"
        "sys.exit(1)"
    )
    result, output, _ = run_check_script(tmp_path, bin_dir, make_app(tmp_path, check_body=body), "check-app.sh")
    assert result.returncode == 0, result.stderr
    assert "verdict=failed" in output
    assert " FAIL manifest.yaml declares x" in output
    assert "Change `app.id` in manifest.yaml to: hack.smoke" in output
    assert "FAILED test_app.py::test_x" in output
    assert "OK  app.py" not in output


def test_check_crash_without_fail_lines_uses_log_tail(tmp_path, bin_dir):
    body = "raise RuntimeError('broken check')"
    result, output, _ = run_check_script(tmp_path, bin_dir, make_app(tmp_path, check_body=body), "check-app.sh")
    assert result.returncode == 0, result.stderr
    assert "RuntimeError: broken check" in output


# ---- hack-publish ----

def run_publish(tmp_path, bindir, **inputs):
    env = base_env(tmp_path, bindir)
    env.update({
        "GITHUB_ACTION_PATH": str(PUBLISH_ACTION_DIR),
        "APP_DIR": "app",
        "REPORT_TOKEN": "t",
        "CHECKOUT_OUTCOME": "success",
        "PYTHON_OUTCOME": "success",
        "DEPENDENCIES_OUTCOME": "success",
        "CREDENTIALS": "success",
        "CHECK_RESULT": "success",
        "CHECK_VERDICT": "passed",
    })
    env.update(inputs)
    result = subprocess.run(["bash", str(PUBLISH_ACTION_DIR / "hack-publish.sh")], cwd=tmp_path, env=env,
                            capture_output=True, text=True)
    return result, read(tmp_path / "calls.log")


def posted(calls: str, kind: str) -> dict:
    lines = calls.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith("gh ") and kind in line)
    return json.loads(lines[index + 1])


def test_publish_reports_check_failure_as_code(tmp_path, bin_dir):
    make_app(tmp_path)
    result, calls = run_publish(tmp_path, bin_dir, CHECK_RESULT="failure", CHECK_VERDICT="failed",
                                CHECK_DETAILS=" FAIL the tests fail", CHECKOUT_OUTCOME="skipped",
                                DEPENDENCIES_OUTCOME="skipped", CREDENTIALS="skipped")
    assert result.returncode == 1
    assert "aws cloudwatch" not in calls
    assert posted(calls, "/statuses/abc123")["description"] == "Code error: FAIL the tests fail"
    assert posted(calls, "/commits/abc123/comments")["body"].startswith("CODE_ERROR")


def test_publish_reports_dependency_refusal_as_code(tmp_path, bin_dir):
    make_app(tmp_path)
    details = (" FAIL requirements-dev.txt line 1 installs the Pippa SDK\n"
               "       The build installs the Pippa SDK itself, at the version your app runs with. Delete this line.")
    result, calls = run_publish(tmp_path, bin_dir, CHECK_RESULT="failure", CHECK_VERDICT="failed",
                                CHECK_DETAILS=details, CHECKOUT_OUTCOME="skipped",
                                DEPENDENCIES_OUTCOME="skipped", CREDENTIALS="skipped")
    assert result.returncode == 1
    body = posted(calls, "/commits/abc123/comments")["body"]
    assert body.startswith("CODE_ERROR") and "installs the Pippa SDK" in body


def test_publish_check_platform_failure_is_infra_with_metric(tmp_path, bin_dir):
    make_app(tmp_path)
    result, calls = run_publish(tmp_path, bin_dir, CHECK_RESULT="failure", CHECK_VERDICT="",
                                CHECKOUT_OUTCOME="skipped", DEPENDENCIES_OUTCOME="skipped")
    assert result.returncode == 1
    assert "aws cloudwatch put-metric-data --namespace Pippa/Hackathon --metric-name InfraErrors --value 1" in calls
    assert posted(calls, "/commits/abc123/comments")["body"].startswith("PLATFORM_ERROR")


def test_publish_setup_failure_is_infra_with_metric(tmp_path, bin_dir):
    make_app(tmp_path)
    result, calls = run_publish(tmp_path, bin_dir, DEPENDENCIES_OUTCOME="failure")
    assert result.returncode == 1
    assert "aws cloudwatch put-metric-data --namespace Pippa/Hackathon --metric-name InfraErrors --value 1" in calls
    assert posted(calls, "/commits/abc123/comments")["body"].startswith("PLATFORM_ERROR")


def test_publish_credentials_failure_is_infra_without_metric(tmp_path, bin_dir):
    make_app(tmp_path)
    result, calls = run_publish(tmp_path, bin_dir, CREDENTIALS="failure")
    assert result.returncode == 1
    assert "aws cloudwatch" not in calls
    assert posted(calls, "/statuses/abc123")["description"] == "Platform error, reference gh-99-1"


def test_publish_module_crash_uses_fallback_report(tmp_path, bin_dir):
    make_app(tmp_path)
    (bin_dir / "python").unlink()
    write_exec(bin_dir / "python", "#!/bin/sh\nexit 3\n")
    result, calls = run_publish(tmp_path, bin_dir)
    assert result.returncode == 1
    assert posted(calls, "/statuses/abc123")["state"] == "failure"
    assert posted(calls, "/commits/abc123/comments")["body"].startswith("PLATFORM_ERROR")


def test_publish_hands_the_check_warnings_to_the_module(tmp_path, bin_dir):
    make_app(tmp_path)
    result, _ = run_publish(tmp_path, bin_dir, CHECK_RESULT="failure", CHECK_VERDICT="failed",
                            CHECK_DETAILS=" FAIL x", CHECK_WARNINGS=" WARN owner_email is missing",
                            CHECKOUT_OUTCOME="skipped", DEPENDENCIES_OUTCOME="skipped", CREDENTIALS="skipped")
    assert result.returncode == 1
    assert read(tmp_path / "runner" / "hack-check-warnings.txt") == " WARN owner_email is missing"
