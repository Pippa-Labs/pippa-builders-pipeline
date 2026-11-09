"""Structure of reusable-hack-publish.yml: job permissions, step order and references."""

import re

import pytest
import yaml

from conftest import REPO

WORKFLOW = REPO / ".github" / "workflows" / "reusable-hack-publish.yml"
OWN_PREFIX = "Pippa-Labs/pippa-builders-pipeline/.github/actions/"
SHA_PIN = re.compile(r"^[^@]+@[0-9a-f]{40}$")


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def steps(workflow, job):
    return workflow["jobs"][job]["steps"]


def all_uses():
    workflows = [WORKFLOW, *sorted((REPO / ".github" / "actions").glob("*/action.yml"))]
    for path in workflows:
        for match in re.finditer(r"^\s*(?:-\s*)?uses:\s*(\S+)", path.read_text(), re.MULTILINE):
            yield match.group(1)


def test_check_job_permissions(workflow):
    assert workflow["jobs"]["check"]["permissions"] == {"contents": "read", "id-token": "write"}
    assert workflow["permissions"] == {"contents": "read"}


def test_check_job_order_keeps_credentials_away_from_app_code(workflow):
    names = [step.get("with", {}).get("mode") or (step["uses"].split("@")[0].split("/")[-1]
             if "uses" in step else step["id"]) for step in steps(workflow, "check")]
    assert names == ["checkout", "setup-python", "prepare", "configure-aws-credentials", "sdk", "clear", "check",
                     "verdict"]
    clear = steps(workflow, "check")[5]
    assert clear["if"].replace("${{", "").strip().startswith("always()")


def test_refused_dependencies_stop_before_credentials(workflow):
    for index in (3, 4, 6):
        assert "steps.prepare.outputs.verdict != 'failed'" in steps(workflow, "check")[index]["if"]


def test_last_step_fails_the_job_on_a_failed_verdict(workflow):
    last = steps(workflow, "check")[-1]
    assert "steps.prepare.outputs.verdict == 'failed' || steps.check.outputs.verdict == 'failed'" in last["if"]
    assert "!cancelled()" in last["if"]
    assert last["run"].strip().endswith("exit 1")
    assert "uses" not in last


def test_sdk_role_and_session(workflow):
    aws = steps(workflow, "check")[3]["with"]
    assert aws["role-to-assume"] == "arn:aws:iam::780886633355:role/PippaHackSdkReadRole"
    assert aws["aws-region"] == "eu-west-2"
    assert aws["role-duration-seconds"] == 900


def test_publish_job_environment_and_role(workflow):
    publish = workflow["jobs"]["publish"]
    assert publish["environment"] == "pippa-publish"
    assert publish["permissions"] == {"contents": "write", "id-token": "write", "statuses": "write"}
    aws = next(step for step in publish["steps"] if step.get("id") == "aws")
    assert aws["with"]["role-to-assume"] == "arn:aws:iam::780886633355:role/PippaHackCiRole"
    assert aws["with"]["aws-region"] == "eu-west-2"


def test_publish_counts_platform_failures_of_the_check(workflow):
    aws = next(step for step in steps(workflow, "publish") if step.get("id") == "aws")
    assert "needs.check.result == 'failure' && needs.check.outputs.verdict != 'failed'" in aws["if"]


def test_warnings_reach_the_publish_step(workflow):
    assert workflow["jobs"]["check"]["outputs"]["warnings"] == "${{ steps.check.outputs.warnings }}"
    publish = next(step for step in steps(workflow, "publish") if step.get("with", {}).get("mode") == "publish")
    assert publish["with"]["check_warnings"] == "${{ needs.check.outputs.warnings }}"


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
