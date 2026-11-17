#!/usr/bin/env bash
# The SDK job: downloads the Pippa SDK wheel from the SDK-only package repository, at the version
# and hash of the SDK parameter, into the hand-over directory the workflow passes to the check
# job (as a cache entry of this run, never an artifact: artifacts of a public repository can be
# downloaded by anyone). Uses the credentials of the SDK read role from the previous step; the
# repository token lives only in this script and nothing is written to pip config. This job runs
# no app code and checks nothing out.
#
# Hand-over directory: sdk.txt (the hash-locked requirement) and the wheel, nothing else.
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
HANDOVER_DIR="${RUNNER_TEMP}/hack-sdk-handover"

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
    "--out=${HANDOVER_DIR}/sdk.txt"
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
    --index-url "$url" --dest "$HANDOVER_DIR" -r "${HANDOVER_DIR}/sdk.txt"
}

require_one_wheel() {
  local wheels=() entries=()
  shopt -s nullglob dotglob
  wheels=("${HANDOVER_DIR}"/pippa_agentapp_sdk-*.whl)
  entries=("${HANDOVER_DIR}"/*)
  shopt -u nullglob dotglob
  if [ "${#wheels[@]}" -ne 1 ] || [ "${#entries[@]}" -ne 2 ]; then
    log_error "Expected sdk.txt and one SDK wheel to hand over, found ${#entries[@]} file(s)"
    exit 1
  fi
}

main() {
  remove_all
  mkdir -m 700 "$WORK_DIR" "$HANDOVER_DIR"
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

  cd /
  remove_work_dir
  log_info "${requirement} fetched for the check job"
}

main "$@"
