#!/usr/bin/env bash
# Installs the publisher dependencies from the hash-locked requirements file.
set -euo pipefail

LOG_SCOPE="hack-publish/install"

COMMON_DIR="${GITHUB_ACTION_PATH}/../../scripts/common"
HACK_DIR="${GITHUB_ACTION_PATH}/../../scripts/hack"

# shellcheck source=../../../scripts/common/logger.sh
source "${COMMON_DIR}/logger.sh"
# shellcheck source=../../../scripts/common/error-handler.sh
source "${COMMON_DIR}/error-handler.sh"

INSTALL_TIMEOUT=120

log_info "Installing publisher dependencies"
timeout "$INSTALL_TIMEOUT" python -m pip install --disable-pip-version-check --no-input --quiet \
  --require-hashes --only-binary :all: -r "${HACK_DIR}/requirements.txt"
log_info "Publisher dependencies installed"
