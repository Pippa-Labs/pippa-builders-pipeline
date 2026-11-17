#!/usr/bin/env bash
# Mode `sdk`, for the workflow made before the check was split into the prepare, SDK and check
# jobs. Remove after 2026-12-20, together with mode `clear` (clear-credentials.sh).
#
# A re-run of single jobs of a run made before then (GitHub allows it for 30 days) replays that
# workflow, whose check job calls this action @main with modes prepare, sdk, clear and check. Its
# check job holds the SDK read role and can request an OIDC token, so the SDK is never installed
# and the app's code never runs there. This step fails, so its "Check the app" step is skipped,
# and says what to do. It is not a platform error: the publish job of that workflow reports it
# as REFUSED (hack-publish's input old_workflow), and no InfraErrors is counted.
set -euo pipefail

LOG_SCOPE="hack-check/old-workflow"

ACTION_DIR="${GITHUB_ACTION_PATH}"
COMMON_DIR="${ACTION_DIR}/../../scripts/common"

# shellcheck source=../../../scripts/common/logger.sh
source "${COMMON_DIR}/logger.sh"
# shellcheck source=../../../scripts/common/error-handler.sh
source "${COMMON_DIR}/error-handler.sh"

TITLE="Push a new commit to publish"
MESSAGE="Not published: this run was made by an older version of the Pippa publish workflow, which can no longer publish, so re-running it will not help. Push a new commit to main to publish your app."

main() {
  # A workflow command whatever GITHUB_ACTIONS says: the annotation is the builder's answer.
  printf '::error title=%s::%s\n' "$TITLE" "$MESSAGE"
  printf '### %s\n\n%s\n' "$TITLE" "$MESSAGE" >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
  log_error_detail "$MESSAGE"
  exit 1
}

main "$@"
