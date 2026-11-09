#!/usr/bin/env bash
# Blanks the AWS credentials for every later step of the job and removes what the
# SDK install could have left behind, before any app code runs.
set -euo pipefail

LOG_SCOPE="hack-check/clear-credentials"

ACTION_DIR="${GITHUB_ACTION_PATH}"
COMMON_DIR="${ACTION_DIR}/../../scripts/common"

# shellcheck source=../../../scripts/common/logger.sh
source "${COMMON_DIR}/logger.sh"
# shellcheck source=../../../scripts/common/error-handler.sh
source "${COMMON_DIR}/error-handler.sh"

CREDENTIAL_VARIABLES=(AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN)
WORK_DIR="${RUNNER_TEMP}/hack-sdk"
REPOSITORY_HOST_MARKER="codeartifact"

pip_config_files() {
  printf '%s\n' \
    "${PIP_CONFIG_FILE:-}" \
    "${XDG_CONFIG_HOME:-${HOME}/.config}/pip/pip.conf" \
    "${HOME}/.config/pip/pip.conf" \
    "${HOME}/.pip/pip.conf"
}

blank_credentials() {
  local name
  for name in "${CREDENTIAL_VARIABLES[@]}"; do
    printf '%s=\n' "$name" >> "$GITHUB_ENV"
  done
  log_info "AWS credentials blanked for the remaining steps"
}

remove_pip_config() {
  local file
  while IFS= read -r file; do
    if [ -n "$file" ] && [ -f "$file" ] && grep -q "$REPOSITORY_HOST_MARKER" "$file"; then
      rm -f "$file"
      log_warn "Removed pip config ${file}"
    fi
  done < <(pip_config_files)
}

main() {
  blank_credentials
  rm -rf "$WORK_DIR"
  remove_pip_config
}

main "$@"
