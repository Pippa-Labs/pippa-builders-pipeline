#!/usr/bin/env bash
# Refuses personal data, decides whether there is an app to publish and refuses dependencies
# that do not come from PyPI. Runs before any app code and before any AWS credentials exist
# in the job.
#
# The personal data check always runs, also on a push with nothing to publish yet: an email
# address in a file, or on a commit of a public repository, is refused. A refusal there makes
# the run ready anyway, so the publish job reports it on the commit. Commit emails of a private
# repository are warnings.
#
# Outputs: ready; verdict=failed/details when something is refused; warnings. A refusal
# exits 0 so the outputs always reach the job; the job's last step fails on verdict=failed.
# Exits 1 only when a check itself could not run (platform error).
set -euo pipefail

LOG_SCOPE="hack-check/prepare"

ACTION_DIR="${GITHUB_ACTION_PATH}"
COMMON_DIR="${ACTION_DIR}/../../scripts/common"
HACK_DIR="${ACTION_DIR}/../../scripts/hack"

# shellcheck source=../../../scripts/common/logger.sh
source "${COMMON_DIR}/logger.sh"
# shellcheck source=../../../scripts/common/error-handler.sh
source "${COMMON_DIR}/error-handler.sh"
# shellcheck source=verdict.sh
source "${ACTION_DIR}/scripts/verdict.sh"

APP_DIR="${GITHUB_WORKSPACE:-$PWD}"
WARNINGS_FILE="${RUNNER_TEMP}/hack-prepare-warnings.txt"
FAILURES=""
REASONS=""

add_failures() {
  if [ -n "$FAILURES" ]; then
    FAILURES="${FAILURES}"$'\n'"$1"
  else
    FAILURES="$1"
  fi
}

check_personal_data() {
  local findings status=0
  : > "$WARNINGS_FILE"
  findings="$(python -I "${HACK_DIR}/personal_data.py" "--app-dir=${APP_DIR}" \
    "--warnings-file=${WARNINGS_FILE}")" || status=$?
  case "$status" in
    0)
      log_info "No personal data found"
      ;;
    1)
      printf '%s\n' "$findings"
      add_failures "$findings"
      log_warn "Personal data found; the job fails at its last step"
      ;;
    *)
      log_error "The personal data check could not run (exit ${status})"
      exit 1
      ;;
  esac
}

read_reasons() {
  local status=0
  REASONS="$(python -I "${HACK_DIR}/nothing_to_publish.py" "--manifest=${APP_DIR}/manifest.yaml")" || status=$?
  if [ "$status" -ne 0 ]; then
    log_error "The nothing-to-publish check could not run (exit ${status})"
    exit 1
  fi
}

check_dependencies() {
  local findings status=0
  findings="$(python -I "${HACK_DIR}/dependency_guard.py" "--app-dir=${APP_DIR}")" || status=$?
  case "$status" in
    0)
      log_info "Dependencies accepted"
      ;;
    1)
      printf '%s\n' "$findings"
      add_failures "$findings"
      log_warn "The dependencies were refused; the job fails at its last step"
      ;;
    *)
      log_error "The dependency check could not run (exit ${status})"
      exit 1
      ;;
  esac
}

write_warnings() {
  local warnings
  warnings="$(cat "$WARNINGS_FILE")"
  [ -n "$warnings" ] || return 0
  printf '%s\n' "$warnings"
  verdict_warnings "$warnings"
  printf '### Warnings\n\n```text\n%s\n```\n' "$warnings" >> "$GITHUB_STEP_SUMMARY"
  log_warn "Personal data warnings; they are shown on the commit of a published app"
}

main() {
  check_personal_data
  read_reasons
  write_warnings

  if [ -n "$REASONS" ] && [ -z "$FAILURES" ]; then
    echo "ready=false" >> "$GITHUB_OUTPUT"
    printf '### Nothing to publish yet\n\n%s\n' "$REASONS" >> "$GITHUB_STEP_SUMMARY"
    log_info "Nothing to publish yet: ${REASONS//$'\n'/ }"
    exit 0
  fi

  echo "ready=true" >> "$GITHUB_OUTPUT"
  if [ -z "$REASONS" ]; then
    check_dependencies
  else
    log_warn "Nothing to publish yet, but personal data was found; it is reported on the commit"
  fi
  if [ -n "$FAILURES" ]; then
    verdict_failed "$FAILURES"
  fi
}

main "$@"
