#!/usr/bin/env bash
# Posts the commit status and the commit comment built by publish.py and writes the step summary.
#
# Args:
#   $1  directory with status.json, comment.json and summary.md
#
# Env:
#   GH_TOKEN  token with statuses write and contents read on the repository
set -euo pipefail

LOG_SCOPE="hack-publish/report"

COMMON_DIR="${GITHUB_ACTION_PATH}/../../scripts/common"

# shellcheck source=../../../scripts/common/logger.sh
source "${COMMON_DIR}/logger.sh"
# shellcheck source=../../../scripts/common/error-handler.sh
source "${COMMON_DIR}/error-handler.sh"

OUT_DIR="${1:?report directory is required}"
: "${GH_TOKEN:?token is required}"

post_status() {
  if gh api --method POST "repos/${GITHUB_REPOSITORY}/statuses/${GITHUB_SHA}" \
    --input "${OUT_DIR}/status.json" --silent; then
    log_info "Commit status pippa/publish set on ${GITHUB_SHA}"
  else
    log_warn "Could not set the commit status on ${GITHUB_SHA}"
  fi
}

post_comment() {
  if gh api --method POST "repos/${GITHUB_REPOSITORY}/commits/${GITHUB_SHA}/comments" \
    --input "${OUT_DIR}/comment.json" --silent; then
    log_info "Comment posted on ${GITHUB_SHA}"
  else
    log_warn "Could not comment on ${GITHUB_SHA}"
  fi
}

write_summary() {
  cat "${OUT_DIR}/summary.md" >> "$GITHUB_STEP_SUMMARY"
}

write_summary
post_status
post_comment
