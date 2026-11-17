"""Runs the hack-check and hack-publish scripts, and the skipped job's step, with stubbed gh, aws, gitleaks and pip."""

import json
import os
import shutil
import subprocess

import pytest
import yaml

from conftest import (CHECK_ACTION_DIR, MANIFEST, PLACEHOLDER, PUBLISH_ACTION_DIR, REPO, base_env, make_app,
                      read, write_exec)

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


# ---- prepare: nothing to publish yet, and personal data ----

# Invented addresses: college.local is never a real mailbox; a commit email is refused
# unless it is a GitHub no-reply address, so example.com works for commits.
PERSONAL = "ada.lovelace" + "@" + "college.local"
NOREPLY = "1234567+tide-watcher@users.noreply.github.com"
AUTHOR_BLANK = 'app_author_name: ""\n' + MANIFEST


def git(app, *args, email=NOREPLY):
    env = {**os.environ, "GIT_AUTHOR_NAME": "Tide Watcher", "GIT_AUTHOR_EMAIL": email,
           "GIT_COMMITTER_NAME": "Tide Watcher", "GIT_COMMITTER_EMAIL": email,
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    return subprocess.run(["git", "-C", str(app), *args], env=env, capture_output=True, text=True,
                          check=True).stdout.strip()


def commit_all(app, email=NOREPLY):
    if not (app / ".git").exists():
        git(app, "init", "-q")
    git(app, "add", "-A")
    git(app, "commit", "-q", "--allow-empty", "-m", "Change the app", email=email)
    return git(app, "rev-parse", "HEAD")


def push_env(tmp_path, before, after, private):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"before": before, "after": after, "commits": [],
                                 "repository": {"private": private}}))
    return {"GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": str(event), "GITHUB_SHA": after}


def test_prepare_blank_author_name_is_nothing_to_publish(tmp_path, bin_dir):
    app = make_app(tmp_path, manifest=AUTHOR_BLANK, requirements="pippa_agentapp_sdk==0.1.3\n")
    result, output, summary = run_check_script(tmp_path, bin_dir, app, "prepare.sh")
    assert result.returncode == 0, result.stderr
    assert "ready=false" in output and "verdict" not in output
    assert summary.startswith("### Nothing to publish yet\n\nNothing to publish yet: choose your public author "
                              "name (app_author_name in manifest.yaml). Claude will ask you for it.")


def test_prepare_placeholder_and_blank_author_name_give_both_reasons(tmp_path, bin_dir):
    app = make_app(tmp_path, manifest='app_author_name: ""\n' + PLACEHOLDER)
    result, output, summary = run_check_script(tmp_path, bin_dir, app, "prepare.sh")
    assert result.returncode == 0, result.stderr
    assert "ready=false" in output
    assert "placeholder app id hack.your_app_name_here" in summary
    assert "choose your public author name" in summary


def test_prepare_refuses_an_email_address_in_a_file(tmp_path, bin_dir):
    app = make_app(tmp_path)
    (app / "notes.md").write_text(f"# Notes\nAsk {PERSONAL}\n")
    result, output, _ = run_check_script(tmp_path, bin_dir, app, "prepare.sh")
    assert result.returncode == 0, result.stderr
    assert "ready=true" in output and "verdict=failed" in output
    assert " FAIL notes.md:2 contains the email address a***@college.local" in output
    assert "nothing personal belongs in this repository" in output
    assert PERSONAL not in output + result.stdout + result.stderr


def test_prepare_reports_personal_data_even_with_nothing_to_publish(tmp_path, bin_dir):
    app = make_app(tmp_path, manifest=PLACEHOLDER)
    (app / "notes.md").write_text(f"Ask {PERSONAL}\n")
    result, output, summary = run_check_script(tmp_path, bin_dir, app, "prepare.sh")
    assert result.returncode == 0, result.stderr
    assert "ready=true" in output and "verdict=failed" in output
    assert "Nothing to publish yet" not in summary


def test_prepare_lists_personal_data_and_dependency_refusals_together(tmp_path, bin_dir):
    app = make_app(tmp_path, requirements="pippa_agentapp_sdk==0.1.3\n")
    (app / "notes.md").write_text(f"Ask {PERSONAL}\n")
    result, output, _ = run_check_script(tmp_path, bin_dir, app, "prepare.sh")
    assert result.returncode == 0, result.stderr
    assert output.count("verdict=failed") == 1
    details = output_value(output, "details")
    assert " FAIL notes.md:1 contains the email address" in details
    assert " FAIL requirements-dev.txt line 1 installs the Pippa SDK" in details


def test_prepare_refuses_a_personal_commit_email_in_a_public_repository(tmp_path, bin_dir):
    app = make_app(tmp_path)
    base = commit_all(app)
    (app / "helper.py").write_text("X = 1\n")
    tip = commit_all(app, email=PERSONAL)
    result, output, _ = run_check_script(tmp_path, bin_dir, app, "prepare.sh",
                                         **push_env(tmp_path, base, tip, private=False))
    assert result.returncode == 0, result.stderr
    assert "verdict=failed" in output
    assert (f" FAIL commit {tip[:7]} (author and committer): Your commit shows the email address "
            "a***@college.local publicly.") in output
    assert PERSONAL not in output + result.stdout + result.stderr


def test_prepare_warns_on_a_personal_commit_email_in_a_private_repository(tmp_path, bin_dir):
    app = make_app(tmp_path)
    base = commit_all(app)
    (app / "helper.py").write_text("X = 1\n")
    tip = commit_all(app, email="builder@example.com")
    result, output, summary = run_check_script(tmp_path, bin_dir, app, "prepare.sh",
                                               **push_env(tmp_path, base, tip, private=True))
    assert result.returncode == 0, result.stderr
    assert "ready=true" in output and "verdict" not in output
    warnings = output_value(output, "warnings")
    assert warnings.startswith(f" WARN commit {tip[:7]} (author and committer): This commit shows the email "
                               "address b***@example.com")
    assert "### Warnings" in summary and "b***@example.com" in summary


def test_prepare_warnings_alone_do_not_publish_a_placeholder_app(tmp_path, bin_dir):
    app = make_app(tmp_path, manifest=PLACEHOLDER)
    base = commit_all(app)
    (app / "helper.py").write_text("X = 1\n")
    tip = commit_all(app, email="builder@example.com")
    result, output, summary = run_check_script(tmp_path, bin_dir, app, "prepare.sh",
                                               **push_env(tmp_path, base, tip, private=True))
    assert result.returncode == 0, result.stderr
    assert "ready=false" in output and "verdict" not in output
    assert "### Warnings" in summary and "### Nothing to publish yet" in summary


def test_prepare_passes_a_clean_push(tmp_path, bin_dir):
    app = make_app(tmp_path)
    base = commit_all(app)
    (app / "helper.py").write_text("X = 1\n")
    tip = commit_all(app)
    result, output, _ = run_check_script(tmp_path, bin_dir, app, "prepare.sh",
                                         **push_env(tmp_path, base, tip, private=False))
    assert result.returncode == 0, result.stderr
    assert "ready=true" in output and "verdict" not in output and "warnings" not in output


def test_prepare_personal_data_check_crash_fails_without_verdict(tmp_path, bin_dir):
    app = make_app(tmp_path)
    result, output, _ = run_check_script(tmp_path, bin_dir, app, "prepare.sh",
                                         **push_env(tmp_path, "a" * 40, "b" * 40, private=False))
    assert result.returncode == 1
    assert "verdict" not in output and "ready" not in output
    assert "The personal data check could not run (exit 2)" in result.stderr


# ---- fetch-sdk (the SDK job) ----

def run_fetch(tmp_path, bin_dir, **extra):
    app = make_app(tmp_path)
    env = {"STUB_PIP": "1", "STUB_PARAMETER": PARAMETER,
           "PIP_EXTRA_INDEX_URL": "https://evil.example/simple"}
    env.update(extra)
    result, _, _ = run_check_script(tmp_path, bin_dir, app, "fetch-sdk.sh", **env)
    return result, read(tmp_path / "calls.log")


HANDOVER = "hack-sdk-handover"


def test_fetch_sdk_downloads_only_from_the_sdk_repository_with_pin_and_hash(tmp_path, bin_dir):
    result, calls = run_fetch(tmp_path, bin_dir)
    assert result.returncode == 0, result.stderr
    pip = next(line for line in calls.splitlines() if line.startswith("pip "))
    assert pip.startswith("pip download ")
    assert "--index-url https://aws:tok-secret-value@pippa-780886633355.d.codeartifact.eu-west-2.amazonaws.com" \
           "/pypi/pippa-prod-codeartifact-euw2-hack-sdk/simple/" in pip
    assert f"--dest {tmp_path / 'runner' / HANDOVER}" in pip
    assert "extra-index-url" not in pip
    for flag in ("--no-deps", "--require-hashes", "--only-binary :all:", "--no-cache-dir"):
        assert flag in pip
    assert f"REQUIREMENTS pippa-agentapp-sdk==0.1.3 --hash=sha256:{SHA}" in calls
    assert "PIP_CONFIG_FILE=/dev/null" in calls
    assert "PIP_EXTRA_INDEX_URL=unset" in calls
    assert "FLAGS=-I" in calls
    assert f"CWD={tmp_path / 'runner' / 'hack-sdk'}" in calls


def test_fetch_sdk_hands_over_the_pin_and_one_wheel_and_nothing_else(tmp_path, bin_dir):
    result, _ = run_fetch(tmp_path, bin_dir)
    assert result.returncode == 0, result.stderr
    handover = tmp_path / "runner" / HANDOVER
    assert sorted(path.name for path in handover.iterdir()) == ["pippa_agentapp_sdk-0.1.3-py3-none-any.whl", "sdk.txt"]
    assert (handover / "sdk.txt").read_text() == f"pippa-agentapp-sdk==0.1.3 --hash=sha256:{SHA}\n"
    assert not (tmp_path / "runner" / "hack-sdk").exists()
    assert list((tmp_path / "home").iterdir()) == []
    assert "tok-secret-value" not in "".join(path.read_text(errors="replace") for path in handover.iterdir())


PLANTED_PIP = "open({marker!r}, 'w').write('planted pip ran')\n"


def test_planted_pip_in_the_current_directory_never_runs_while_credentials_exist(tmp_path, bin_dir):
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
        tmp_path, bin_dir, app, "fetch-sdk.sh", STUB_PARAMETER=PARAMETER,
        STUB_ENDPOINT="https://127.0.0.1:9/pypi/sdk/", PIP_RETRIES="0", PIP_TIMEOUT="2",
    )
    assert result.returncode != 0
    assert not marker.exists()
    assert "tok-secret-value" not in result.stderr


def test_fetch_sdk_token_is_short_lived_and_masked(tmp_path, bin_dir):
    result, calls = run_fetch(tmp_path, bin_dir)
    assert "--duration-seconds 900" in calls
    assert "--domain pippa --domain-owner 780886633355" in calls
    assert "--name /pippa/prod/hack/sdk" in calls
    assert "::add-mask::tok-secret-value" in result.stdout
    assert "tok-secret-value" not in result.stderr


def test_fetch_sdk_bad_parameter_fails_before_any_token(tmp_path, bin_dir):
    result, calls = run_fetch(tmp_path, bin_dir, STUB_PARAMETER='{"version": "0.1.3"}')
    assert result.returncode != 0
    assert "get-authorization-token" not in calls
    assert "pip " not in calls
    assert not (tmp_path / "runner" / "hack-sdk").exists()
    assert not (tmp_path / "runner" / HANDOVER).exists()


def test_fetch_sdk_token_failure_fails(tmp_path, bin_dir):
    result, calls = run_fetch(tmp_path, bin_dir, STUB_AWS_FAIL="get-authorization-token")
    assert result.returncode != 0
    assert "pip " not in calls
    assert not (tmp_path / "runner" / HANDOVER).exists()


def test_fetch_sdk_empty_token_fails(tmp_path, bin_dir):
    result, calls = run_fetch(tmp_path, bin_dir, STUB_TOKEN="")
    assert result.returncode != 0
    assert "pip " not in calls


def test_fetch_sdk_pip_failure_fails_and_cleans_up(tmp_path, bin_dir):
    result, _ = run_fetch(tmp_path, bin_dir, STUB_PIP_EXIT="1")
    assert result.returncode != 0
    assert "tok-secret-value" not in result.stderr
    assert not (tmp_path / "runner" / "hack-sdk").exists()
    assert not (tmp_path / "runner" / HANDOVER).exists()


def test_fetch_sdk_without_a_wheel_fails(tmp_path, bin_dir):
    result, _ = run_fetch(tmp_path, bin_dir, STUB_PIP_NO_WHEEL="1")
    assert result.returncode != 0
    assert "Expected sdk.txt and one SDK wheel to hand over, found 1 file(s)" in result.stderr
    assert not (tmp_path / "runner" / HANDOVER).exists()


# ---- install-sdk (the check job) ----

PIN = f"pippa-agentapp-sdk==0.1.3 --hash=sha256:{SHA}\n"


def run_install(tmp_path, bin_dir, pin=PIN, wheel=True, **extra):
    app = make_app(tmp_path)
    handover = tmp_path / "runner" / HANDOVER
    handover.mkdir(parents=True)
    if pin is not None:
        (handover / "sdk.txt").write_text(pin)
    if wheel:
        (handover / "pippa_agentapp_sdk-0.1.3-py3-none-any.whl").write_bytes(b"PK")
    env = {"STUB_PIP": "1", "PIP_EXTRA_INDEX_URL": "https://evil.example/simple"}
    env.update(extra)
    result, _, _ = run_check_script(tmp_path, bin_dir, app, "install-sdk.sh", **env)
    return result, read(tmp_path / "calls.log"), handover


def test_install_sdk_installs_the_handed_over_wheel_offline_with_its_hash(tmp_path, bin_dir):
    result, calls, handover = run_install(tmp_path, bin_dir)
    assert result.returncode == 0, result.stderr
    pip = next(line for line in calls.splitlines() if line.startswith("pip "))
    assert pip.startswith("pip install ")
    assert f"--no-index --find-links {handover}" in pip
    for flag in ("--no-deps", "--require-hashes", "--only-binary :all:", "--no-cache-dir"):
        assert flag in pip
    assert "index-url" not in pip
    assert f"REQUIREMENTS {PIN.strip()}" in calls
    assert "PIP_CONFIG_FILE=/dev/null" in calls and "PIP_EXTRA_INDEX_URL=unset" in calls
    assert "FLAGS=-I" in calls
    assert f"CWD={handover}" in calls
    assert not handover.exists()
    assert "aws " not in calls


@pytest.mark.parametrize("pin", [
    None,
    "pippa-agentapp-sdk==0.1.3\n",
    f"pippa-agentapp-sdk==0.1.3 --hash=sha256:{SHA}\n--extra-index-url https://evil.example/simple\n",
    f"evil-package==1.0 --hash=sha256:{SHA}\n",
    f"pippa-agentapp-sdk==0.1.3 --hash=sha256:{SHA} --index-url https://evil.example/\n",
])
def test_install_sdk_refuses_anything_but_one_hash_locked_sdk_requirement(tmp_path, bin_dir, pin):
    result, calls, handover = run_install(tmp_path, bin_dir, pin=pin)
    assert result.returncode == 1
    assert "pip " not in calls
    assert not handover.exists()


def test_install_sdk_pip_failure_fails_and_cleans_up(tmp_path, bin_dir):
    result, _, handover = run_install(tmp_path, bin_dir, STUB_PIP_EXIT="1")
    assert result.returncode != 0
    assert not handover.exists()


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


@pytest.mark.parametrize("name", ["ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN"])
def test_check_refuses_to_run_app_code_where_it_could_request_an_oidc_token(tmp_path, bin_dir, name):
    app = make_app(tmp_path, check_body="open('ran', 'w').write('x')")
    result, output, _ = run_check_script(tmp_path, bin_dir, app, "check-app.sh", **{name: "https://token.example"})
    assert result.returncode == 1
    assert "verdict" not in output
    assert f"{name} is set; the app's code never runs in a job that can request an OIDC token" in result.stderr
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


def test_publish_of_a_rerun_another_account_started_sends_counts_and_reports_nothing(tmp_path, bin_dir):
    make_app(tmp_path)
    result, calls = run_publish(tmp_path, bin_dir, GITHUB_TRIGGERING_ACTOR="org-writer")
    assert result.returncode == 1
    assert "gh " not in calls, "nothing is posted on the commit"
    assert "aws " not in calls, "no platform error is counted"
    assert "this re-run was started by org-writer, not by tide-watcher, who pushed" in result.stderr
    assert "only the account that pushed can re-run a publish; nothing is reported on the commit" in result.stderr
    assert not (tmp_path / "runner" / "hack-publish" / "outcome").exists()


def test_publish_reports_nothing_when_the_module_says_another_account_started_the_run(tmp_path, bin_dir):
    make_app(tmp_path)
    (bin_dir / "python").unlink()
    write_exec(bin_dir / "python", "#!/bin/sh\nexit 77\n")
    result, calls = run_publish(tmp_path, bin_dir)
    assert result.returncode == 1
    assert "gh " not in calls and "aws " not in calls
    assert "PLATFORM_ERROR" not in calls


def test_publish_hands_the_check_warnings_to_the_module(tmp_path, bin_dir):
    make_app(tmp_path)
    result, _ = run_publish(tmp_path, bin_dir, CHECK_RESULT="failure", CHECK_VERDICT="failed",
                            CHECK_DETAILS=" FAIL x", CHECK_WARNINGS=" WARN owner_email is missing",
                            CHECKOUT_OUTCOME="skipped", DEPENDENCIES_OUTCOME="skipped", CREDENTIALS="skipped")
    assert result.returncode == 1
    assert read(tmp_path / "runner" / "hack-check-warnings.txt") == " WARN owner_email is missing"


def test_publish_puts_the_personal_data_warnings_first(tmp_path, bin_dir):
    make_app(tmp_path)
    result, _ = run_publish(tmp_path, bin_dir, CHECK_RESULT="failure", CHECK_VERDICT="failed",
                            CHECK_DETAILS=" FAIL x", CHECK_WARNINGS="scripts/check.py\n WARN y",
                            PREPARE_WARNINGS=" WARN commit 1234567 (author): z",
                            CHECKOUT_OUTCOME="skipped", DEPENDENCIES_OUTCOME="skipped", CREDENTIALS="skipped")
    assert result.returncode == 1
    assert read(tmp_path / "runner" / "hack-check-warnings.txt") == (
        "Personal data\n WARN commit 1234567 (author): z\n\nscripts/check.py\n WARN y"
    )


def test_publish_with_only_personal_data_warnings(tmp_path, bin_dir):
    make_app(tmp_path)
    run_publish(tmp_path, bin_dir, CHECK_RESULT="failure", CHECK_VERDICT="failed", CHECK_DETAILS=" FAIL x",
                PREPARE_WARNINGS=" WARN commit 1234567 (author): z", CHECKOUT_OUTCOME="skipped",
                DEPENDENCIES_OUTCOME="skipped", CREDENTIALS="skipped")
    assert read(tmp_path / "runner" / "hack-check-warnings.txt") == "Personal data\n WARN commit 1234567 (author): z"


# ---- prepare: committed keys (gitleaks stubbed; secret_scan.py has its own tests) ----

KEY_FINDING = json.dumps([{"RuleID": "generic-api-key", "File": "weather.py", "StartLine": 4, "Commit": "",
                           "Secret": "REDACTED", "Match": "REDACTED"}])


def test_prepare_runs_the_secret_check_on_every_push(tmp_path, bin_dir):
    app = make_app(tmp_path)
    result, output, _ = run_check_script(tmp_path, bin_dir, app, "prepare.sh")
    assert result.returncode == 0, result.stderr
    assert "ready=true" in output and "verdict" not in output
    gitleaks = next(line for line in read(tmp_path / "calls.log").splitlines() if line.startswith("gitleaks "))
    assert gitleaks.startswith("gitleaks dir ") and "--redact" in gitleaks
    assert not (tmp_path / "runner" / "hack-secrets").exists()


def test_prepare_refuses_a_committed_key_as_a_code_error(tmp_path, bin_dir):
    app = make_app(tmp_path)
    result, output, _ = run_check_script(tmp_path, bin_dir, app, "prepare.sh",
                                         STUB_GITLEAKS_REPORT=KEY_FINDING, STUB_GITLEAKS_EXIT="3")
    assert result.returncode == 0, result.stderr
    assert "ready=true" in output and output.count("verdict=failed") == 1
    details = output_value(output, "details")
    assert details.startswith(" FAIL a value that looks like a key was committed in weather.py:4 (gitleaks rule "
                              "generic-api-key); remove it, rotate the key, use app_secrets\n")
    assert "Rotate the key at its provider first" in details
    assert not (tmp_path / "runner" / "hack-secrets").exists()


def test_prepare_reports_a_committed_key_even_with_nothing_to_publish(tmp_path, bin_dir):
    app = make_app(tmp_path, manifest=PLACEHOLDER)
    result, output, summary = run_check_script(tmp_path, bin_dir, app, "prepare.sh",
                                               STUB_GITLEAKS_REPORT=KEY_FINDING, STUB_GITLEAKS_EXIT="3")
    assert result.returncode == 0, result.stderr
    assert "ready=true" in output and "verdict=failed" in output
    assert "Nothing to publish yet" not in summary


def test_prepare_lists_personal_data_and_a_committed_key_together(tmp_path, bin_dir):
    app = make_app(tmp_path)
    (app / "notes.md").write_text(f"Ask {PERSONAL}\n")
    result, output, _ = run_check_script(tmp_path, bin_dir, app, "prepare.sh",
                                         STUB_GITLEAKS_REPORT=KEY_FINDING, STUB_GITLEAKS_EXIT="3")
    details = output_value(output, "details")
    assert " FAIL notes.md:1 contains the email address" in details
    assert " FAIL a value that looks like a key was committed in weather.py:4" in details


def test_prepare_secret_check_crash_fails_without_verdict(tmp_path, bin_dir):
    app = make_app(tmp_path)
    result, output, _ = run_check_script(tmp_path, bin_dir, app, "prepare.sh", STUB_GITLEAKS_EXIT="1")
    assert result.returncode == 1
    assert "verdict" not in output and "ready" not in output
    assert "The secret check could not run (exit 2)" in result.stderr


def test_prepare_uses_a_test_gitleaks_only_outside_github_actions():
    script = (CHECK_ACTION_DIR / "scripts" / "prepare.sh").read_text()
    assert 'if [ "${GITHUB_ACTIONS:-}" != "true" ] && [ -n "${HACK_GITLEAKS_BIN:-}" ]; then' in script


# ---- the skipped job: why nothing is published ----

GUARD = ("github.event_name == 'push' && github.ref == 'refs/heads/main' && github.repository_owner == 'Pippa-Labs' "
         "&& startsWith(github.event.repository.name, 'pippa-hack-') && !github.event.repository.fork "
         "&& github.repository != 'Pippa-Labs/pippa-hack-template' "
         "&& github.repository != 'Pippa-Labs/pippa-hack-template-public' "
         "&& github.triggering_actor == github.actor")
PUSHED = {"EVENT_NAME": "push", "REF": "refs/heads/main", "OWNER": "Pippa-Labs",
          "REPOSITORY": "Pippa-Labs/pippa-hack-smoke", "REPOSITORY_NAME": "pippa-hack-smoke", "FORK": "false",
          "ACTOR": "tide-watcher", "TRIGGERING_ACTOR": "tide-watcher"}


def skipped_reason(tmp_path, **changes):
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "reusable-hack-publish.yml").read_text())
    step = workflow["jobs"]["skipped"]["steps"][0]
    summary = tmp_path / "skipped-summary.md"
    summary.write_text("")
    env = {"PATH": os.environ["PATH"], "GITHUB_STEP_SUMMARY": str(summary), **PUSHED, **changes}
    result = subprocess.run(["bash", "-c", step["run"]], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return summary.read_text()


@pytest.mark.parametrize("changes, reason", [
    ({"EVENT_NAME": "workflow_dispatch"}, "only a push to main publishes; this run was started by workflow_dispatch."),
    ({"EVENT_NAME": "pull_request", "REF": "refs/pull/1/merge"},
     "only a push to main publishes; this run was started by pull_request."),
    ({"REF": "refs/heads/feature"}, "only a push to main publishes; this push was to refs/heads/feature."),
    ({"OWNER": "someone-else", "REPOSITORY": "someone-else/pippa-hack-smoke"},
     "only repositories of Pippa-Labs publish; this one belongs to someone-else."),
    ({"REPOSITORY_NAME": "pippa_agent", "REPOSITORY": "Pippa-Labs/pippa_agent"},
     "only pippa-hack-* app repositories publish; this is pippa_agent."),
    ({"FORK": "true"}, "a fork never publishes; only the app repository Pippa Labs created does."),
    ({"REPOSITORY": "Pippa-Labs/pippa-hack-template", "REPOSITORY_NAME": "pippa-hack-template"},
     "this is the app template; the repositories created from it publish."),
    ({"REPOSITORY": "Pippa-Labs/pippa-hack-template-public", "REPOSITORY_NAME": "pippa-hack-template-public"},
     "this is the app template; the repositories created from it publish."),
    ({"TRIGGERING_ACTOR": "night-owl-7"},
     "this re-run was started by night-owl-7, not by tide-watcher, who pushed; only the account that pushed can "
     "re-run a publish. Push again instead."),
    ({}, "this run does not meet the conditions for publishing."),
])
def test_the_skipped_job_writes_one_line_saying_why(tmp_path, changes, reason):
    assert skipped_reason(tmp_path, **changes) == f"Not published: {reason}\n"


def test_the_skipped_job_never_echoes_markup_from_a_ref(tmp_path):
    summary = skipped_reason(tmp_path, REF="refs/heads/<img src=x>`$(id)`")
    assert summary == "Not published: only a push to main publishes; this push was to refs/heads/?img?src?x????id??.\n"


def test_the_skipped_job_runs_exactly_when_prepare_does_not():
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "reusable-hack-publish.yml").read_text())
    assert workflow["jobs"]["prepare"]["if"] == GUARD
    assert workflow["jobs"]["skipped"]["if"] == f"${{{{ !({GUARD}) }}}}"
    assert workflow["jobs"]["skipped"]["permissions"] == {}


def test_the_skipped_job_never_echoes_markup_from_an_actor(tmp_path):
    summary = skipped_reason(tmp_path, TRIGGERING_ACTOR="<b>x</b>`$(id)`")
    assert summary.startswith("Not published: this re-run was started by ?b?x?/b????id??, not by tide-watcher,")
