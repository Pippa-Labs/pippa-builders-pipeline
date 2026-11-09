#!/usr/bin/env bash
# Decides whether there is an app to publish and refuses dependencies that do not come from PyPI.
# Runs before any app code and before any AWS credentials exist in the job.
#
# Outputs: ready, and verdict=failed/details when the dependencies are refused. A refusal
# exits 0 so the outputs always reach the job; the job's last step fails on verdict=failed.
# Exits 1 only when the check itself could not run (platform error).
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
PLACEHOLDER_PATTERN='^[[:space:]]*id:[[:space:]]*hack\.your_app_name_here[[:space:]]*$'

placeholder_app() {
  grep -qE "$PLACEHOLDER_PATTERN" "${APP_DIR}/manifest.yaml" 2>/dev/null
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
      verdict_failed "$findings"
      log_warn "The dependencies were refused; the job fails at its last step"
      ;;
    *)
      log_error "The dependency check could not run (exit ${status})"
      exit 1
      ;;
  esac
}

main() {
  if placeholder_app; then
    echo "ready=false" >> "$GITHUB_OUTPUT"
    printf '### Nothing to publish yet\n\nmanifest.yaml still has the placeholder app id hack.your_app_name_here. The next push with a real app id publishes.\n' >> "$GITHUB_STEP_SUMMARY"
    log_info "Placeholder app id; nothing to publish"
    exit 0
  fi
  echo "ready=true" >> "$GITHUB_OUTPUT"
  check_dependencies
}

main "$@"
