#!/usr/bin/env bash
# The SDK job: downloads the Pippa SDK wheel from the SDK-only package repository, at the version
# and hash of the SDK parameter, and seals it into the hand-over directory the workflow passes to
# the check job as a cache entry of this run (never an artifact: artifacts of a public repository
# can be downloaded by anyone). Uses the credentials of the SDK read role from the previous step;
# the repository token lives only in this script and nothing is written to pip config. This job
# runs no app code and checks nothing out.
#
# A pull request's run, also one from a fork, and a branch's run can restore the caches of main,
# so the hand-over is sealed: a tar archive of sdk.txt (the hash-locked requirement) and the wheel,
# encrypted (AES-256-CBC, PBKDF2) with a random key made for this run. The key leaves only as the
# step output `handover_key`, which reaches this run's check job as a job output and no other run.
#
# Hand-over directory: sdk.tar.enc, nothing else. Output: handover_key (64 hex characters).
set -euo pipefail

LOG_SCOPE="hack-check/fetch-sdk"

ACTION_DIR="${GITHUB_ACTION_PATH}"
COMMON_DIR="${ACTION_DIR}/../../scripts/common"
HACK_DIR="${ACTION_DIR}/../../scripts/hack"

# shellcheck source=../../../scripts/common/logger.sh
source "${COMMON_DIR}/logger.sh"
# shellcheck source=../../../scripts/common/error-handler.sh
source "${COMMON_DIR}/error-handler.sh"

ACCOUNT_ID="780886633355"
ENVIRONMENT="prod"
REGION="eu-west-2"
REGION_SHORT="euw2"
COMPONENT="hack-sdk"
DOMAIN="pippa"
REPOSITORY="pippa-${ENVIRONMENT}-codeartifact-${REGION_SHORT}-${COMPONENT}"
PARAMETER="/pippa/${ENVIRONMENT}/hack/sdk"
TOKEN_SECONDS=900
DOWNLOAD_TIMEOUT=120
WORK_DIR="${RUNNER_TEMP}/hack-sdk"
# sdk.txt and the wheel in the clear; they never leave this job.
SDK_DIR="${WORK_DIR}/sdk"
HANDOVER_DIR="${RUNNER_TEMP}/hack-sdk-handover"
SEALED="${HANDOVER_DIR}/sdk.tar.enc"

remove_work_dir() {
  rm -rf "$WORK_DIR"
}

remove_all() {
  remove_work_dir
  rm -rf "$HANDOVER_DIR"
}
error_register_cleanup remove_all

read_pin() {
  aws ssm get-parameter --region "$REGION" --name "$PARAMETER" \
    --query Parameter.Value --output text > "${WORK_DIR}/parameter.json"
  python -I "${HACK_DIR}/sdk_pin.py" \
    "--parameter-file=${WORK_DIR}/parameter.json" \
    "--out=${SDK_DIR}/sdk.txt"
}

repository_endpoint() {
  aws codeartifact get-repository-endpoint --region "$REGION" \
    --domain "$DOMAIN" --domain-owner "$ACCOUNT_ID" --repository "$REPOSITORY" \
    --format pypi --query repositoryEndpoint --output text
}

repository_token() {
  aws codeartifact get-authorization-token --region "$REGION" \
    --domain "$DOMAIN" --domain-owner "$ACCOUNT_ID" \
    --duration-seconds "$TOKEN_SECONDS" --query authorizationToken --output text
}

index_url() {
  local endpoint="${1#https://}" token="$2"
  printf 'https://aws:%s@%s/simple/' "$token" "${endpoint%/}"
}

download_sdk() {
  local url="$1"
  env -u PIP_EXTRA_INDEX_URL -u PIP_INDEX_URL -u PIP_FIND_LINKS -u PIP_TRUSTED_HOST \
    PIP_CONFIG_FILE=/dev/null \
    timeout "$DOWNLOAD_TIMEOUT" python -I -m pip download \
    --disable-pip-version-check --no-input --quiet --no-cache-dir \
    --no-deps --only-binary :all: --require-hashes \
    --index-url "$url" --dest "$SDK_DIR" -r "${SDK_DIR}/sdk.txt"
}

require_one_wheel() {
  local wheels=() entries=()
  shopt -s nullglob dotglob
  wheels=("${SDK_DIR}"/pippa_agentapp_sdk-*.whl)
  entries=("${SDK_DIR}"/*)
  shopt -u nullglob dotglob
  if [ "${#wheels[@]}" -ne 1 ] || [ "${#entries[@]}" -ne 2 ]; then
    log_error "Expected sdk.txt and one SDK wheel to hand over, found ${#entries[@]} file(s)"
    exit 1
  fi
}

# Seals sdk.txt and the one wheel with a new key and writes the key as the step output handover_key.
seal_handover() {
  local wheels=() key
  wheels=("${SDK_DIR}"/pippa_agentapp_sdk-*.whl)
  key="$(openssl rand -hex 32)"
  if ! [[ "$key" =~ ^[0-9a-f]{64}$ ]]; then
    log_error "Could not make a key for the SDK hand-over"
    exit 1
  fi
  tar -C "$SDK_DIR" -cf - sdk.txt "${wheels[0]##*/}" \
    | HANDOVER_KEY="$key" openssl enc -aes-256-cbc -pbkdf2 -md sha256 -salt \
      -pass env:HANDOVER_KEY -out "$SEALED"
  echo "handover_key=${key}" >> "$GITHUB_OUTPUT"
}

main() {
  remove_all
  mkdir -m 700 "$WORK_DIR" "$HANDOVER_DIR"
  mkdir -m 700 "$SDK_DIR"
  # Nothing may be importable or readable as config from the current directory while credentials exist.
  cd "$WORK_DIR"

  local requirement endpoint token
  requirement="$(read_pin)"
  log_info "Fetching ${requirement}"

  endpoint="$(repository_endpoint)"
  token="$(repository_token)"
  if [ -z "$token" ] || [ -z "$endpoint" ]; then
    log_error "The package repository returned no endpoint or no token"
    exit 1
  fi
  echo "::add-mask::${token}"
  download_sdk "$(index_url "$endpoint" "$token")"
  unset token
  require_one_wheel
  seal_handover

  cd /
  remove_work_dir
  log_info "${requirement} fetched and sealed for the check job"
}

main "$@"
