"""Structure of reusable-hack-publish.yml: who may run it, job permissions, step order and references."""

import re

import pytest
import yaml

from conftest import REPO
from workflow_expressions import Status, runs

WORKFLOW = REPO / ".github" / "workflows" / "reusable-hack-publish.yml"
OWN_PREFIX = "Pippa-Labs/pippa-builders-pipeline/.github/actions/"
SHA_PIN = re.compile(r"^[^@]+@[0-9a-f]{40}$")
GUARD = ("github.event_name == 'push' && github.ref == 'refs/heads/main' && github.repository_owner == 'Pippa-Labs' "
         "&& startsWith(github.event.repository.name, 'pippa-hack-') && !github.event.repository.fork "
         "&& github.repository != 'Pippa-Labs/pippa-hack-template' "
         "&& github.repository != 'Pippa-Labs/pippa-hack-template-public' "
         "&& github.triggering_actor == github.actor")
HANDOVER_PATH = "${{ runner.temp }}/hack-sdk-handover"
HANDOVER_ENTRY = "pippa-hack-sdk-${{ github.run_id }}-${{ github.run_attempt }}"


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
    assert save["with"] == {"path": HANDOVER_PATH, "key": HANDOVER_ENTRY}
    assert restore["with"] == {"path": HANDOVER_PATH, "key": "${{ needs.sdk.outputs.handover_entry }}",
                               "fail-on-cache-miss": True}
    assert "upload-artifact" not in WORKFLOW.read_text() and "download-artifact" not in WORKFLOW.read_text()


def test_the_check_job_restores_the_entry_the_sdk_job_sealed_with_the_key_it_gets(workflow):
    # A cache entry cannot be replaced. "Re-run all jobs" seals with a new key, so it saves an entry
    # of its own attempt; "Re-run failed jobs" keeps the SDK job's outputs, entry name and key alike.
    assert workflow["jobs"]["sdk"]["outputs"]["handover_entry"] == HANDOVER_ENTRY
    assert "github.run_attempt" in HANDOVER_ENTRY
    restore = next(step for step in steps(workflow, "check") if "cache/restore" in step.get("uses", ""))
    install = next(step for step in steps(workflow, "check") if step.get("with", {}).get("mode") == "sdk-install")
    # Both come from the outputs of the same attempt of the SDK job, never from the check job's own attempt.
    assert restore["with"]["key"] == "${{ needs.sdk.outputs.handover_entry }}"
    assert install["with"]["handover_key"] == "${{ needs.sdk.outputs.handover_key }}"


def test_the_key_of_the_sealed_hand_over_goes_from_the_sdk_job_to_the_install_step_only(workflow):
    # The cache entry can be restored by a pull request's run, also from a fork; the job output
    # that carries the key cannot.
    assert workflow["jobs"]["sdk"]["outputs"] == {"handover_key": "${{ steps.fetch.outputs.handover_key }}",
                                                  "handover_entry": HANDOVER_ENTRY}
    fetch = next(step for step in steps(workflow, "sdk") if step.get("with", {}).get("mode") == "sdk-fetch")
    assert fetch["id"] == "fetch"
    for step in steps(workflow, "check"):
        if step.get("with", {}).get("mode") == "sdk-install":
            assert step["with"]["handover_key"] == "${{ needs.sdk.outputs.handover_key }}"
        else:
            assert "handover_key" not in yaml.safe_dump(step), step.get("name")
    for job in ("prepare", "publish", "skipped"):
        assert "handover_key" not in yaml.safe_dump(workflow["jobs"][job]), job


def test_only_the_install_step_of_the_check_action_receives_the_key():
    action = yaml.safe_load((REPO / ".github" / "actions" / "hack-check" / "action.yml").read_text())
    assert action["outputs"]["handover_key"]["value"] == "${{ steps.fetch.outputs.handover_key }}"
    with_key = [step["name"] for step in action["runs"]["steps"] if "inputs.handover_key" in yaml.safe_dump(step)]
    assert with_key == ["Install the Pippa SDK"]
    install = next(step for step in action["runs"]["steps"] if step["name"] == "Install the Pippa SDK")
    assert install["env"]["HANDOVER_KEY"] == "${{ inputs.handover_key }}"
    fetch = next(step for step in action["runs"]["steps"] if step["name"] == "Fetch the Pippa SDK")
    assert fetch["id"] == "fetch"


def test_check_job_order_installs_the_sdk_before_any_app_code(workflow):
    assert step_names(workflow, "check") == ["actions/checkout", "actions/setup-python", "actions/cache/restore",
                                             "sdk-install", "check", "verdict"]
    assert workflow["jobs"]["check"]["if"] == "needs.sdk.result == 'success' && github.triggering_actor == github.actor"


def test_the_sdk_job_runs_only_when_prepare_found_something_to_publish(workflow):
    assert workflow["jobs"]["sdk"]["if"] == (
        "needs.prepare.outputs.ready == 'true' && needs.prepare.outputs.verdict != 'failed' "
        "&& github.triggering_actor == github.actor"
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


def test_the_publish_step_says_it_is_not_the_old_workflow(workflow):
    """hack-publish takes a missing input as the workflow made before the split (remove after 2026-12-20)."""
    assert publish_step(workflow)["with"]["old_workflow"] == "false"


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


# ---- concurrency: a newer push cancels an older one, nothing else cancels a push ----

def test_only_a_push_cancels_a_run_and_only_another_push(workflow):
    concurrency = workflow["concurrency"]
    assert concurrency["group"] == (
        "hack-publish-${{ github.repository }}-${{ github.event_name == 'push' && 'push' || github.run_id }}"
    )
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'push' }}"
    for name, job_config in workflow["jobs"].items():
        assert "concurrency" not in job_config, name


def test_a_rerun_by_another_account_does_not_publish(workflow):
    assert workflow["jobs"]["prepare"]["if"].endswith(" && github.triggering_actor == github.actor")
    env = workflow["jobs"]["skipped"]["steps"][0]["env"]
    assert env["ACTOR"] == "${{ github.actor }}"
    assert env["TRIGGERING_ACTOR"] == "${{ github.triggering_actor }}"


# ---- re-runs: GitHub evaluates only the conditions of the jobs it runs again ----
#
# "Re-run failed jobs" and "Re-run this job" keep the results and outputs of the jobs that are not
# run again, so every job, and the step that assumes the publish role, checks who started the run.

PUSHER = "tide-watcher"
PUSH = {
    "github.event_name": "push", "github.ref": "refs/heads/main", "github.repository_owner": "Pippa-Labs",
    "github.event.repository.name": "pippa-hack-tides", "github.event.repository.fork": False,
    "github.repository": "Pippa-Labs/pippa-hack-tides", "github.actor": PUSHER, "github.triggering_actor": PUSHER,
}
#: What the first attempt left when every job passed; a re-run of one job keeps the others'.
PASSED = {
    "needs.prepare.result": "success", "needs.prepare.outputs.ready": "true", "needs.prepare.outputs.verdict": "",
    "needs.sdk.result": "success", "needs.check.result": "success", "needs.check.outputs.verdict": "passed",
}
#: What the first attempt left when the check job failed without a verdict (a platform error).
CHECK_BROKE = {**PASSED, "needs.check.result": "failure", "needs.check.outputs.verdict": ""}
NEEDS = {"prepare": (), "sdk": ("prepare",), "check": ("prepare", "sdk"), "publish": ("prepare", "sdk", "check")}


def started_by(account, first_attempt=PASSED):
    return {**PUSH, **first_attempt, "github.triggering_actor": account}


def job_runs(workflow, name, context):
    return runs(workflow["jobs"][name]["if"],
                context, Status.of(*(context[f"needs.{need}.result"] for need in NEEDS[name])))


def role_step_runs(workflow, context):
    aws = next(step for step in steps(workflow, "publish") if step.get("id") == "aws")
    return runs(aws["if"], context, Status.of(*(context[f"needs.{need}.result"] for need in NEEDS["publish"])))


def test_a_push_by_the_builder_runs_every_job_and_assumes_the_role(workflow):
    context = started_by(PUSHER)
    assert [name for name in NEEDS if job_runs(workflow, name, context)] == list(NEEDS)
    assert role_step_runs(workflow, context)
    assert not runs(workflow["jobs"]["skipped"]["if"], context)


@pytest.mark.parametrize("name", list(NEEDS))
def test_a_rerun_of_any_one_job_by_another_account_runs_nothing(workflow, name):
    assert not job_runs(workflow, name, started_by("org-writer"))
    assert job_runs(workflow, name, started_by(PUSHER)), "the pusher may re-run it"


def test_a_rerun_by_another_account_never_assumes_the_publish_role(workflow):
    for first_attempt in (PASSED, CHECK_BROKE):
        assert not role_step_runs(workflow, started_by("org-writer", first_attempt))
        assert role_step_runs(workflow, started_by(PUSHER, first_attempt))


def test_rerun_failed_jobs_by_another_account_after_a_platform_error_publishes_nothing(workflow):
    context = started_by("org-writer", CHECK_BROKE)
    assert not job_runs(workflow, "check", context)
    assert not job_runs(workflow, "publish", context)


def test_every_job_that_can_request_a_token_or_runs_app_code_checks_who_started_the_run(workflow):
    for name in ("sdk", "check", "publish"):
        assert "github.triggering_actor == github.actor" in workflow["jobs"][name]["if"], name
    aws = next(step for step in steps(workflow, "publish") if step.get("id") == "aws")
    assert "github.triggering_actor == github.actor" in aws["if"]
