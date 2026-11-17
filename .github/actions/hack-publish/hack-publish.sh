#!/usr/bin/env bash
# Publishes the app, reports the result on the commit and fails the job on any error.
#
# A run somebody other than the account that pushed started (a re-run) only fails the job:
# publish.py stops it before any token with exit status RERUN_REFUSED, and nothing is posted on
# the commit or counted as a platform error.
#
# Env:
#   APP_DIR               directory of the checked out app repository
#   CHECK_RESULT          result of the check job
#   CHECK_VERDICT         verdict written by the check
#   CHECK_DETAILS         failing lines of the check
#   CHECK_WARNINGS        non-blocking warnings of a passed check
#   PREPARE_WARNINGS      non-blocking warnings of the personal data check, shown first
#   CHECKOUT_OUTCOME      outcome of the app checkout step
#   PYTHON_OUTCOME        outcome of the Python setup step
#   DEPENDENCIES_OUTCOME  outcome of the dependency install step
#   CREDENTIALS           outcome of the AWS role assumption step
#   REPORT_TOKEN          token for the commit status and the commit comment
#   OLD_WORKFLOW          true for the workflow made before the check was split into the prepare,
#                         SDK and check jobs (remove after 2026-12-20): a check that failed without
#                         a verdict is then REFUSED, push a new commit, not a platform error
set -euo pipefail

LOG_SCOPE="hack-publish"

ACTION_DIR="${GITHUB_ACTION_PATH}"
COMMON_DIR="${ACTION_DIR}/../../scripts/common"
HACK_DIR="${ACTION_DIR}/../../scripts/hack"

# shellcheck source=../../scripts/common/logger.sh
source "${COMMON_DIR}/logger.sh"
# shellcheck source=../../scripts/common/error-handler.sh
source "${COMMON_DIR}/error-handler.sh"

APP_DIR="${APP_DIR:-app}"
CHECK_RESULT="${CHECK_RESULT:-}"
CHECK_VERDICT="${CHECK_VERDICT:-}"
CHECK_DETAILS="${CHECK_DETAILS:-}"
CHECK_WARNINGS="${CHECK_WARNINGS:-}"
PREPARE_WARNINGS="${PREPARE_WARNINGS:-}"
CHECKOUT_OUTCOME="${CHECKOUT_OUTCOME:-skipped}"
PYTHON_OUTCOME="${PYTHON_OUTCOME:-skipped}"
DEPENDENCIES_OUTCOME="${DEPENDENCIES_OUTCOME:-skipped}"
CREDENTIALS="${CREDENTIALS:-skipped}"
OLD_WORKFLOW="${OLD_WORKFLOW:-false}"
: "${REPORT_TOKEN:?token is required}"

OUT_DIR="${RUNNER_TEMP}/hack-publish"
DETAILS_FILE="${RUNNER_TEMP}/hack-check-details.txt"
WARNINGS_FILE="${RUNNER_TEMP}/hack-check-warnings.txt"
RUN_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
REFERENCE="gh-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
# publish.py's exit status when the account that pushed did not start the run (RERUN_REFUSED_EXIT).
RERUN_REFUSED=77

setup_outcome() {
  if [ "$CHECKOUT_OUTCOME" = "success" ] && [ "$PYTHON_OUTCOME" = "success" ] \
    && [ "$DEPENDENCIES_OUTCOME" = "success" ]; then
    echo success
  else
    log_warn "Setup outcomes: checkout=${CHECKOUT_OUTCOME} python=${PYTHON_OUTCOME} dependencies=${DEPENDENCIES_OUTCOME}"
    echo failure
  fi
}

all_warnings() {
  if [ -n "$PREPARE_WARNINGS" ] && [ -n "$CHECK_WARNINGS" ]; then
    printf 'Personal data\n%s\n\n%s' "$PREPARE_WARNINGS" "$CHECK_WARNINGS"
  elif [ -n "$PREPARE_WARNINGS" ]; then
    printf 'Personal data\n%s' "$PREPARE_WARNINGS"
  else
    printf '%s' "$CHECK_WARNINGS"
  fi
}

run_publisher() {
  local setup="$1"
  local args=(
    "--app-dir=${APP_DIR}"
    "--run-url=${RUN_URL}"
    "--reference=${REFERENCE}"
    "--out-dir=${OUT_DIR}"
    "--check-result=${CHECK_RESULT}"
    "--check-verdict=${CHECK_VERDICT}"
    "--check-details-file=${DETAILS_FILE}"
    "--check-warnings-file=${WARNINGS_FILE}"
    "--credentials=${CREDENTIALS}"
    "--setup=${setup}"
  )
  # Remove after 2026-12-20, with the action's input old_workflow.
  if [ "$OLD_WORKFLOW" = "true" ]; then
    args+=("--old-workflow")
  fi
  printf '%s' "$CHECK_DETAILS" > "$DETAILS_FILE"
  all_warnings > "$WARNINGS_FILE"
  env -u REPORT_TOKEN python "${HACK_DIR}/publish.py" "${args[@]}"
}

main() {
  rm -rf "$OUT_DIR"
  mkdir -p "$OUT_DIR"

  local setup status=0
  setup="$(setup_outcome)"

  run_publisher "$setup" || status=$?
  if [ "$status" -eq "$RERUN_REFUSED" ]; then
    log_error "Not published: only the account that pushed can re-run a publish; nothing is reported on the commit"
    exit 1
  fi
  if [ "$status" -ne 0 ]; then
    log_error "The publisher module failed; reporting a platform error"
    bash "${ACTION_DIR}/scripts/fallback-report.sh" "$OUT_DIR" "$RUN_URL" "$REFERENCE"
  fi

  local outcome
  outcome="$(<"${OUT_DIR}/outcome")"
  log_info "Outcome: ${outcome}"

  if [ "$outcome" = "infra" ] && [ "$CREDENTIALS" = "success" ]; then
    bash "${ACTION_DIR}/scripts/alert.sh"
  fi

  GH_TOKEN="$REPORT_TOKEN" bash "${ACTION_DIR}/scripts/report.sh" "$OUT_DIR"

  if [ "$outcome" != "success" ]; then
    log_error "Not published (${outcome} error); the commit comment has the details"
    exit 1
  fi
}

main "$@"
