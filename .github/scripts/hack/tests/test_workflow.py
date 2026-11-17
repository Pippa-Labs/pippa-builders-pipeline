"""Structure of reusable-hack-publish.yml: who may run it, job permissions, step order and references."""

import re

import pytest
import yaml

from conftest import REPO

WORKFLOW = REPO / ".github" / "workflows" / "reusable-hack-publish.yml"
OWN_PREFIX = "Pippa-Labs/pippa-builders-pipeline/.github/actions/"
SHA_PIN = re.compile(r"^[^@]+@[0-9a-f]{40}$")
GUARD = ("github.event_name == 'push' && github.ref == 'refs/heads/main' && github.repository_owner == 'Pippa-Labs' "
         "&& startsWith(github.event.repository.name, 'pippa-hack-') && !github.event.repository.fork "
         "&& github.repository != 'Pippa-Labs/pippa-hack-template' "
         "&& github.repository != 'Pippa-Labs/pippa-hack-template-public'")
HANDOVER_PATH = "${{ runner.temp }}/hack-sdk-handover"
HANDOVER_KEY = "pippa-hack-sdk-${{ github.run_id }}"


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def steps(workflow, job):
    return workflow["jobs"][job]["steps"]


def step_names(workflow, job):
    return [step.get("with", {}).get("mode") or (step["uses"].split("@")[0] if "uses" in step else step["id"])
            for step in steps(workflow, job)]


def all_uses():
    workflows = [WORKFLOW, *sorted((REPO / ".github" / "actions").glob("*/action.yml"))]
    for path in workflows:
        for match in re.finditer(r"^\s*(?:-\s*)?uses:\s*(\S+)", path.read_text(), re.MULTILINE):
            yield match.group(1)


# ---- who may run it ----

def test_jobs_and_their_order(workflow):
    assert list(workflow["jobs"]) == ["prepare", "sdk", "check", "publish", "skipped"]
    assert workflow["jobs"]["sdk"]["needs"] == "prepare"
    assert workflow["jobs"]["check"]["needs"] == ["prepare", "sdk"]
    assert workflow["jobs"]["publish"]["needs"] == ["prepare", "sdk", "check"]
    assert "needs" not in workflow["jobs"]["skipped"]


def test_only_a_push_to_main_of_a_pippa_hack_repository_runs_the_first_job(workflow):
    assert workflow["jobs"]["prepare"]["if"] == GUARD


def test_every_other_job_depends_on_the_first(workflow):
    for name in ("sdk", "check", "publish"):
        needs = workflow["jobs"][name]["needs"]
        assert "prepare" in ([needs] if isinstance(needs, str) else needs)
    assert "needs.prepare.result != 'skipped'" in workflow["jobs"]["publish"]["if"]


def test_the_skipped_job_is_the_exact_opposite_and_has_no_permissions(workflow):
    skipped = workflow["jobs"]["skipped"]
    assert skipped["if"] == f"${{{{ !({GUARD}) }}}}"
    assert skipped["permissions"] == {}
    assert [step.get("uses") for step in skipped["steps"]] == [None]
    env = skipped["steps"][0]["env"]
    assert env["REPOSITORY_NAME"] == "${{ github.event.repository.name }}"
    assert "${{" not in skipped["steps"][0]["run"], "values reach the script through env only"
    assert "GITHUB_STEP_SUMMARY" in skipped["steps"][0]["run"]


# ---- permissions: least privilege, and no OIDC where app code runs ----

def test_permissions_of_every_job(workflow):
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["prepare"]["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["sdk"]["permissions"] == {"id-token": "write"}
    assert workflow["jobs"]["check"]["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["publish"]["permissions"] == {"contents": "write", "id-token": "write",
                                                          "statuses": "write"}
    assert workflow["jobs"]["skipped"]["permissions"] == {}


def test_only_the_check_job_runs_app_code_and_it_cannot_request_a_token(workflow):
    assert "id-token" not in workflow["jobs"]["check"]["permissions"]
    assert "check" in step_names(workflow, "check")
    for job in ("prepare", "sdk", "publish"):
        assert "check" not in step_names(workflow, job), job
        for step in steps(workflow, job):
            assert "pip install" not in step.get("run", ""), job


def test_the_sdk_job_checks_out_nothing(workflow):
    assert not any(ref.startswith("actions/checkout@") for ref in
                   (step.get("uses", "") for step in steps(workflow, "sdk")))
    assert step_names(workflow, "sdk") == ["actions/setup-python", "aws-actions/configure-aws-credentials",
                                           "sdk-fetch", "actions/cache/save"]


def test_sdk_role_and_session(workflow):
    aws = steps(workflow, "sdk")[1]["with"]
    assert aws["role-to-assume"] == "arn:aws:iam::780886633355:role/PippaHackSdkReadRole"
    assert aws["aws-region"] == "eu-west-2"
    assert aws["role-duration-seconds"] == 900


def test_the_sdk_job_is_never_in_an_environment(workflow):
    # PippaHackSdkReadRole trusts the `ref:refs/heads/main` subject, which an environment would change.
    assert "environment" not in workflow["jobs"]["sdk"]


def test_the_sdk_reaches_the_check_job_as_a_cache_entry_of_this_run_never_an_artifact(workflow):
    save = steps(workflow, "sdk")[-1]
    restore = next(step for step in steps(workflow, "check") if "cache/restore" in step.get("uses", ""))
    assert save["with"] == {"path": HANDOVER_PATH, "key": HANDOVER_KEY}
    assert restore["with"] == {"path": HANDOVER_PATH, "key": HANDOVER_KEY, "fail-on-cache-miss": True}
    assert "upload-artifact" not in WORKFLOW.read_text() and "download-artifact" not in WORKFLOW.read_text()


def test_check_job_order_installs_the_sdk_before_any_app_code(workflow):
    assert step_names(workflow, "check") == ["actions/checkout", "actions/setup-python", "actions/cache/restore",
                                             "sdk-install", "check", "verdict"]
    assert workflow["jobs"]["check"]["if"] == "needs.sdk.result == 'success'"


def test_the_sdk_job_runs_only_when_prepare_found_something_to_publish(workflow):
    assert workflow["jobs"]["sdk"]["if"] == (
        "needs.prepare.outputs.ready == 'true' && needs.prepare.outputs.verdict != 'failed'"
    )


def test_last_steps_fail_their_job_on_a_failed_verdict(workflow):
    for job, step_id in (("prepare", "prepare"), ("check", "check")):
        last = steps(workflow, job)[-1]
        assert f"steps.{step_id}.outputs.verdict == 'failed'" in last["if"]
        assert "!cancelled()" in last["if"]
        assert last["run"].strip().endswith("exit 1")
        assert "uses" not in last


# ---- prepare ----

def test_prepare_checks_out_the_whole_history_for_the_commit_checks(workflow):
    checkout = steps(workflow, "prepare")[0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["fetch-depth"] == 0
    assert checkout["with"]["persist-credentials"] is False


def test_personal_data_and_secret_checks_run_on_every_push_before_app_code(workflow):
    prepare = steps(workflow, "prepare")[2]
    assert prepare["with"]["mode"] == "prepare"
    assert "if" not in prepare


def test_prepare_outputs(workflow):
    assert workflow["jobs"]["prepare"]["outputs"] == {
        "ready": "${{ steps.prepare.outputs.ready }}",
        "verdict": "${{ steps.prepare.outputs.verdict }}",
        "details": "${{ steps.prepare.outputs.details }}",
        "warnings": "${{ steps.prepare.outputs.warnings }}",
    }


def test_checkouts_never_keep_credentials(workflow):
    for job in workflow["jobs"]:
        for step in workflow["jobs"][job]["steps"]:
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False


# ---- publish ----

def publish_step(workflow):
    return next(step for step in steps(workflow, "publish") if step.get("with", {}).get("mode") == "publish")


def test_publish_job_environment_and_role(workflow):
    publish = workflow["jobs"]["publish"]
    assert publish["environment"] == "pippa-publish"
    aws = next(step for step in publish["steps"] if step.get("id") == "aws")
    assert aws["with"]["role-to-assume"] == "arn:aws:iam::780886633355:role/PippaHackCiRole"
    assert aws["with"]["aws-region"] == "eu-west-2"


def test_publish_runs_after_a_failed_prepare_but_not_when_there_is_nothing_to_publish(workflow):
    condition = workflow["jobs"]["publish"]["if"]
    assert "!cancelled()" in condition
    assert "needs.prepare.result != 'skipped'" in condition
    assert "needs.prepare.outputs.ready != 'false'" in condition


def test_publish_counts_platform_failures_of_every_earlier_job(workflow):
    condition = next(step for step in steps(workflow, "publish") if step.get("id") == "aws")["if"]
    assert "needs.check.result == 'success'" in condition
    assert "needs.sdk.result == 'failure'" in condition
    assert "(needs.prepare.result == 'failure' && needs.prepare.outputs.verdict != 'failed')" in condition
    assert "(needs.check.result == 'failure' && needs.check.outputs.verdict != 'failed')" in condition


def test_a_failed_prepare_counts_as_a_failed_check(workflow):
    assert publish_step(workflow)["with"]["check_result"] == (
        "${{ needs.prepare.result == 'success' && needs.check.result || 'failure' }}"
    )


def test_verdicts_details_and_warnings_reach_the_publish_step(workflow):
    inputs = publish_step(workflow)["with"]
    assert inputs["check_verdict"] == "${{ needs.prepare.outputs.verdict || needs.check.outputs.verdict }}"
    assert inputs["check_details"] == "${{ needs.prepare.outputs.details || needs.check.outputs.details }}"
    assert inputs["check_warnings"] == "${{ needs.check.outputs.warnings }}"
    assert inputs["prepare_warnings"] == "${{ needs.prepare.outputs.warnings }}"
    assert workflow["jobs"]["check"]["outputs"]["warnings"] == "${{ steps.check.outputs.warnings }}"


# ---- references ----

def test_no_private_pipeline_in_sources():
    for path in [WORKFLOW, *(REPO / ".github").rglob("*.sh"), *(REPO / ".github").rglob("action.yml")]:
        assert "pippa-common-pipeline" not in path.read_text(), path


def test_actions_are_pinned():
    for ref in all_uses():
        if ref.startswith(OWN_PREFIX):
            assert ref.endswith("@main"), ref
        else:
            assert SHA_PIN.match(ref), ref


def test_only_log_level_input(workflow):
    trigger = workflow.get("on", workflow.get(True))
    assert set(trigger["workflow_call"]["inputs"]) == {"log_level"}


def test_no_expression_is_expanded_inside_a_script(workflow):
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            assert "${{" not in step.get("run", ""), step.get("name")
