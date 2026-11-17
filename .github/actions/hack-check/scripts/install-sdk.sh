#!/usr/bin/env bash
# The check job: opens the sealed SDK hand-over of the SDK job with this run's key (HANDOVER_KEY,
# the SDK job's output), installs the Pippa SDK wheel from it offline, with the hash of its
# hash-locked requirement, then removes the hand-over and everything opened from it. Runs before
# any app code, in a job that has no credentials and cannot request an OIDC token.
#
# The hand-over must be one file, sdk.tar.enc: a tar archive holding sdk.txt and one SDK wheel,
# nothing else. A hand-over in the clear, a missing or wrong key, or anything else in the archive
# is refused before pip runs.
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
SEALED="${HANDOVER_DIR}/sdk.tar.enc"
SDK_DIR="${RUNNER_TEMP}/hack-sdk-install"
ARCHIVE="${SDK_DIR}/sdk.tar"
PIN_FILE="${SDK_DIR}/sdk.txt"
KEY_PATTERN='^[0-9a-f]{64}$'
WHEEL_PATTERN='^pippa_agentapp_sdk-[A-Za-z0-9_.+-]+\.whl$'
PIN_PATTERN='^pippa-agentapp-sdk==[0-9]+(\.[0-9]+){0,3}(\.post[0-9]+)? --hash=sha256:[0-9a-f]{64}$'

remove_handover() {
  rm -rf "$HANDOVER_DIR" "$SDK_DIR"
}
error_register_cleanup remove_handover

require_sealed_handover() {
  local entries=()
  shopt -s nullglob dotglob
  entries=("${HANDOVER_DIR}"/*)
  shopt -u nullglob dotglob
  if [ "${#entries[@]}" -ne 1 ] || [ "${entries[0]}" != "$SEALED" ] || [ -L "$SEALED" ] || [ ! -f "$SEALED" ]; then
    log_error "The SDK job handed over something other than the sealed SDK"
    exit 1
  fi
  if ! [[ "${HANDOVER_KEY:-}" =~ $KEY_PATTERN ]]; then
    log_error "The SDK job handed over no key for the sealed SDK"
    exit 1
  fi
}

# Decrypts the hand-over and unpacks it, after checking that it holds sdk.txt and one SDK wheel only.
open_handover() {
  mkdir -m 700 "$SDK_DIR"
  if ! openssl enc -d -aes-256-cbc -pbkdf2 -md sha256 -pass env:HANDOVER_KEY \
    -in "$SEALED" -out "$ARCHIVE" 2>/dev/null; then
    log_error "The sealed SDK does not open with this run's key"
    exit 1
  fi
  unset HANDOVER_KEY

  local names=() name wheels=0
  if ! mapfile -t names < <(tar -tf "$ARCHIVE" 2>/dev/null) || [ "${#names[@]}" -ne 2 ]; then
    log_error "The sealed SDK is not an archive of sdk.txt and one SDK wheel"
    exit 1
  fi
  for name in "${names[@]}"; do
    if [[ "$name" =~ $WHEEL_PATTERN ]]; then
      wheels=$((wheels + 1))
    elif [ "$name" != "sdk.txt" ]; then
      log_error "The sealed SDK is not an archive of sdk.txt and one SDK wheel"
      exit 1
    fi
  done
  if [ "$wheels" -ne 1 ]; then
    log_error "The sealed SDK is not an archive of sdk.txt and one SDK wheel"
    exit 1
  fi
  tar -xf "$ARCHIVE" -C "$SDK_DIR" --no-same-owner --no-same-permissions -- "${names[@]}"
  rm -f "$ARCHIVE"
}

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
    --no-index --find-links "$SDK_DIR" -r "$PIN_FILE"
}

main() {
  rm -rf "$SDK_DIR"
  require_sealed_handover
  open_handover
  require_pin
  local requirement
  requirement="$(cut -d' ' -f1 "$PIN_FILE")"
  # Nothing from the app's checkout may be importable as pip or readable as pip config.
  cd "$SDK_DIR"
  install_sdk
  cd /
  remove_handover
  log_info "${requirement} installed"
}

main "$@"
