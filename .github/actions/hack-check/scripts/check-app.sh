#!/usr/bin/env bash
# Installs the app's development requirements from PyPI, runs the app's scripts/check.py
# and writes the verdict. After a passed check, runs scripts/routing_check.py and writes the
# non-blocking warnings of both; they never change the verdict. This is the first step that
# runs app code. A failed check exits 0 so the outputs always reach the job; the job's last
# step fails on verdict=failed. Exits 1 only when the step itself cannot run the check.
set -euo pipefail

LOG_SCOPE="hack-check/check-app"

ACTION_DIR="${GITHUB_ACTION_PATH}"
COMMON_DIR="${ACTION_DIR}/../../scripts/common"
HACK_DIR="${ACTION_DIR}/../../scripts/hack"

# shellcheck source=../../../scripts/common/logger.sh
source "${COMMON_DIR}/logger.sh"
# shellcheck source=../../../scripts/common/error-handler.sh
source "${COMMON_DIR}/error-handler.sh"
# shellcheck source=verdict.sh
source "${ACTION_DIR}/scripts/verdict.sh"

CREDENTIAL_VARIABLES=(AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN)
# Set only in a job with `id-token: write`; the app's code never runs in one.
OIDC_VARIABLES=(ACTIONS_ID_TOKEN_REQUEST_URL ACTIONS_ID_TOKEN_REQUEST_TOKEN)
INSTALL_TIMEOUT=240
CHECK_TIMEOUT=300
ROUTING_TIMEOUT=60
TIMEOUT_STATUS=124
LOG_FILE="${RUNNER_TEMP}/check.log"
ROUTING_LOG_FILE="${RUNNER_TEMP}/routing-check.log"

require_no_credentials() {
  local name
  for name in "${CREDENTIAL_VARIABLES[@]}"; do
    if [ -n "${!name:-}" ]; then
      log_error "${name} is still set; the app's code does not run with credentials"
      exit 1
    fi
    unset "$name"
  done
  for name in "${OIDC_VARIABLES[@]}"; do
    if [ -n "${!name:-}" ]; then
      log_error "${name} is set; the app's code never runs in a job that can request an OIDC token"
      exit 1
    fi
  done
}

failure_details() {
  local status="$1" details
  if [ "$status" -eq "$TIMEOUT_STATUS" ]; then
    echo "The check did not finish within its time limit. A test or an import is probably waiting forever, for example on a network call."
    return 0
  fi
  details="$(awk '/^ ?FAIL /{print; f=1; next} f && /^       [^ ]/{print; next} {f=0} /^(FAILED|ERROR) /{print}' "$LOG_FILE")"
  [ -n "$details" ] || details="$(tail -n 40 "$LOG_FILE")"
  printf '%s' "$details"
}

run_routing_check() {
  local status=0
  timeout "$ROUTING_TIMEOUT" python scripts/routing_check.py > "$ROUTING_LOG_FILE" 2>&1 || status=$?
  cat "$ROUTING_LOG_FILE" >&2
  log_info "Routing check finished (exit ${status}); it does not change the verdict"
  echo "$status"
}

write_warnings() {
  local routing_status="$1" summary
  if ! summary="$(python -I "${HACK_DIR}/check_warnings.py" \
    "--check-log=${LOG_FILE}" "--routing-log=${ROUTING_LOG_FILE}" \
    "--routing-status=${routing_status}")"; then
    log_warn "The warnings could not be summarised; the commit comment has none"
    return 0
  fi
  if [ -n "$summary" ]; then
    verdict_warnings "$summary"
    log_warn "The check passed with warnings; they are shown on the commit"
  fi
}

main() {
  require_no_credentials

  local status=0
  : > "$LOG_FILE"
  timeout "$INSTALL_TIMEOUT" python -m pip install --disable-pip-version-check --no-input \
    -r requirements-dev.txt 2>&1 | tee "$LOG_FILE" || status=$?
  if [ "$status" -eq 0 ]; then
    timeout "$CHECK_TIMEOUT" python scripts/check.py 2>&1 | tee -a "$LOG_FILE" || status=$?
  fi
  if [ "$status" -eq 0 ]; then
    : > "$ROUTING_LOG_FILE"
    write_warnings "$(run_routing_check)"
    verdict_passed
    exit 0
  fi

  verdict_failed "$(failure_details "$status")"
  log_warn "The check failed (exit ${status}); the job fails at its last step"
}

main "$@"
