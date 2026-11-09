#!/usr/bin/env bash
# Writes a platform error report when publish.py could not produce one.
#
# Args:
#   $1  report directory
#   $2  run URL
#   $3  reference shown to the user
set -euo pipefail

LOG_SCOPE="hack-publish/fallback-report"

COMMON_DIR="${GITHUB_ACTION_PATH}/../../scripts/common"

# shellcheck source=../../../scripts/common/logger.sh
source "${COMMON_DIR}/logger.sh"
# shellcheck source=../../../scripts/common/error-handler.sh
source "${COMMON_DIR}/error-handler.sh"

OUT_DIR="${1:?report directory is required}"
RUN_URL="${2:?run URL is required}"
REFERENCE="${3:?reference is required}"

MESSAGE="PLATFORM_ERROR: the app was not published because of a problem on the Pippa platform, not in the app. Reference: ${REFERENCE}

Do not change your code. Tell an organiser and give them the reference."

mkdir -p "$OUT_DIR"
printf 'infra' > "${OUT_DIR}/outcome"
jq -nc --arg description "Platform error, reference ${REFERENCE}" --arg url "$RUN_URL" \
  '{state: "failure", context: "pippa/publish", description: $description, target_url: $url}' \
  > "${OUT_DIR}/status.json"
jq -nc --arg body "$MESSAGE" '{body: $body}' > "${OUT_DIR}/comment.json"
printf '### Publish to Pippa\n\n%s\n' "$MESSAGE" > "${OUT_DIR}/summary.md"
log_warn "Fallback platform error report written"
