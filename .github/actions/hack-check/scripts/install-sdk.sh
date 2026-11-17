#!/usr/bin/env bash
# The check job: installs the Pippa SDK wheel the SDK job handed over, offline, with the hash of
# its hash-locked requirement, then removes the hand-over directory. Runs before any app code, in
# a job that has no credentials and cannot request an OIDC token.
set -euo pipefail

LOG_SCOPE="hack-check/install-sdk"

ACTION_DIR="${GITHUB_ACTION_PATH}"
COMMON_DIR="${ACTION_DIR}/../../scripts/common"

# shellcheck source=../../../scripts/common/logger.sh
source "${COMMON_DIR}/logger.sh"
# shellcheck source=../../../scripts/common/error-handler.sh
source "${COMMON_DIR}/error-handler.sh"

INSTALL_TIMEOUT=120
HANDOVER_DIR="${RUNNER_TEMP}/hack-sdk-handover"
PIN_FILE="${HANDOVER_DIR}/sdk.txt"
PIN_PATTERN='^pippa-agentapp-sdk==[0-9]+(\.[0-9]+){0,3}(\.post[0-9]+)? --hash=sha256:[0-9a-f]{64}$'

remove_handover() {
  rm -rf "$HANDOVER_DIR"
}
error_register_cleanup remove_handover

require_pin() {
  if [ ! -f "$PIN_FILE" ] || [ -L "$PIN_FILE" ]; then
    log_error "The SDK job handed over no sdk.txt"
    exit 1
  fi
  if [ "$(wc -l < "$PIN_FILE")" -ne 1 ] || ! grep -Eq "$PIN_PATTERN" "$PIN_FILE"; then
    log_error "The handed-over sdk.txt is not one hash-locked SDK requirement"
    exit 1
  fi
}

install_sdk() {
  env -u PIP_EXTRA_INDEX_URL -u PIP_INDEX_URL -u PIP_FIND_LINKS -u PIP_TRUSTED_HOST \
    PIP_CONFIG_FILE=/dev/null \
    timeout "$INSTALL_TIMEOUT" python -I -m pip install \
    --disable-pip-version-check --no-input --quiet --no-cache-dir \
    --no-deps --only-binary :all: --require-hashes \
    --no-index --find-links "$HANDOVER_DIR" -r "$PIN_FILE"
}

main() {
  require_pin
  local requirement
  requirement="$(cut -d' ' -f1 "$PIN_FILE")"
  # Nothing from the app's checkout may be importable as pip or readable as pip config.
  cd "$HANDOVER_DIR"
  install_sdk
  cd /
  remove_handover
  log_info "${requirement} installed"
}

main "$@"
